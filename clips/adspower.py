"""AdsPower Local API: one profile = one Instagram account with its own proxy (H2).

Docs: https://localapi-doc-en.adspower.com/
"""
import os
import time

import requests


class ProfileBusy(Exception):
    """The profile is already open here (operator is using it): do not touch it."""


class AdsPowerError(Exception):
    """Local API answered with code != 0 (also: profile open on another device of the team)."""


def _get(cfg, path, **params) -> dict:
    time.sleep(cfg["adspower"]["request_interval_sec"])  # some endpoints allow 1 request/second
    key = os.environ.get("ADSPOWER_API_KEY", "")
    r = requests.get(cfg["adspower"]["base_url"] + path, params=params, timeout=120,
                     headers={"Authorization": f"Bearer {key}"} if key else {})
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise AdsPowerError(f"{path} {params}: {j.get('msg')}")
    return j.get("data") or {}


def is_active(cfg, profile_id) -> bool:
    return _get(cfg, "/api/v1/browser/active", user_id=profile_id).get("status") == "Active"


def start(cfg, profile_id) -> str:
    """Start the profile's browser, return its CDP websocket URL."""
    if is_active(cfg, profile_id):
        raise ProfileBusy(profile_id)
    d = _get(cfg, "/api/v1/browser/start", user_id=profile_id,
             headless=1 if cfg["adspower"]["headless_browser"] else 0, open_tabs=1, ip_tab=0)
    return d["ws"]["puppeteer"]


def stop(cfg, profile_id) -> None:
    _get(cfg, "/api/v1/browser/stop", user_id=profile_id)
