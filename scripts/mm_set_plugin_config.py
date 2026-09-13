#!/usr/bin/env python3
"""Set per-plugin settings (SharedSecret, BotUserID) for hermes-bridge via the
global config's PluginSettings.Plugins.<id> map, then read back.

Plugin settings are stored under config.PluginSettings.Plugins["hermes-bridge"]
and are applied to the plugin on save (server re-runs OnConfigurationChange).
"""
import json
import os
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
    with urllib.request.urlopen(r, timeout=90) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


secret = os.environ["BRIDGE_SECRET"].strip()
bot = os.environ["BRIDGE_BOT_USER_ID"].strip()
cfg = req("GET", "/api/v4/config")
ps = cfg["PluginSettings"]
plugins = ps.get("Plugins") or {}
plugins["hermes-bridge"] = {"SharedSecret": secret, "BotUserID": bot}
ps["Plugins"] = plugins
cfg["PluginSettings"] = ps
req("PUT", "/api/v4/config", cfg)

# read back
cfg2 = req("GET", "/api/v4/config")
got = (cfg2["PluginSettings"].get("Plugins") or {}).get("hermes-bridge", {})
print("hermes-bridge settings now:", json.dumps(got))
print("SharedSecret set:", bool(got.get("SharedSecret")), "| BotUserID:", got.get("BotUserID"))