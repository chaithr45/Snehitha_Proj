#!/usr/bin/env python3
"""Tests for supervisor.py (standard library only):  python -m unittest firmware/tools/test_supervisor.py"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import supervisor  # noqa: E402


def post(base, path, body, raw=False):
    data = body if raw else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, r.read().decode()


def alert(aid, event="ALERT_FALL", needs_ack=True, worker="w1", **kw):
    return dict(type="alert", id=aid, worker=worker, event=event, detail="test", needs_ack=needs_ack, **kw)


class SupervisorTest(unittest.TestCase):
    def start(self, **kw):
        fd, self.log = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.remove, self.log)
        server = supervisor.make_server("127.0.0.1", 0, self.log, **kw)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", server.station

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
        post(base, "/ack", {"id": "c-1"})
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[1]["acked"], ["b-1"])

    def test_retries_are_not_duplicated(self):
        base, st = self.start()
        for _ in range(3):   # store-and-forward may resend the same alert
            self.assertEqual(post(base, "/alert", alert("b-7"))[0], 200)
        self.assertEqual(len(st.snapshot()["alerts"]), 1)
        with open(self.log) as f:
            self.assertEqual(sum(1 for line in f if '"kind": "alert"' in line), 1)

    def test_dashboard_ack_button(self):
        base, st = self.start()
        post(base, "/alert", alert("b-1"))
        post(base, "/alert", alert("b-2", event="ALERT_CLEARED", needs_ack=False))
        page = get(base, "/")[1]
        self.assertEqual(page.count("<button>Acknowledge</button>"), 1)   # only for the real alert
        req = urllib.request.Request(base + "/ack", data=b"id=b-1", method="POST")
        urllib.request.urlopen(req, timeout=5)   # form post, follows the redirect to /
        self.assertIsNotNone(st.by_id["b-1"]["acked_at"])
        self.assertNotIn("<button>Acknowledge</button>", get(base, "/")[1])

    def test_auto_ack_only_for_alerts_that_need_it(self):
        base, st = self.start(auto_ack=0.1)
        post(base, "/alert", alert("b-1"))
        post(base, "/alert", alert("b-2", event="DEVICE_OK", needs_ack=False))
        time.sleep(0.5)
        self.assertEqual(post(base, "/telemetry", {"worker": "w1"})[1]["acked"], ["b-1"])

    def test_online_offline(self):
        base, st = self.start(offline_after=0.3)
        post(base, "/telemetry", {"worker": "w1", "state": "NORMAL", "vitals_reliable": False})
        self.assertTrue(st.snapshot()["workers"]["w1"]["online"])
        self.assertIn("vitals paused", get(base, "/")[1])
        time.sleep(0.5)
        self.assertFalse(st.snapshot()["workers"]["w1"]["online"])
        self.assertIn("OFFLINE", get(base, "/")[1])

    def test_bad_requests(self):
        base, _ = self.start()
        self.assertEqual(post(base, "/alert", b"not json", raw=True)[0], 400)
        self.assertEqual(post(base, "/alert", b"[1, 2]", raw=True)[0], 400)
        self.assertEqual(post(base, "/nowhere", {})[0], 404)
        self.assertEqual(post(base, "/ack", {"id": "missing"})[0], 404)

    def test_html_is_escaped(self):
        base, _ = self.start()
        post(base, "/alert", alert("x-1", worker="<script>alert(1)</script>"))
        page = get(base, "/")[1]
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
