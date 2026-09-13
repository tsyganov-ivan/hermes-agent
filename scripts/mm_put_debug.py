#!/usr/bin/env python3
"""Print the raw HTTP status/body of the PUT that toggles EnableUploads — see why it's rejected."""
import json
import os
import urllib.request
import urllib.error

BASE = os.environ["MATTERMOST_URL"].rstrip("/")
TOKEN = os.environ["MATTERMOST_TOKEN"]
UA = "HermesDeploy/1.0"


def get(path):
    r = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": UA})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


cfg = get("/api/v4/config")
cfg["PluginSettings"]["EnableUploads"] = True
r = urllib.request.Request(
    BASE + "/api/v4/config",
    data=json.dumps(cfg).encode(),
    headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": UA, "Content-Type": "application/json"},
    method="PUT",
)
try:
    with urllib.request.urlopen(r, timeout=90) as resp:
        print("PUT status:", resp.status)
        raw = resp.read()
        print("PUT body:", raw[:500] if raw else "<empty>")
except urllib.error.HTTPError as e:
    print("PUT HTTPError status:", e.code)
    print("PUT body:", e.read()[:800])