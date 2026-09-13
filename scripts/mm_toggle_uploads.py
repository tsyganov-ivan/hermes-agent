#!/usr/bin/env python3
"""Toggle Mattermost PluginSettings.EnableUploads on the live server via admin token.

Downloads the current config, changes ONLY EnableUploads, PUTs it back, and
verifies the effective value. Reads token/URL from the environment (sourced from
~/.hermes/.env by the caller), so secrets never appear in argv or logs.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ["MATTERMOST_URL"].rstrip("/")
TOKEN = os.environ["MATTERMOST_TOKEN"]
UA = "HermesDeploy/1.0"


def req(method, path, body=None):
    r = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": UA,
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(r, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


want = (sys.argv[1].lower() in {"1", "true", "on"}) if len(sys.argv) > 1 else True
cfg = req("GET", "/api/v4/config")
cur = cfg["PluginSettings"]["EnableUploads"]
print(f"EnableUploads before: {cur}")
if cur == want:
    print("already set; not writing")
else:
    cfg["PluginSettings"]["EnableUploads"] = want
    req("PUT", "/api/v4/config", cfg)
    cfg2 = req("GET", "/api/v4/config")
    print(f"EnableUploads after:  {cfg2['PluginSettings']['EnableUploads']}")