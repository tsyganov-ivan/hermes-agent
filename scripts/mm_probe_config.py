#!/usr/bin/env python3
"""Probe whether PluginSettings config writes actually persist.
Checks: does the PUT echo contain EnableUploads=true, and does any other
plugin setting write persist? Also reports ServiceSettings write persistence
(a control group) to tell "all config writes fail (read-only/env)" vs
"only plugin settings are protected"."""
import json
import os
import urllib.request
import urllib.error

BASE = os.environ["MATTERMOST_URL"].rstrip("/")
TOKEN = os.environ["MATTERMOST_TOKEN"]
UA = "HermesProbe/1.0"


def get(path):
    r = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": UA})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


def put(body):
    r = urllib.request.Request(
        BASE + "/api/v4/config",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": UA, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode(errors="replace")


cfg = get("/api/v4/config")
ps = cfg["PluginSettings"]
print("GET EnableUploads:", ps.get("EnableUploads"))
# Probe 1: set EnableUploads=true
cfg["PluginSettings"]["EnableUploads"] = True
st, echo = put(cfg)
print("PUT1 status:", st, "| echo EnableUploads:", echo.get("PluginSettings", {}).get("EnableUploads"))
# Probe 2 (control): toggle a ServiceSettings boolean and read back
sv = cfg["ServiceSettings"]
# Use EnableUserAccessTokens as a safe reversible write
key = "EnableUserAccessTokens"
was = sv.get(key)
sv[key] = not bool(was)
st2, echo2 = put(cfg)
print(f"PUT2 ({key}) status:", st2, "| echo:", echo2.get("ServiceSettings", {}).get(key))
readback = get("/api/v4/config")
print("RE-READ EnableUploads:", readback["PluginSettings"].get("EnableUploads"),
      f"| {key}:", readback["ServiceSettings"].get(key))
# restore control
sv[key] = was
put(cfg)