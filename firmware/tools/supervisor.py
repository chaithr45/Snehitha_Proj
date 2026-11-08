#!/usr/bin/env python3
"""Supervisor station: receives alerts and status from the worker devices.

    python firmware/tools/supervisor.py --port 8080 --log alerts.jsonl

Devices POST JSON to /alert and /telemetry (firmware/main/net.c). Open
http://<this-machine>:8080/ for the live page: every worker's state, heart rate, SpO2,
posture and connection, and every alert with an Acknowledge button. An acknowledgment
is returned to the device in its next reply, which switches its buzzer to
"help is on the way". Standard library only.

    --auto-ack SECONDS   acknowledge alerts automatically (testing only)
"""
import argparse
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class Station:
    """All supervisor state, shared by the request threads."""

    def __init__(self, log_path=None, auto_ack=None, offline_after=90.0):
        self.lock = threading.Lock()
        self.workers = {}    # worker id -> latest status
        self.alerts = []     # newest last
        self.by_id = {}      # alert id -> alert
        self.log_path, self.auto_ack, self.offline_after = log_path, auto_ack, offline_after

    def _log(self, rec):
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def acked_for(self, worker):
        """IDs of this worker's acknowledged alerts (sent back in every reply)."""
        with self.lock:
            ids = [a["id"] for a in self.alerts if a.get("worker") == worker and a.get("acked_at")]
        return ids[-50:]

    def alert(self, msg):
        now = time.time()
        aid = msg.get("id") or f"anon-{len(self.alerts) + 1}"
        with self.lock:
            if aid in self.by_id:       # a retry of an alert we already have
                return False
            rec = dict(msg, id=aid, received=now, acked_at=None, kind="alert")
            self.alerts.append(rec)
            self.by_id[aid] = rec
            del self.alerts[:-500]
            w = self.workers.setdefault(msg.get("worker", "?"), {})
            w["last_alert"], w["last_seen"] = msg.get("event"), now
            self._log(rec)
        extra = ", ".join(f"{k}={msg[k]}" for k in ("posture", "impact_g", "hr", "spo2") if k in msg)
        print(f"\a*** {msg.get('event')} *** {msg.get('worker')}: {msg.get('detail')} ({extra}) id={aid}",
              flush=True)
        if self.auto_ack is not None and msg.get("needs_ack"):
            threading.Timer(self.auto_ack, self.ack, args=(aid, "auto")).start()
        return True

    def telemetry(self, msg):
        with self.lock:
            w = self.workers.setdefault(msg.get("worker", "?"), {})
            w.update(msg, last_seen=time.time())
            self._log(dict(msg, kind="telemetry", received=time.time()))

    def ack(self, aid, by="supervisor"):
        with self.lock:
            rec = self.by_id.get(aid)
            if not rec or rec["acked_at"]:
                return False
            rec["acked_at"], rec["acked_by"] = time.time(), by
            self._log({"kind": "ack", "id": aid, "by": by, "received": rec["acked_at"]})
        print(f"acknowledged {aid} ({by})", flush=True)
        return True

    def snapshot(self):
        now = time.time()
        with self.lock:
            workers = {k: dict(v, online=now - v.get("last_seen", 0) < self.offline_after)
                       for k, v in self.workers.items()}
            return dict(workers=workers, alerts=[dict(a) for a in self.alerts])


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<title>Worker safety</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1d2433;--muted:#5b6475;--line:#e3e6eb;--bad:#b42318;--warn:#b54708;--ok:#067647}}
body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:var(--bg);color:var(--ink)}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:24px 0 8px;color:var(--muted)}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:var(--card)}}
td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;font-size:14px;white-space:nowrap}}
th{{color:var(--muted);font-weight:600}} .ALERT,.bad{{color:var(--bad);font-weight:600}}
.FALL_PENDING,.MAN_DOWN_PENDING,.warn{{color:var(--warn);font-weight:600}} .ok{{color:var(--ok)}}
button{{font:inherit;padding:3px 10px;border:1px solid var(--bad);color:var(--bad);background:#fff;border-radius:4px;cursor:pointer}}
small{{color:var(--muted)}}
</style></head><body><h1>Worker safety monitor</h1><small>updated {now}</small>
<h2>Workers</h2><div class="wrap"><table><tr><th>Worker</th><th>Link</th><th>State</th><th>HR</th>
<th>SpO2</th><th>Posture</th><th>Notes</th><th>Last alert</th></tr>{workers}</table></div>
<h2>Alerts</h2><div class="wrap"><table><tr><th>Received</th><th>Worker</th><th>Event</th><th>Detail</th>
<th>Posture</th><th>Impact</th><th>HR</th><th>SpO2</th><th>Acknowledged</th></tr>{alerts}</table></div>
</body></html>"""


def render(snap):
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
        rows.append(f"<tr><td>{e(k)}</td><td>{link}</td><td class='{e(w.get('state'))}'>{e(w.get('state'))}</td>"
                    f"<td>{e(w.get('hr'))}</td><td>{e(w.get('spo2'))}</td><td>{e(w.get('posture'))}</td>"
                    f"<td>{'; '.join(notes) or '-'}</td><td>{e(w.get('last_alert'))}</td></tr>")
    alerts = []
    for a in reversed(snap["alerts"]):
        if a.get("acked_at"):
            ack = f"<span class='ok'>{ts(a['acked_at'])}</span>"
        elif a.get("needs_ack"):
            ack = (f"<form method='post' action='/ack'><input type='hidden' name='id' value='{e(a['id'])}'>"
                   "<button>Acknowledge</button></form>")
        else:
            ack = "-"
        cls = "bad" if a.get("needs_ack") else ""
        impact = f"{a['impact_g']} g" if "impact_g" in a else "-"
        alerts.append(f"<tr><td>{ts(a['received'])}</td><td>{e(a.get('worker'))}</td>"
                      f"<td class='{cls}'>{e(a.get('event'))}</td><td>{e(a.get('detail'))}</td>"
                      f"<td>{e(a.get('posture'))}</td><td>{e(impact)}</td><td>{e(a.get('hr'))}</td>"
                      f"<td>{e(a.get('spo2'))}</td><td>{ack}</td></tr>")
    return PAGE.format(now=time.strftime("%H:%M:%S"), workers="".join(rows), alerts="".join(alerts))


def make_handler(station):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="application/json"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            if self.path == "/ack":
                raw = self._body()
                try:
                    aid = json.loads(raw)["id"]
                    form = False
                except (ValueError, KeyError, TypeError):
                    aid, form = parse_qs(raw.decode()).get("id", [None])[0], True
                ok = bool(aid) and station.ack(aid)
                if form:   # dashboard button: back to the page
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                else:
                    self._reply(200 if ok else 404, json.dumps({"ok": ok}))
                return
            kind = {"/alert": "alert", "/telemetry": "telemetry"}.get(self.path)
            if kind is None:
                self._reply(404, json.dumps({"ok": False, "error": "unknown path"}))
                return
            try:
                msg = json.loads(self._body())
                if not isinstance(msg, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as err:
                self._reply(400, json.dumps({"ok": False, "error": str(err)}))
                return
            station.alert(msg) if kind == "alert" else station.telemetry(msg)
            self._reply(200, json.dumps({"ok": True, "acked": station.acked_for(msg.get("worker"))}))

        def do_GET(self):
            if self.path == "/api/state":
                self._reply(200, json.dumps(station.snapshot()))
            elif self.path in ("/", "/index.html"):
                self._reply(200, render(station.snapshot()), "text/html; charset=utf-8")
            else:
                self._reply(404, json.dumps({"ok": False, "error": "unknown path"}))

        def log_message(self, fmt, *args):
            pass   # keep the console for alerts

    return Handler


def make_server(host="0.0.0.0", port=8080, log_path=None, auto_ack=None, offline_after=90.0):
    station = Station(log_path, auto_ack, offline_after)
    server = ThreadingHTTPServer((host, port), make_handler(station))
    server.station = station
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--log", help="append every message to this JSON-lines file")
    ap.add_argument("--auto-ack", type=float, help="acknowledge alerts after this many seconds (testing)")
    ap.add_argument("--offline-after", type=float, default=90.0, help="seconds without a report = offline")
    a = ap.parse_args()
    server = make_server(a.host, a.port, a.log, a.auto_ack, a.offline_after)
    print(f"supervisor listening on http://{a.host}:{a.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
