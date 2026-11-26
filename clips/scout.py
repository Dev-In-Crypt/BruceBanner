"""Donor reels through a logged-in browser (the AdsPower scout profile): free, no Meta API, no Apify.

Opens instagram.com/<name>/reels/ like a person and reads the JSON the web app loads for itself (XHR answers and
JSON embedded in the page), not the DOM, which changes often. Items come back in Apify field names, so the rest of
the pipeline does not care where they came from. Downloads stay anonymous (CDN link, then yt-dlp): the scout's
cookies never leave the browser (H2).
"""
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from clips import instagram

log = logging.getLogger(__name__)


class ScoutDown(RuntimeError):
    """The scout profile is logged out, checkpointed or blocked: stop, do not keep loading pages."""


def medias(node):
    """Every media dict anywhere in an Instagram JSON answer (REST and GraphQL shapes alike)."""
    if isinstance(node, dict):
        if node.get("code") and node.get("taken_at"):
            yield node
            return
        for v in node.values():
            yield from medias(v)
    elif isinstance(node, list):
        for v in node:
            yield from medias(v)


def _first(m, *keys):
    return next((m[k] for k in keys if m.get(k) is not None), None)


def item(m: dict, owner: str) -> dict | None:
    if m.get("media_type") != 2:  # 2 = video; photos and carousels are not reels
        return None
    cap = m.get("caption")
    versions = m.get("video_versions") or []
    return {"shortCode": m["code"], "ownerUsername": owner, "videoUrl": versions[0].get("url") if versions else None,
            "caption": cap.get("text") if isinstance(cap, dict) else cap,
            "videoPlayCount": _first(m, "play_count", "ig_play_count", "view_count"),
            "likesCount": m.get("like_count"), "commentsCount": m.get("comment_count"),
            "timestamp": datetime.fromtimestamp(int(m["taken_at"]), timezone.utc).isoformat(),
            "videoDuration": m.get("video_duration")}


def collect(bodies, owner: str) -> dict[str, dict]:
    """shortcode -> item from raw JSON texts; later answers (fresher counts) win."""
    out = {}
    for b in bodies:
        try:
            j = json.loads(b)
        except ValueError:
            continue
        for m in medias(j):
            i = item(m, owner)
            if i:
                out[i["shortCode"]] = i
    return out


def reels(cfg, usernames, limit, days=None) -> list[dict]:
    """Up to `limit` latest reels per username, newer than `days`. A donor whose page fails is logged and skipped;
    a scout that is logged out or blocked stops the whole run (alert), so it is not hammered further."""
    sc = cfg["scout"]
    if not sc.get("adspower_id"):
        raise RuntimeError("scout.adspower_id is empty: set the scout AdsPower profile in config.toml")
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    out, bodies = [], []

    def keep(r):  # only queue here: reading a body inside an event handler can stall the sync API
        if "json" in (r.headers.get("content-type") or "") and ("/api/" in r.url or "/graphql" in r.url):
            bodies.append(r)

    with instagram.browser_lock(cfg), instagram.session(cfg, {"adspower_id": sc["adspower_id"], "nick": "scout"}) as page:
        page.on("response", keep)
        page.goto(instagram.IG + "/", wait_until="domcontentloaded")
        health = instagram.check_health(page)
        if health != "ok":
            instagram.alert(f"Разведчик (профиль AdsPower {sc['adspower_id']}): {health}. Сбор роликов остановлен.")
            raise ScoutDown(health)
        for n, name in enumerate(usernames):
            if n:
                page.wait_for_timeout(random.uniform(*sc["between_donors_sec"]) * 1000)
            bodies.clear()
            try:
                page.goto(f"{instagram.IG}/{name}/reels/", wait_until="domcontentloaded")
                got = {}
                for scroll in range(sc["max_scrolls"] + 1):
                    if scroll:
                        page.mouse.wheel(0, random.randint(2500, 4000))
                    page.wait_for_timeout(random.uniform(*sc["pause_sec"]) * 1000)
                    texts = page.locator('script[type="application/json"]').all_inner_texts()
                    for r in list(bodies):
                        try:
                            texts.append(r.text())
                        except Exception:  # noqa: BLE001  (redirects, bodies already gone)
                            pass
                    got = collect(texts, name.lower())
                    oldest = min((i["timestamp"] for i in got.values()), default=None)
                    if len(got) >= limit or (since and oldest and datetime.fromisoformat(oldest) < since):
                        break
                if not got:
                    health = instagram.check_health(page)
                    if health != "ok":
                        instagram.alert(f"Разведчик: {health} на @{name}. Сбор роликов остановлен.")
                        raise ScoutDown(health)
                    log.warning("scout: @%s gave no reels (private, renamed or page changed)", name)
            except ScoutDown:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("scout: @%s skipped: %s", name, e)
                continue
            items = sorted(got.values(), key=lambda i: i["timestamp"], reverse=True)[:limit]
            out += [i for i in items if not since or datetime.fromisoformat(i["timestamp"]) >= since]
            log.info("scout: @%s %s reels", name, len(items))
    return out
