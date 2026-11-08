#!/usr/bin/env python3
"""Supervisor station: receives alerts and telemetry from the worker devices.

    python firmware/tools/supervisor.py --port 8080 --log alerts.jsonl

Devices POST JSON to /alert and /telemetry (see firmware/main/net.c).
Open http://<this-machine>:8080/ for a live status page. Standard library only.
"""
import argparse
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
WORKERS = {}     # worker id -> latest telemetry
ALERTS = []      # newest last
LOG_PATH = None


def record(kind, msg):
    msg = dict(msg, received=time.strftime("%Y-%m-%d %H:%M:%S"), kind=kind)
    with LOCK:
        if kind == "alert":
            ALERTS.append(msg)
            del ALERTS[:-200]
            w = WORKERS.setdefault(msg.get("worker", "?"), {})
            w["last_alert"] = msg.get("event")
        else:
            WORKERS.setdefault(msg.get("worker", "?"), {}).update(msg)
        if LOG_PATH:
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps(msg) + "\n")
    if kind == "alert":
        print(f"\a*** {msg.get('event')} *** worker {msg.get('worker')}: {msg.get('detail')} "
              f"(hr={msg.get('hr')}, spo2={msg.get('spo2')})", flush=True)
    else:
        print(f"telemetry {msg.get('worker')}: state={msg.get('state')} hr={msg.get('hr')} "
              f"spo2={msg.get('spo2')}", flush=True)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<title>Worker safety</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#f6f7f9;color:#1d2433}}
table{{border-collapse:collapse;width:100%;background:#fff;margin-bottom:24px}}
td,th{{border-bottom:1px solid #e3e6eb;padding:6px 8px;text-align:left}}
.ALERT{{color:#b42318;font-weight:600}} .FALL_PENDING{{color:#b54708;font-weight:600}}
</style></head><body><h1>Worker safety monitor</h1>
<h2>Workers</h2><table><tr><th>Worker</th><th>State</th><th>HR</th><th>SpO2</th>
<th>Last alert</th><th>Last report</th></tr>{workers}</table>
<h2>Alerts</h2><table><tr><th>Received</th><th>Worker</th><th>Event</th><th>Detail</th>
<th>HR</th><th>SpO2</th></tr>{alerts}</table></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        kind = {"/alert": "alert", "/telemetry": "telemetry"}.get(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(n))
            if kind is None or not isinstance(msg, dict):
                raise ValueError("unknown path or body")
        except (ValueError, json.JSONDecodeError) as e:
            self._reply(400, json.dumps({"ok": False, "error": str(e)}))
            return
        record(kind, msg)
        self._reply(200, json.dumps({"ok": True}))

    def do_GET(self):
        if self.path == "/api/state":
            with LOCK:
                self._reply(200, json.dumps({"workers": WORKERS, "alerts": ALERTS}))
            return
        e = lambda v: html.escape(str(v if v is not None else "-"))
        with LOCK:
            workers = "".join(
                f"<tr><td>{e(k)}</td><td class='{e(w.get('state'))}'>{e(w.get('state'))}</td>"
                f"<td>{e(w.get('hr'))}</td><td>{e(w.get('spo2'))}</td><td>{e(w.get('last_alert'))}</td>"
                f"<td>{e(w.get('received'))}</td></tr>" for k, w in sorted(WORKERS.items()))
            alerts = "".join(
                f"<tr><td>{e(a['received'])}</td><td>{e(a.get('worker'))}</td><td class='ALERT'>"
                f"{e(a.get('event'))}</td><td>{e(a.get('detail'))}</td><td>{e(a.get('hr'))}</td>"
                f"<td>{e(a.get('spo2'))}</td></tr>" for a in reversed(ALERTS))
        self._reply(200, PAGE.format(workers=workers, alerts=alerts), "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        pass   # keep the console for alerts


def main():
    global LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--log", help="append every message to this JSON-lines file")
    a = ap.parse_args()
    LOG_PATH = a.log
    print(f"supervisor listening on http://{a.host}:{a.port}/", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
