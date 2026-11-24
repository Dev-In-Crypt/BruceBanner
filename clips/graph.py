"""Instagram Graph API, Business Discovery: public reels of professional accounts, free instead of Apify.

Needs a long-lived Facebook Login user token (INSTAGRAM_GRAPH_TOKEN) of a Facebook Page linked to one
Instagram professional account. Only donors that are professional (business/creator) accounts can be read.
Items come back in the Apify field names, so the rest of the pipeline does not care where they came from.
Duration is not in the API: it is checked after download.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from clips import config

log = logging.getLogger(__name__)
FIELDS = "id,caption,media_product_type,media_url,permalink,timestamp,like_count,comments_count,view_count"


def _get(cfg, path, params) -> dict:
    r = requests.get(f"{cfg['graph']['base_url']}/{path}", timeout=60,
                     params={**params, "access_token": config.secret("INSTAGRAM_GRAPH_TOKEN")})
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"graph {path}: {j['error'].get('message')}")
    return j


def ig_user(cfg) -> str:
    """The Instagram professional account linked to the token's Facebook Page."""
    for page in _get(cfg, "me/accounts", {"fields": "instagram_business_account"}).get("data", []):
        if page.get("instagram_business_account"):
            return page["instagram_business_account"]["id"]
    raise RuntimeError("graph: no Instagram professional account is linked to the token's Facebook Page")


def item(m: dict, owner: str) -> dict | None:
    code = re.search(r"/(?:reels?|p|tv)/([^/?]+)", m.get("permalink") or "")
    if m.get("media_product_type") != "REELS" or not code:
        return None
    return {"shortCode": code.group(1), "ownerUsername": owner, "videoUrl": m.get("media_url"),
            "caption": m.get("caption"), "videoPlayCount": m.get("view_count"), "likesCount": m.get("like_count"),
            "commentsCount": m.get("comments_count"), "timestamp": m.get("timestamp"), "videoDuration": None}


def reels(cfg, usernames, limit, days=None) -> list[dict]:
    """Up to `limit` latest reels per username, newer than `days`. A donor that fails (not professional, renamed,
    private) is logged and skipped."""
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    user, out = ig_user(cfg), []
    for name in usernames:
        try:
            got, after = [], None
            while len(got) < limit:
                media = f"media{f'.after({after})' if after else ''}.limit({min(limit, 50)}){{{FIELDS}}}"
                bd = _get(cfg, user, {"fields": f"business_discovery.username({name}){{{media}}}"})
                page = bd["business_discovery"].get("media", {})
                got += page.get("data", [])
                after = page.get("paging", {}).get("cursors", {}).get("after")
                old = since and got and datetime.fromisoformat(got[-1]["timestamp"]) < since
                if not after or not page.get("data") or old:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("graph: @%s skipped: %s", name, e)
            continue
        items = [i for i in (item(m, name.lower()) for m in got[:limit]) if i]
        out += [i for i in items if not since or datetime.fromisoformat(i["timestamp"]) >= since]
    return out
