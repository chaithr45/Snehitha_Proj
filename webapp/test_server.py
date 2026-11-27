#!/usr/bin/env python3
"""Tests for the web app (standard library only):  python webapp/test_server.py -v"""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import simulate  # noqa: E402


def request(base, path, body=None, raw=False, headers=None, method=None):
    data = None if body is None else (body if raw else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(base, path, body, **kw):
    code, text = request(base, path, body, **kw)
    return code, json.loads(text or "{}")


def get(base, path, **kw):
    return request(base, path, **kw)


def alert(aid, event="ALERT_FALL", needs_ack=True, worker="w1", detail="test", **kw):
    return dict(type="alert", id=aid, worker=worker, event=event, detail=detail, needs_ack=needs_ack, **kw)


class ServerTest(unittest.TestCase):
    def start(self, db=":memory:", **kw):
        fd, self.log = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.remove, self.log)
        srv = server.make_server("127.0.0.1", 0, self.log, db=db, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.station.close)
        return f"http://127.0.0.1:{srv.server_address[1]}", srv.station

    # ------------------------------------------------------------ device protocol (firmware/main/net.c)
    def test_alert_ack_round_trip(self):
        base, st = self.start()
        code, rep = post(base, "/alert", alert("b-1", posture="lying", impact_g=3.4))
        self.assertEqual((code, rep), (200, {"ok": True, "acked": []}))
        self.assertEqual(post(base, "/ack", {"id": "b-1"})[0], 200)
        # the device learns about the acknowledgment from its next reply
        code, rep = post(base, "/telemetry", {"type": "telemetry", "worker": "w1", "state": "ALERT"})
        self.assertEqual(rep["acked"], ["b-1"])
        # acknowledgments of other workers are not sent to w1
        post(base, "/alert", alert("c-1", worker="w2"))
        post(base, "/api/ack", {"id": "c-1", "by": "Priya"})
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[1]["acked"], ["b-1"])
        self.assertEqual(st.by_id["c-1"]["acked_by"], "Priya")

    def test_retries_are_not_duplicated(self):
        base, st = self.start()
        for _ in range(3):   # store-and-forward may resend the same alert
            self.assertEqual(post(base, "/alert", alert("b-7"))[0], 200)
        self.assertEqual(len(st.snapshot()["alerts"]), 1)
        with open(self.log) as f:
            self.assertEqual(sum(1 for line in f if '"kind": "alert"' in line), 1)

    def test_auto_ack_only_for_alerts_that_need_it(self):
        base, st = self.start(auto_ack=0.1)
        post(base, "/alert", alert("b-1"))
        post(base, "/alert", alert("b-2", event="DEVICE_OK", needs_ack=False))
        time.sleep(0.5)
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[1]["acked"], ["b-1"])

    def test_clearing_events_close_alerts(self):
        base, st = self.start()
        post(base, "/alert", alert("b-1", event="ALERT_FALL"))
        post(base, "/alert", alert("b-2", event="DEVICE_FAULT"))
        self.assertEqual(len(st.worker_view("w1")["open_alerts"]), 2)
        post(base, "/alert", alert("b-3", event="ALERT_CLEARED", needs_ack=False))
        self.assertEqual(st.worker_view("w1")["open_alerts"], ["b-2"])       # a fault is closed by DEVICE_OK only
        post(base, "/alert", alert("b-4", event="DEVICE_OK", needs_ack=False))
        self.assertEqual(st.worker_view("w1")["open_alerts"], [])
        self.assertIsNotNone(st.by_id["b-1"]["cleared_at"])

    def test_online_offline(self):
        base, st = self.start(offline_after=0.3)
        post(base, "/telemetry", {"worker": "w1", "state": "NORMAL", "vitals_reliable": False})
        self.assertTrue(st.snapshot()["workers"]["w1"]["online"])
        self.assertIn("vitals paused", get(base, "/simple")[1])
        time.sleep(0.5)
        self.assertFalse(st.snapshot()["workers"]["w1"]["online"])
        self.assertIn("OFFLINE", get(base, "/simple")[1])

    def test_bad_requests(self):
        base, _ = self.start()
        self.assertEqual(post(base, "/alert", b"not json", raw=True)[0], 400)
        self.assertEqual(post(base, "/alert", b"[1, 2]", raw=True)[0], 400)
        self.assertEqual(post(base, "/nowhere", {})[0], 404)
        self.assertEqual(post(base, "/ack", {"id": "missing"})[0], 404)
        self.assertEqual(post(base, "/api/ack", {"id": "missing"})[0], 404)

    # ------------------------------------------------------------ persistence
    def test_history_survives_a_restart(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        db = os.path.join(tmp, "s.db")
        base, st = self.start(db=db)
        post(base, "/telemetry", {"worker": "w1", "hr": 71, "spo2": 98, "motion_g": 0.1})
        post(base, "/alert", alert("b-1"))
        post(base, "/api/ack", {"id": "b-1", "by": "Sam"})
        post(base, "/alert", alert("b-2", event="ALERT_VITALS"))
        post(base, "/api/workers/w1", {"name": "Asha", "note": "bay 3"})
        st.close()
        base2, st2 = self.start(db=db)
        snap = st2.snapshot()
        self.assertEqual(snap["workers"]["w1"]["name"], "Asha")
        self.assertEqual(snap["workers"]["w1"]["open_alerts"], ["b-2"])
        self.assertEqual(len(snap["workers"]["w1"]["spark"]), 1)
        # a retry of an alert from before the restart is still recognised
        post(base2, "/alert", alert("b-1"))
        self.assertEqual(len(st2.snapshot()["alerts"]), 2)
        self.assertEqual(post(base2, "/telemetry", {"worker": "w1"})[1]["acked"], ["b-1"])

    def test_history_and_csv(self):
        base, st = self.start()
        for hr in (70, 72, 150):
            post(base, "/telemetry", {"worker": "w1", "hr": hr, "spo2": 97, "motion_g": 0.05, "fall_prob": 0.01})
        pts = json.loads(get(base, "/api/history?worker=w1&minutes=5")[1])["points"]
        self.assertEqual([p[2] for p in pts], [70, 72, 150])
        post(base, "/alert", alert("b-1", hr=150, detail="x, \"quoted\""))
        post(base, "/api/ack", {"id": "b-1", "by": "Lee"})
        code, csv_text = get(base, "/api/alerts.csv")
        self.assertEqual(code, 200)
        lines = csv_text.strip().splitlines()
        self.assertTrue(lines[0].startswith("received,worker,worker_name,event"))
        self.assertIn("Lee", lines[1])
        open_only = json.loads(get(base, "/api/alerts?open=1")[1])["alerts"]
        self.assertEqual(open_only, [])

    # ------------------------------------------------------------ live updates
    def test_stream_pushes_updates(self):
        base, st = self.start()
        got = []
        ready = threading.Event()

        def listen():
            with urllib.request.urlopen(base + "/api/stream", timeout=5) as r:
                ready.set()
                for raw in r:
                    line = raw.decode().strip()
                    if line.startswith("data: "):
                        got.append(json.loads(line[6:]))
                        if any(m["type"] == "alert" for m in got):
                            return
        t = threading.Thread(target=listen, daemon=True)
        t.start()
        ready.wait(3)
        time.sleep(0.2)
        post(base, "/telemetry", {"worker": "w9", "hr": 80})
        post(base, "/alert", alert("s-1", worker="w9"))
        t.join(5)
        kinds = [m["type"] for m in got]
        self.assertIn("worker", kinds)
        self.assertIn("alert", kinds)
        w = next(m["worker"] for m in got if m["type"] == "worker")
        self.assertEqual(w["point"][1], 80)

    # ------------------------------------------------------------ access control
    def test_device_token(self):
        base, _ = self.start(token="s3cret")
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[0], 401)
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"}, headers={"X-Device-Token": "nope"})[0], 401)
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"}, headers={"X-Device-Token": "s3cret"})[0], 200)
        self.assertEqual(get(base, "/api/state")[0], 200)          # the browser app needs no device token

    def test_password(self):
        base, _ = self.start(password="pw")
        self.assertEqual(get(base, "/api/state")[0], 401)
        auth = {"Authorization": "Basic " + base64.b64encode(b"anyone:pw").decode()}
        self.assertEqual(get(base, "/api/state", headers=auth)[0], 200)
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[0], 200)  # devices use the token instead
        self.assertEqual(get(base, "/healthz")[0], 200)

    # ------------------------------------------------------------ pages
    def test_pages_and_static_files(self):
        base, _ = self.start()
        code, page = get(base, "/")
        self.assertEqual(code, 200)
        self.assertIn("Worker Safety Monitor", page)
        self.assertEqual(get(base, "/static/app.js")[0], 200)
        self.assertEqual(get(base, "/static/app.css")[0], 200)
        self.assertEqual(get(base, "/static/../server.py")[0], 404)
        self.assertEqual(get(base, "/static/%2e%2e%2fserver.py")[0], 404)
        info = json.loads(get(base, "/api/info")[1])
        self.assertIn("device_urls", info)
        self.assertFalse(info["token_required"])

    def test_simple_page_ack_button_and_escaping(self):
        base, st = self.start()
        post(base, "/alert", alert("b-1"))
        post(base, "/alert", alert("b-2", event="ALERT_CLEARED", needs_ack=False))
        post(base, "/alert", alert("b-3", worker="<script>alert(1)</script>"))
        page = get(base, "/simple")[1]
        self.assertEqual(page.count("<button>Acknowledge</button>"), 1)    # b-1 was cleared by b-2; b-3 is open
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        req = urllib.request.Request(base + "/ack", data=b"id=b-3", method="POST")
        urllib.request.urlopen(req, timeout=5)    # form post, follows the redirect back to /simple
        self.assertIsNotNone(st.by_id["b-3"]["acked_at"])

    def test_demo_fall_command(self):
        base, st = self.start()
        self.assertEqual(post(base, "/api/workers/w1/demo-fall", {})[0], 404)       # unknown worker
        post(base, "/telemetry", {"worker": "w1", "demo": True})
        self.assertEqual(post(base, "/api/workers/w1/demo-fall", {})[0], 200)
        self.assertEqual(post(base, "/telemetry", {"worker": "w2"})[1].get("cmd"), None)   # only for w1
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[1].get("cmd"), "demo_fall")
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[1].get("cmd"), None)   # delivered once

    def test_test_alert_and_rename(self):
        base, st = self.start()
        self.assertTrue(post(base, "/api/test-alert", {})[1]["ok"])
        a = st.snapshot()["alerts"][-1]
        self.assertEqual((a["event"], a["needs_ack"]), ("ALERT_TEST", True))
        self.assertEqual(post(base, "/api/workers/nobody", {"name": "x"})[0], 404)

    # ------------------------------------------------------------ simulator
    def test_simulated_fall_end_to_end(self):
        base, st = self.start(auto_ack=0.05)
        dev = simulate.SimDevice("sim-1", base, period=0.5, seed=3, speed=50)
        dev.scenario("fall")
        events = [a["event"] for a in st.snapshot()["alerts"]]
        self.assertEqual(events, ["ALERT_FALL", "ALERT_CLEARED"])
        fall = st.snapshot()["alerts"][0]
        self.assertEqual(fall["posture"], "lying")
        self.assertIsNotNone(fall["acked_at"])
        self.assertIsNotNone(fall["cleared_at"])
        self.assertIn(fall["id"], dev.acked)

    def test_simulator_queues_while_server_is_down(self):
        base, st = self.start()
        dev = simulate.SimDevice("sim-2", "http://127.0.0.1:9", period=0.5, seed=4, speed=50)  # nothing listens
        dev.send_alert("DEVICE_FAULT", "x", True)
        self.assertEqual(len(dev.queue), 1)
        dev.server = base
        dev.flush()
        self.assertEqual((len(dev.queue), [a["event"] for a in st.snapshot()["alerts"]]), (0, ["DEVICE_FAULT"]))


if __name__ == "__main__":
    unittest.main()
