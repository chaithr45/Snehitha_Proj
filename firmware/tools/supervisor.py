#!/usr/bin/env python3
"""The supervisor station is now the web app in webapp/server.py. This wrapper keeps the
old command working (history is not kept unless --db is given):

    python firmware/tools/supervisor.py --port 8080 --log alerts.jsonl
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "webapp"))
from server import Station, main, make_server  # noqa: E402,F401

if __name__ == "__main__":
    if "--db" not in sys.argv:
        sys.argv += ["--db", ":memory:"]
    main()
