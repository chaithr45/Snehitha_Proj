#!/usr/bin/env python3
"""Download the ready-to-flash firmware into webapp/firmware/, where the web app's Setup
page offers it to the browser flasher (Chrome or Edge, page opened as http://localhost).

    python webapp/get_firmware.py                      latest release
    python webapp/get_firmware.py --tag v2.0.0
    python webapp/get_firmware.py --repo you/your-fork

If the repository is private, download worker_safety_esp32.bin (and/or
worker_safety_esp32s3.bin) from its Releases page, or from the "firmware-esp32" artifact
of a GitHub Actions run, and copy it into webapp/firmware/ yourself.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ("worker_safety_esp32.bin", "worker_safety_esp32s3.bin")


def main():
    ap = argparse.ArgumentParser(description="Download the firmware for the web app's browser flasher")
    ap.add_argument("--repo", default="chaithr45/Snehitha_Proj")
    ap.add_argument("--tag", help="release tag (default: the latest release)")
    a = ap.parse_args()
    api = f"https://api.github.com/repos/{a.repo}/releases/" + (f"tags/{a.tag}" if a.tag else "latest")
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers={"Accept": "application/vnd.github+json"}),
                                    timeout=30) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"No release found at {api} (HTTP {e.code}). If the repository is private, download the .bin "
                 f"files from its Releases page and copy them into {os.path.join(HERE, 'firmware')}.")
    out = os.path.join(HERE, "firmware")
    os.makedirs(out, exist_ok=True)
    got = 0
    for asset in rel.get("assets", []):
        if asset["name"] in NAMES:
            dst = os.path.join(out, asset["name"])
            print(f"downloading {asset['name']} ({asset['size'] / 1024:.0f} kB) from {rel['tag_name']}")
            urllib.request.urlretrieve(asset["browser_download_url"], dst)
            got += 1
    if not got:
        sys.exit(f"Release {rel.get('tag_name')} has no firmware images ({', '.join(NAMES)}).")
    print(f"done: open http://localhost:8080/#/setup and use 'Install' in step 2")


if __name__ == "__main__":
    main()
