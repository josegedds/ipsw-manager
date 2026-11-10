#!/usr/bin/env python3
"""Apple's own list of the firmware each iPhone restores to. Stdlib only.

This is the feed Finder and iTunes read. It's the fallback for when ipsw.me
can't be reached: it has the current restore IPSW for every iPhone (URL,
version, build and SHA-1), but no file sizes, device names or history.

Run as a script, it prints {identifier: {url, version, buildid, sha1sum}} as
JSON, which is how ipsw-manager.sh uses it. webui.py imports it.
"""

import gzip
import json
import plistlib
import sys
import urllib.request

FEED_URL = "https://s.mzstatic.com/version"


def fetch(timeout=60):
    """{identifier: {"url", "version", "buildid", "sha1sum"}} for every iPhone in the feed."""
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "ipsw-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":  # sent gzipped whether or not it was asked for
        raw = gzip.decompress(raw)
    by_version = plistlib.loads(raw)["MobileDeviceSoftwareVersionsByVersion"]
    # Keyed by feed format version; the newest lists every device.
    devices = by_version[max(by_version, key=int)]["MobileDeviceSoftwareVersions"]

    firmware = {}
    for identifier, builds in devices.items():
        if not identifier.startswith("iPhone"):
            continue
        try:
            # What a phone on any build is restored to.
            restore = builds["Unknown"]["Universal"]["Restore"]
            firmware[identifier] = {
                "url": restore["FirmwareURL"],
                "version": restore["ProductVersion"],
                "buildid": restore.get("BuildVersion"),
                "sha1sum": restore.get("FirmwareSHA1"),
            }
        except (KeyError, TypeError):
            continue
    if not firmware:
        raise ValueError("no iPhone firmware in Apple's feed")
    return firmware


if __name__ == "__main__":
    try:
        json.dump(fetch(), sys.stdout)
    except Exception as exc:
        print(f"Couldn't load Apple's firmware feed: {exc}", file=sys.stderr)
        sys.exit(1)
