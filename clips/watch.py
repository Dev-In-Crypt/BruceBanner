"""Campaign watcher: new promote.fun campaigns -> Telegram, so the operator joins in the first minutes.

Their budgets go in a day or two (the Movie Trailer campaigns of 19 and 24.11.2026 were spent in about two days).
promote.fun's web app reads its campaign list from a public backend query; the watcher reads the same list.
The first run only remembers what is live (no flood of alerts), later runs alert on what is new.
"""
import logging
import re

import requests

from clips import db, telegram

log = logging.getLogger(__name__)
SITE = "promote.fun"


def _query(cfg, path, args) -> object:
    r = requests.post(f"{cfg['watch']['promote_api']}/api/query", timeout=30,
                      json={"path": path, "args": args, "format": "json"})
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(f"{path}: {j.get('errorMessage')}")
    return j["value"]


def promote_campaigns(cfg) -> list[dict]:
    """Active campaigns: slug, title, rate per 1K views, budget, platforms, label names."""
    labels = {lb["_id"]: lb["name"] for lb in _query(cfg, "campaigns:listDiscoveryLabels", {}) or []}
    out, cursor = [], None
    for _ in range(20):  # ponytail: 20 pages x 50, far more than the site ever lists
        v = _query(cfg, "campaigns:listCampaigns", {"status": "active", "sort": "newest", "labelIds": [],
                                                     "paginationOpts": {"numItems": 50, "cursor": cursor}})
        for c in v["page"]:
            out.append({"id": c["_id"], "slug": c.get("slug") or c["_id"], "title": (c.get("title") or "").strip(),
                        "rate": c.get("costPer1kViews"), "budget": c.get("totalBudget"),
                        "platforms": c.get("acceptedPlatforms") or [],
                        "labels": [lb.get("name", "") if isinstance(lb, dict) else labels.get(lb, "")
                                   for lb in c.get("labels") or []]})
        if v.get("isDone"):
            break
        cursor = v["continueCursor"]
    return out


def message(c: dict, hot: bool) -> str:
    plat = ", ".join({"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "twitter": "X"}.get(p, p)
                     for p in c["platforms"]) or "?"
    tag = f" [{', '.join(x for x in c['labels'] if x)}]" if any(c["labels"]) else ""
    return (f"{'🔥 ' if hot else ''}promote.fun: новая кампания «{c['title']}»{tag}\n"
            f"${c['rate']} за 1 000 просмотров, бюджет ${c['budget']:,.0f}, {plat}\n"
            f"https://www.promote.fun/campaigns/{c['slug']}")


def run(cfg, con, dry) -> list[dict]:
    w = cfg["watch"]
    live = promote_campaigns(cfg)
    first = not con.execute("SELECT 1 FROM watched_campaigns WHERE site = ?", (SITE,)).fetchone()
    seen = {r[0] for r in con.execute("SELECT id FROM watched_campaigns WHERE site = ?", (SITE,))}
    new = [c for c in live if c["id"] not in seen]
    for c in new:
        hot = bool(re.search(w["highlight"], f"{c['title']} {' '.join(c['labels'])}", re.I))
        wanted = not w["platforms"] or set(c["platforms"]) & set(w["platforms"])
        if not first and wanted:
            telegram.send(message(c, hot), dry=dry)
        if not dry:
            with con:
                con.execute("INSERT OR IGNORE INTO watched_campaigns (site, id, slug, title, seen_at) VALUES (?,?,?,?,?)",
                            (SITE, c["id"], c["slug"], c["title"], db.now_iso()))
    log.info("watch: %s live, %s new%s", len(live), len(new), " (first run: remembered, no alerts)" if first else "")
    return new
