#!/usr/bin/env python3
"""
Worker Safety Monitor -- the supervisor web app.

    python webapp/server.py                  then open http://localhost:8080/
    python webapp/server.py --demo 3         three simulated workers, no hardware needed

Worker devices (firmware/main/net.c) POST JSON to /alert and /telemetry. Every reply lists
the worker's acknowledged alert IDs: that is how a click on "Acknowledge" reaches the
device, which then tells the worker that help is on the way. The browser app
(webapp/static/) loads /api/state once and then receives live updates over Server-Sent
Events from /api/stream. Telemetry and alerts are stored in SQLite, so history and open
alerts survive a restart. Python standard library only.

  --token SECRET      devices must send the header  X-Device-Token: SECRET
  --password SECRET   the browser app asks for this password (any user name)
  --auto-ack SECONDS  acknowledge alerts automatically (testing only)
  --log FILE          also append every message to a JSON-lines file
"""
import argparse
import base64
import csv
import hmac
import html
import io
import json
import math
import os
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
FIRMWARE_DIR = os.path.join(HERE, "firmware")
SPARK_POINTS = 180            # recent telemetry points kept per worker for the overview charts
RECENT_ALERTS = 1000          # alerts kept in memory (all of them stay in SQLite)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
FIRMWARE_CHIPS = (("esp32", "ESP32"), ("esp32s3", "ESP32-S3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, name TEXT, note TEXT, first_seen REAL,
    last_seen REAL, last_alert TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY, worker TEXT, event TEXT, needs_ack INTEGER,
    received REAL, acked_at REAL, acked_by TEXT, cleared_at REAL, note TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS alerts_received ON alerts(received);
CREATE TABLE IF NOT EXISTS telemetry(worker TEXT, ts REAL, state TEXT, hr REAL, spo2 REAL,
    motion REAL, tilt REAL, prob REAL, rssi REAL);
CREATE INDEX IF NOT EXISTS telemetry_worker_ts ON telemetry(worker, ts);
"""


def num(v):
    """A finite number or None (JSON from devices uses null for 'no reading')."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def lan_addresses():
    """IPv4 addresses other machines on the network can reach this one at."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # no packet is sent; picks the outgoing interface
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    seen = []
    for ip in ips:
        if ip not in seen and not ip.startswith("127."):
            seen.append(ip)
    return seen


def firmware_files():
    if not os.path.isdir(FIRMWARE_DIR):
        return []
    return [(chip, fam) for chip, fam in FIRMWARE_CHIPS
            if os.path.isfile(os.path.join(FIRMWARE_DIR, f"worker_safety_{chip}.bin"))]


class Station:
    """All supervisor state, shared by the request threads."""

    def __init__(self, db=":memory:", log_path=None, auto_ack=None, offline_after=90.0, token=None,
                 retention_days=30):
        self.lock = threading.RLock()
        if db != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
        self.db = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.log_path, self.auto_ack, self.offline_after = log_path, auto_ack, offline_after
        self.token, self.retention = token, retention_days * 86400
        self.workers, self.spark = {}, {}
        self.alerts, self.by_id = [], {}
        self.subs = set()
        self.commands = {}            # worker -> command for its next reply (e.g. "demo_fall")
        self.started = time.time()
        self._online = {}
        self._closed = threading.Event()
        self._load()
        threading.Thread(target=self._monitor, daemon=True).start()

    # ---------------------------------------------------------------- persistence
    def _load(self):
        for wid, name, note, first, last, last_alert, status in self.db.execute(
                "SELECT id, name, note, first_seen, last_seen, last_alert, status FROM workers"):
            self.workers[wid] = dict(id=wid, name=name or wid, note=note or "", first_seen=first, last_seen=last,
                                     last_alert=last_alert, status=json.loads(status or "{}"))
            rows = self.db.execute("SELECT ts, hr, spo2, motion, prob FROM telemetry WHERE worker=? "
                                   "ORDER BY ts DESC LIMIT ?", (wid, SPARK_POINTS)).fetchall()
            self.spark[wid] = deque((list(r) for r in reversed(rows)), maxlen=SPARK_POINTS)
        for row in self.db.execute("SELECT data, received, acked_at, acked_by, cleared_at, note FROM alerts "
                                   "ORDER BY received DESC LIMIT ?", (RECENT_ALERTS,)).fetchall()[::-1]:
            rec = json.loads(row[0])
            rec.update(received=row[1], acked_at=row[2], acked_by=row[3], cleared_at=row[4], note=row[5])
            self.alerts.append(rec)
            self.by_id[rec["id"]] = rec

    def _save_worker(self, w):
        self.db.execute("INSERT OR REPLACE INTO workers VALUES (?,?,?,?,?,?,?)",
                        (w["id"], w["name"], w["note"], w["first_seen"], w["last_seen"], w["last_alert"],
                         json.dumps(w["status"])))

    def _save_alert(self, rec):
        data = {k: v for k, v in rec.items() if k not in ("received", "acked_at", "acked_by", "cleared_at", "note")}
        self.db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rec["id"], rec["worker"], rec.get("event"), int(bool(rec.get("needs_ack"))),
                         rec["received"], rec["acked_at"], rec["acked_by"], rec["cleared_at"], rec["note"],
                         json.dumps(data)))

    def _log(self, rec):
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    # ---------------------------------------------------------------- live updates
    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def _publish(self, kind, payload):
        msg = json.dumps(dict(type=kind, **payload))
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:          # a stalled browser tab: drop it, it reconnects and reloads
                self.unsubscribe(q)

    def close(self):
        self._closed.set()
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        with self.lock:
            self.db.close()

    # ---------------------------------------------------------------- views
    def _worker(self, wid, now):
        w = self.workers.get(wid)
        if w is None:
            w = self.workers[wid] = dict(id=wid, name=wid, note="", first_seen=now, last_seen=now,
                                         last_alert=None, status={})
            self.spark[wid] = deque(maxlen=SPARK_POINTS)
        return w

    def _open_alerts(self, wid):
        return [a["id"] for a in self.alerts
                if a.get("worker") == wid and a.get("needs_ack") and not a.get("acked_at") and not a.get("cleared_at")]

    def worker_view(self, wid, now=None, spark=True):
        now = now or time.time()
        with self.lock:
            w = self.workers[wid]
            v = dict(w["status"])                     # telemetry fields, flat (state, hr, spo2, ...)
            v.update(id=wid, name=w["name"], note=w["note"], first_seen=w["first_seen"], last_seen=w["last_seen"],
                     last_alert=w["last_alert"], online=now - (w["last_seen"] or 0) < self.offline_after,
                     open_alerts=self._open_alerts(wid))
            if spark:
                v["spark"] = [list(p) for p in self.spark.get(wid, ())]
        return v

    def snapshot(self):
        now = time.time()
        with self.lock:
            return dict(server_time=now, offline_after=self.offline_after, version=VERSION,
                        workers={k: self.worker_view(k, now) for k in self.workers},
                        alerts=[dict(a) for a in self.alerts[-300:]])

    # ---------------------------------------------------------------- device messages
    def alert(self, msg):
        now = time.time()
        aid = str(msg.get("id") or f"anon-{int(now * 1000)}")
        wid = str(msg.get("worker") or "?")
        event = str(msg.get("event") or "ALERT")
        changed = []
        with self.lock:
            if aid in self.by_id or self.db.execute("SELECT 1 FROM alerts WHERE id=?", (aid,)).fetchone():
                return False                      # a store-and-forward retry of an alert we already have
            rec = dict(msg, id=aid, worker=wid, event=event, needs_ack=bool(msg.get("needs_ack")), received=now,
                       acked_at=None, acked_by=None, cleared_at=None, note=None, kind="alert")
            # a clearing event closes this worker's earlier alerts of the matching kind
            closes = {"ALERT_CLEARED": lambda e: e.startswith("ALERT_") and e != "ALERT_CLEARED",
                      "DEVICE_OK": lambda e: e == "DEVICE_FAULT"}.get(event)
            if closes:
                for a in self.alerts:
                    if a["worker"] == wid and a.get("needs_ack") and not a["cleared_at"] and closes(a["event"]):
                        a["cleared_at"] = now
                        self._save_alert(a)
                        changed.append(dict(a))
            self.alerts.append(rec)
            self.by_id[aid] = rec
            for old in self.alerts[:-RECENT_ALERTS]:
                self.by_id.pop(old["id"], None)
            del self.alerts[:-RECENT_ALERTS]
            w = self._worker(wid, now)
            w["last_alert"], w["last_seen"] = event, now
            self._save_alert(rec)
            self._save_worker(w)
            self._log(rec)
        for a in changed + [dict(rec)]:
            self._publish("alert", dict(alert=a))
        self._publish("worker", dict(worker=self.worker_view(wid, spark=False)))
        extra = ", ".join(f"{k}={msg[k]}" for k in ("posture", "impact_g", "hr", "spo2") if msg.get(k) is not None)
        bell = "\a" if rec["needs_ack"] else ""
        print(f"{bell}*** {event} *** {wid}: {msg.get('detail', '')} ({extra}) id={aid}", flush=True)
        if self.auto_ack is not None and rec["needs_ack"]:
            threading.Timer(self.auto_ack, self.ack, args=(aid, "auto")).start()
        return True

    def telemetry(self, msg):
        now = time.time()
        wid = str(msg.get("worker") or "?")
        with self.lock:
            w = self._worker(wid, now)
            w["status"].update({k: v for k, v in msg.items() if k not in ("type", "worker")})
            w["last_seen"] = now
            point = [now, num(msg.get("hr")), num(msg.get("spo2")), num(msg.get("motion_g")), num(msg.get("fall_prob"))]
            self.spark[wid].append(point)
            self.db.execute("INSERT INTO telemetry VALUES (?,?,?,?,?,?,?,?,?)",
                            (wid, now, str(msg.get("state", "")), point[1], point[2], point[3],
                             num(msg.get("tilt_deg")), point[4], num(msg.get("rssi"))))
            self._save_worker(w)
            self._log(dict(msg, kind="telemetry", received=now))
        view = self.worker_view(wid, now, spark=False)
        view["point"] = point
        self._publish("worker", dict(worker=view))

    def acked_for(self, wid):
        """IDs of this worker's acknowledged alerts (sent back in every reply to the device)."""
        with self.lock:
            ids = [a["id"] for a in self.alerts if a.get("worker") == wid and a.get("acked_at")]
        return ids[-50:]

    # ---------------------------------------------------------------- supervisor actions
    def ack(self, aid, by="supervisor", note=None):
        with self.lock:
            rec = self.by_id.get(aid)
            if not rec or rec["acked_at"]:
                return False
            rec["acked_at"], rec["acked_by"] = time.time(), str(by or "supervisor")[:60]
            if note:
                rec["note"] = str(note)[:500]
            self._save_alert(rec)
            self._log({"kind": "ack", "id": aid, "by": rec["acked_by"], "received": rec["acked_at"]})
            wid = rec["worker"]
        self._publish("alert", dict(alert=dict(rec)))
        self._publish("worker", dict(worker=self.worker_view(wid, spark=False)))
        print(f"acknowledged {aid} ({rec['acked_by']})", flush=True)
        return True

    def rename(self, wid, name=None, note=None):
        with self.lock:
            w = self.workers.get(wid)
            if not w:
                return False
            if name is not None:
                w["name"] = str(name).strip()[:60] or wid
            if note is not None:
                w["note"] = str(note).strip()[:200]
            self._save_worker(w)
        self._publish("worker", dict(worker=self.worker_view(wid, spark=False)))
        return True

    def request_demo_fall(self, wid):
        """Ask a device to play its demo fall (firmware CONFIG_SAFETY_DEMO_FALL) at its next report."""
        with self.lock:
            if wid not in self.workers:
                return False
            self.commands[wid] = "demo_fall"
        print(f"demo fall requested for {wid}", flush=True)
        return True

    def take_command(self, wid):
        with self.lock:
            return self.commands.pop(wid, None)

    def test_alert(self):
        n = int(time.time() * 1000) % 10 ** 9
        return self.alert(dict(type="alert", id=f"test-{n}", worker="test-alert", event="ALERT_TEST",
                               detail="test alert from the Setup page", needs_ack=True))

    # ---------------------------------------------------------------- queries
    def history(self, wid, since):
        with self.lock:
            rows = self.db.execute("SELECT ts, state, hr, spo2, motion, tilt, prob, rssi FROM telemetry "
                                   "WHERE worker=? AND ts>=? ORDER BY ts", (wid, since)).fetchall()
        step = max(1, len(rows) // 1500)            # enough points for a chart, not more
        return [list(r) for r in rows[::step]]

    def query_alerts(self, worker=None, limit=500, open_only=False, since=0.0):
        sql, args = "SELECT data, received, acked_at, acked_by, cleared_at, note FROM alerts WHERE received>=?", [since]
        if worker:
            sql += " AND worker=?"
            args.append(worker)
        if open_only:
            sql += " AND needs_ack=1 AND acked_at IS NULL AND cleared_at IS NULL"
        sql += " ORDER BY received DESC LIMIT ?"
        args.append(int(limit))
        out = []
        with self.lock:
            for row in self.db.execute(sql, args):
                rec = json.loads(row[0])
                rec.update(received=row[1], acked_at=row[2], acked_by=row[3], cleared_at=row[4], note=row[5])
                out.append(rec)
        return out

    def alerts_csv(self):
        buf = io.StringIO()
        cols = ["received", "worker", "worker_name", "event", "detail", "posture", "impact_g", "hr", "spo2",
                "needs_ack", "acked_at", "acked_by", "seconds_to_ack", "cleared_at", "note", "id"]
        wr = csv.writer(buf)
        wr.writerow(cols)
        iso = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        for a in reversed(self.query_alerts(limit=100000)):
            name = self.workers.get(a["worker"], {}).get("name", a["worker"])
            wr.writerow([iso(a["received"]), a["worker"], name, a.get("event"), a.get("detail", ""), a.get("posture", ""),
                         a.get("impact_g", ""), a.get("hr", ""), a.get("spo2", ""), int(bool(a.get("needs_ack"))),
                         iso(a["acked_at"]), a["acked_by"] or "",
                         f"{a['acked_at'] - a['received']:.1f}" if a["acked_at"] else "", iso(a["cleared_at"]),
                         a["note"] or "", a["id"]])
        return buf.getvalue()

    # ---------------------------------------------------------------- background
    def _monitor(self):
        last_prune = 0.0
        while not self._closed.wait(1.0):
            now = time.time()
            with self.lock:
                flips = []
                for wid, w in self.workers.items():
                    online = now - (w["last_seen"] or 0) < self.offline_after
                    if self._online.get(wid, online) != online:
                        flips.append((wid, online))
                    self._online[wid] = online
            for wid, online in flips:
                print(f"worker {wid} is {'back online' if online else 'OFFLINE (no report for %.0f s)' % self.offline_after}",
                      flush=True)
                self._publish("worker", dict(worker=self.worker_view(wid, now, spark=False)))
            if now - last_prune > 3600:
                last_prune = now
                with self.lock:
                    self.db.execute("DELETE FROM telemetry WHERE ts<?", (now - self.retention,))


# -------------------------------------------------------------------- no-JavaScript page
SIMPLE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<title>Worker safety (simple view)</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#1d2433}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:24px 0 8px;color:#5b6475}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:#fff}}
td,th{{border-bottom:1px solid #e3e6eb;padding:6px 8px;text-align:left;font-size:14px;white-space:nowrap}}
th{{color:#5b6475;font-weight:600}} .bad{{color:#b42318;font-weight:600}} .warn{{color:#b54708;font-weight:600}}
.ok{{color:#067647}} button{{font:inherit;padding:3px 10px;border:1px solid #b42318;color:#b42318;background:#fff;
border-radius:4px;cursor:pointer}} small{{color:#5b6475}}
</style></head><body><h1>Worker safety monitor</h1><small>updated {now} &middot; <a href="/">full app</a></small>
<h2>Workers</h2><div class="wrap"><table><tr><th>Worker</th><th>Link</th><th>State</th><th>HR</th>
<th>SpO2</th><th>Posture</th><th>Notes</th><th>Last alert</th></tr>{workers}</table></div>
<h2>Alerts</h2><div class="wrap"><table><tr><th>Received</th><th>Worker</th><th>Event</th><th>Detail</th>
<th>Posture</th><th>Impact</th><th>HR</th><th>SpO2</th><th>Acknowledged</th></tr>{alerts}</table></div>
</body></html>"""


def render_simple(snap):
    e = lambda v: html.escape(str(v if v is not None else "-"))
    ts = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else "-"
    rows = []
    for k, w in sorted(snap["workers"].items()):
        notes = []
        if w.get("sensor_fault"):
            notes.append("<span class='bad'>motion sensor fault</span>")
        if w.get("vitals_reliable") is False:
            notes.append("vitals paused (moving)")
        if w.get("worn") is False:
            notes.append("<span class='warn'>sensor not on skin</span>")
        link = "<span class='ok'>online</span>" if w["online"] else "<span class='bad'>OFFLINE</span>"
        cls = "bad" if w.get("state") == "ALERT" else "warn" if str(w.get("state", "")).endswith("PENDING") else ""
        rows.append(f"<tr><td>{e(w.get('name', k))}</td><td>{link}</td><td class='{cls}'>{e(w.get('state'))}</td>"
                    f"<td>{e(w.get('hr'))}</td><td>{e(w.get('spo2'))}</td><td>{e(w.get('posture'))}</td>"
                    f"<td>{'; '.join(notes) or '-'}</td><td>{e(w.get('last_alert'))}</td></tr>")
    alerts = []
    for a in reversed(snap["alerts"]):
        if a.get("acked_at"):
            ack = f"<span class='ok'>{ts(a['acked_at'])}</span>"
        elif a.get("needs_ack") and not a.get("cleared_at"):
            ack = (f"<form method='post' action='/ack'><input type='hidden' name='id' value='{e(a['id'])}'>"
                   "<button>Acknowledge</button></form>")
        else:
            ack = "-"
        impact = f"{a['impact_g']} g" if a.get("impact_g") is not None else "-"
        alerts.append(f"<tr><td>{ts(a['received'])}</td><td>{e(a.get('worker'))}</td>"
                      f"<td class='{'bad' if a.get('needs_ack') else ''}'>{e(a.get('event'))}</td><td>{e(a.get('detail'))}</td>"
                      f"<td>{e(a.get('posture'))}</td><td>{e(impact)}</td><td>{e(a.get('hr'))}</td>"
                      f"<td>{e(a.get('spo2'))}</td><td>{ack}</td></tr>")
    return SIMPLE.format(now=time.strftime("%H:%M:%S"), workers="".join(rows), alerts="".join(alerts))


# -------------------------------------------------------------------- HTTP
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
          ".png": "image/png", ".bin": "application/octet-stream", ".ico": "image/x-icon"}


def make_handler(station, password=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"WorkerSafety/{VERSION}"

        def _send(self, code, body, ctype="application/json", headers=()):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(n, 1 << 20)) if n > 0 else b""

        def _authorised_ui(self):
            if not password:
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    pw = base64.b64decode(auth[6:]).decode().split(":", 1)[1]
                    if hmac.compare_digest(pw, password):
                        return True
                except (ValueError, IndexError):
                    pass
            self._send(401, "password required", "text/plain", [("WWW-Authenticate", 'Basic realm="Worker safety"')])
            return False

        def _authorised_device(self, q):
            if not station.token:
                return True
            got = self.headers.get("X-Device-Token") or (q.get("token") or [""])[0]
            if hmac.compare_digest(got, station.token):
                return True
            self._json(401, {"ok": False, "error": "device token missing or wrong"})
            return False

        def _file(self, folder, name):
            path = os.path.join(folder, name)
            if not SAFE_NAME.match(name) or not os.path.isfile(path):
                return self._json(404, {"ok": False, "error": "not found"})
            with open(path, "rb") as f:
                self._send(200, f.read(), CTYPES.get(os.path.splitext(name)[1], "application/octet-stream"))

        # ---------------------------------------------------------------- POST
        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/alert", "/telemetry"):
                if not self._authorised_device(q):
                    return
                try:
                    msg = json.loads(self._body())
                    if not isinstance(msg, dict):
                        raise ValueError("body must be a JSON object")
                except ValueError as err:
                    return self._json(400, {"ok": False, "error": str(err)})
                station.alert(msg) if u.path == "/alert" else station.telemetry(msg)
                wid = str(msg.get("worker") or "?")
                reply = {"ok": True, "acked": station.acked_for(wid)}
                cmd = station.take_command(wid)
                if cmd:
                    reply["cmd"] = cmd
                return self._json(200, reply)
            if not self._authorised_ui():
                return
            raw = self._body()
            if u.path == "/ack":                       # JSON from tools, or the simple page's form
                try:
                    body, form = json.loads(raw), False
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    body, form = {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()}, True
                ok = bool(body.get("id")) and station.ack(str(body["id"]), body.get("by") or "supervisor",
                                                          body.get("note"))
                if form:
                    self.send_response(303)
                    self.send_header("Location", "/simple")
                    self.end_headers()
                    return
                return self._json(200 if ok else 404, {"ok": ok})
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "error": "body must be a JSON object"})
            if u.path == "/api/ack":
                ok = bool(body.get("id")) and station.ack(str(body["id"]), body.get("by"), body.get("note"))
                return self._json(200 if ok else 404, {"ok": ok})
            if u.path.startswith("/api/workers/") and u.path.endswith("/demo-fall"):
                ok = station.request_demo_fall(u.path[len("/api/workers/"):-len("/demo-fall")])
                return self._json(200 if ok else 404, {"ok": ok})
            if u.path.startswith("/api/workers/"):
                ok = station.rename(u.path[len("/api/workers/"):], body.get("name"), body.get("note"))
                return self._json(200 if ok else 404, {"ok": ok})
            if u.path == "/api/test-alert":
                return self._json(200, {"ok": station.test_alert()})
            self._json(404, {"ok": False, "error": "unknown path"})

        # ---------------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            arg = lambda k, d=None: (q.get(k) or [d])[0]
            if u.path == "/healthz":
                return self._json(200, {"ok": True, "version": VERSION})
            if not self._authorised_ui():
                return
            if u.path in ("/", "/index.html"):
                return self._file(STATIC, "index.html")
            if u.path.startswith("/static/"):
                return self._file(STATIC, u.path[len("/static/"):])
            if u.path == "/simple":
                return self._send(200, render_simple(station.snapshot()), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(200, station.snapshot())
            if u.path == "/api/stream":
                return self._stream()
            if u.path == "/api/history":
                minutes = float(arg("minutes", "30"))
                return self._json(200, {"worker": arg("worker"), "points": station.history(
                    arg("worker", ""), time.time() - minutes * 60)})
            if u.path == "/api/alerts":
                return self._json(200, {"alerts": station.query_alerts(
                    arg("worker"), int(arg("limit", "500")), arg("open") == "1", float(arg("since", "0")))})
            if u.path == "/api/alerts.csv":
                return self._send(200, station.alerts_csv(), "text/csv; charset=utf-8",
                                  [("Content-Disposition", 'attachment; filename="alerts.csv"')])
            if u.path == "/api/info":
                port = self.server.server_address[1]
                ips = lan_addresses()
                return self._json(200, dict(version=VERSION, server_time=time.time(), port=port,
                                            addresses=ips, device_urls=[f"http://{ip}:{port}" for ip in ips],
                                            token_required=bool(station.token), offline_after=station.offline_after,
                                            started=station.started,
                                            firmware=[fam for _, fam in firmware_files()]))
            if u.path == "/firmware/manifest.json":
                files = firmware_files()
                if not files:
                    return self._json(404, {"ok": False, "error": "no firmware in webapp/firmware/"})
                return self._json(200, dict(name="Worker Safety Monitor", version=VERSION,
                                            new_install_prompt_erase=True,
                                            builds=[dict(chipFamily=fam, parts=[dict(
                                                path=f"worker_safety_{chip}.bin", offset=0)]) for chip, fam in files]))
            if u.path.startswith("/firmware/"):
                return self._file(FIRMWARE_DIR, u.path[len("/firmware/"):])
            self._json(404, {"ok": False, "error": "unknown path"})

        def _stream(self):
            q = station.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if msg is None:
                        break
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
            except OSError:
                pass                                    # the browser went away
            finally:
                station.unsubscribe(q)

        def log_message(self, fmt, *args):
            pass                                        # keep the console for alerts

    return Handler


def make_server(host="0.0.0.0", port=8080, log_path=None, auto_ack=None, offline_after=90.0, db=":memory:",
                token=None, password=None):
    station = Station(db, log_path, auto_ack, offline_after, token)
    server = ThreadingHTTPServer((host, port), make_handler(station, password))
    server.daemon_threads = True
    server.station = station
    return server


def main():
    ap = argparse.ArgumentParser(description="Worker Safety Monitor web app")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.path.join(HERE, "data", "safety.db"),
                    help="SQLite file for history (':memory:' keeps nothing)")
    ap.add_argument("--log", help="also append every message to this JSON-lines file")
    ap.add_argument("--auto-ack", type=float, help="acknowledge alerts after this many seconds (testing)")
    ap.add_argument("--offline-after", type=float, default=90.0, help="seconds without a report = offline")
    ap.add_argument("--token", help="shared secret devices must send (X-Device-Token header)")
    ap.add_argument("--password", help="password for the browser app")
    ap.add_argument("--demo", type=int, default=0, metavar="N", help="also run N simulated workers")
    a = ap.parse_args()
    try:
        server = make_server(a.host, a.port, a.log, a.auto_ack, a.offline_after, a.db, a.token, a.password)
    except OSError as err:
        sys.exit(f"cannot listen on port {a.port}: {err}. Is another copy running? Try --port 8090.")
    port = server.server_address[1]
    print(f"Worker Safety Monitor {VERSION}", flush=True)
    print(f"  open in this computer's browser:  http://localhost:{port}/", flush=True)
    for ip in lan_addresses():
        print(f"  devices and other computers:      http://{ip}:{port}", flush=True)
    if a.demo:
        sys.path.insert(0, HERE)
        import simulate
        threading.Thread(target=simulate.run_fleet, args=(f"http://127.0.0.1:{port}", a.demo),
                         kwargs=dict(token=a.token), daemon=True).start()
        print(f"  demo: {a.demo} simulated workers", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.station.close()


if __name__ == "__main__":
    main()
