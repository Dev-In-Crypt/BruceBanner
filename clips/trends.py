"""Trend intake: Apify fetch (spec module 2), selection (3), topic filter (4), download (6), dedup (5)."""
import json
import logging
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from clips import config, db, video

log = logging.getLogger(__name__)
API = "https://api.apify.com/v2"


# ---------- Apify (no login, so work accounts are never used for scraping) ----------

def run_actor(cfg, run_input: dict) -> list[dict]:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {config.secret('APIFY_TOKEN')}"
    actor = cfg["apify"]["actor"]
    run = s.post(f"{API}/acts/{actor}/runs", json=run_input, params={"waitForFinish": 60}, timeout=90).json()["data"]
    deadline = time.monotonic() + cfg["apify"]["run_timeout_sec"]
    while run["status"] in ("READY", "RUNNING"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"apify run {run['id']} still {run['status']}")
        run = s.get(f"{API}/actor-runs/{run['id']}", params={"waitForFinish": 60}, timeout=90).json()["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"apify run {run['id']} ended {run['status']}")
    return s.get(f"{API}/datasets/{run['defaultDatasetId']}/items",
                 params={"clean": "true", "format": "json"}, timeout=120).json()


def dump_raw(cfg, name, items) -> Path:
    """Debug trail, also in dry-run; makes --from-file reruns possible."""
    p = cfg["data"] / "raw" / f"{name}_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return p


def views_of(item, cfg) -> int | None:
    field = cfg["apify"]["views_field"]
    other = "videoViewCount" if field == "videoPlayCount" else "videoPlayCount"
    v = item.get(field) if item.get(field) is not None else item.get(other)
    return int(v) if v is not None else None


def ts(s: str | None) -> str | None:
    return db.iso(datetime.fromisoformat(s)) if s else None


# ---------- fetch (module 2) ----------

def mature_counts(con, cfg) -> dict[str, int]:
    cutoff = db.iso(datetime.now(timezone.utc) - timedelta(hours=cfg["trends"]["max_age_hours"]))
    return {r[0]: r[1] for r in con.execute(
        "SELECT donor, COUNT(*) FROM sources WHERE posted_at <= ? GROUP BY donor", (cutoff,))}


def donor_groups(cfg) -> dict[str, str]:
    """donor username (lowercase) -> 'memes' | 'movies' (addendum 2b: film/series cuts are a separate group)."""
    groups = {d.lower(): "memes" for d in cfg["trends"]["donors"]}
    groups.update({d.lower(): "movies" for d in cfg["trends"].get("donors_movies", [])})
    return groups


def fetch(cfg, con, dry, from_file=None) -> int:
    t = cfg["trends"]
    groups = donor_groups(cfg)
    if from_file:
        items = json.loads(Path(from_file).read_text(encoding="utf-8"))
    else:
        blocked = {d.lower() for d in t["blocked_donors"]}
        donors = [d for d in t["donors"] + t.get("donors_movies", []) if d.lower() not in blocked]
        extra = {"includeSharesCount": True} if cfg["apify"].get("include_shares") else {}
        mature = mature_counts(con, cfg)
        seed = [d for d in donors if mature.get(d.lower(), 0) < t["min_donor_samples"]]  # need history for median
        regular = [d for d in donors if d not in seed]
        items = []
        if seed:
            items += run_actor(cfg, {"username": seed, "resultsLimit": t["seed_results"], "skipPinnedPosts": True,
                                     **extra})
        if regular:
            items += run_actor(cfg, {"username": regular, "resultsLimit": t["results_per_donor"],
                                     "onlyPostsNewerThan": f"{t['fresh_days']} days", "skipPinnedPosts": True,
                                     **extra})
        dump_raw(cfg, "trends", items)
    now, n = db.now_iso(), 0
    rows = [(i["shortCode"], (i.get("ownerUsername") or "").lower(), f"https://www.instagram.com/reel/{i['shortCode']}/",
             i["videoUrl"], i.get("caption"), views_of(i, cfg), i.get("likesCount"), i.get("videoDuration"),
             ts(i.get("timestamp")), now, groups.get((i.get("ownerUsername") or "").lower(), "memes"),
             i.get("sharesCount")) for i in items if i.get("shortCode") and i.get("videoUrl")]
    if not dry:
        with con:
            for r in rows:
                con.execute(
                    "INSERT INTO sources (shortcode, donor, url, video_url, caption, views, likes, duration, posted_at,"
                    " fetched_at, donor_group, shares) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shortcode) DO"
                    " UPDATE SET views=excluded.views, likes=excluded.likes, video_url=excluded.video_url,"
                    " fetched_at=excluded.fetched_at, shares=COALESCE(excluded.shares, shares),"
                    " donor_group=excluded.donor_group", r)  # follows the config if a donor moves lists
                n += 1
    log.info("fetch: %s items, %s usable%s", len(items), len(rows), " (dry-run, nothing stored)" if dry else "")
    return len(rows)


# ---------- selection and topic filter (modules 3, 4) ----------

def blocked_reason(caption: str | None, donor: str, cfg) -> str | None:
    if donor.lower() in {d.lower() for d in cfg["trends"]["blocked_donors"]}:
        return "blocked_donor"
    text = (caption or "").lower().replace("ё", "е")
    for p in cfg["filter"]["blocked_patterns"]:
        if re.search(p, text):
            return f"blocked_keyword:{p}"
    return None


def decide(views, age_hours, duration, median, t) -> tuple[str, str | None]:
    if duration is None or not t["min_duration_sec"] <= duration <= t["max_duration_sec"]:
        return "rejected", "duration"
    if age_hours > t["max_age_hours"]:
        return "rejected", "too_old"
    if median is None:
        return "new", "wait:no_baseline"
    if views is not None and views >= max(t["min_views"], median * t["views_to_median_ratio"]):
        return "selected", None
    return "new", "wait:below_threshold"


def donor_median(con, cfg, donor, exclude_id) -> float | None:
    t = cfg["trends"]
    cutoff = db.iso(datetime.now(timezone.utc) - timedelta(hours=t["max_age_hours"]))
    views = [r[0] for r in con.execute(
        "SELECT views FROM sources WHERE donor = ? AND id != ? AND posted_at <= ? AND views IS NOT NULL "
        "ORDER BY posted_at DESC LIMIT ?", (donor, exclude_id, cutoff, t["median_window"]))]
    return statistics.median(views) if len(views) >= t["min_donor_samples"] else None


def share_median(con, cfg, donor, exclude_id) -> float | None:
    """Median shares/views of the donor's mature reels (a sign of high retention), if Apify gave shares."""
    cutoff = db.iso(datetime.now(timezone.utc) - timedelta(hours=cfg["trends"]["max_age_hours"]))
    rates = [r[0] / r[1] for r in con.execute(
        "SELECT shares, views FROM sources WHERE donor = ? AND id != ? AND posted_at <= ? AND shares IS NOT NULL "
        "AND views > 0 ORDER BY posted_at DESC LIMIT ?", (donor, exclude_id, cutoff, cfg["trends"]["median_window"]))]
    return statistics.median(rates) if len(rates) >= 3 and statistics.median(rates) > 0 else None


def rank_score(views, median, duration, shares, share_med, cfg) -> float:
    """Order in which the planner uses selected sources: views vs donor median, weighted by money and retention."""
    t, b, p = cfg["trends"], cfg["banners"], cfg["playerok"]
    score = views / median
    if t.get("voiced_priority") and b.get("midroll") and duration and duration >= b["midroll_min_source_sec"]:
        score *= p["rate_voiced_rub_per_100k"] / p["rate_static_rub_per_100k"]  # voiced banner pays 700 vs 400
    if t.get("share_weight") and shares is not None and views and share_med:
        score *= min(max(shares / views / share_med, 0.5), 2.0)  # bounded, so shares never dominate views
    return score


def select(cfg, con, dry) -> dict:
    now, counts = datetime.now(timezone.utc), {}
    loop = video.banner_duration(cfg)  # addendum 3: a reel shorter than one banner loop is rejected
    for s in con.execute("SELECT * FROM sources WHERE state = 'new'").fetchall():
        reason = blocked_reason(s["caption"], s["donor"], cfg)
        score = None
        if reason:
            state = "rejected"
        else:
            age = (now - db.parse_iso(s["posted_at"])).total_seconds() / 3600 if s["posted_at"] else float("inf")
            median = donor_median(con, cfg, s["donor"], s["id"])
            state, reason = decide(s["views"], age, s["duration"], median, cfg["trends"])
            if state != "rejected" and s["duration"] is not None and s["duration"] < loop:
                state, reason = "rejected", "shorter_than_banner"
            if state == "selected":
                score = rank_score(s["views"], median, s["duration"], s["shares"],
                                   share_median(con, cfg, s["donor"], s["id"]), cfg)
                log.info("selected %s @%s views=%s median=%s score=%.1f", s["shortcode"], s["donor"],
                         s["views"], median, score)
        counts[state] = counts.get(state, 0) + 1
        if not dry:
            with con:
                con.execute("UPDATE sources SET state = ?, reason = ?, score = ? WHERE id = ?",
                            (state, reason, score, s["id"]))
    log.info("select: %s%s", counts, " (dry-run)" if dry else "")
    return counts


# ---------- download and dedup (modules 6, 5) ----------

def download_one(cfg, src) -> Path:
    """Apify CDN link first, yt-dlp as fallback. Anonymous: never with account cookies (H2)."""
    path = cfg["data"] / "sources" / f"{src['shortcode']}.mp4"
    if path.exists():
        try:
            if video.probe(path)["video"]:
                return path
        except RuntimeError:
            pass
    part = path.with_name(path.stem + ".part.mp4")
    proxy = cfg["download"]["proxy"]
    try:
        with requests.get(src["video_url"], stream=True, timeout=60,
                          proxies={"https": proxy, "http": proxy} if proxy else None) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        video.probe(part)
    except Exception as e:  # noqa: BLE001  (expired CDN link, network, broken file)
        log.info("direct download of %s failed (%s), trying yt-dlp", src["shortcode"], e)
        part.unlink(missing_ok=True)
        cmd = [sys.executable, "-m", "yt_dlp", "-q", "-f", "mp4/best", "-o", str(part), src["url"]]
        if proxy:
            cmd[3:3] = ["--proxy", proxy]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    p = video.probe(part)
    if not p["video"] or p["duration"] <= 0:
        part.unlink(missing_ok=True)
        raise ValueError("downloaded file has no video")
    part.replace(path)
    return path


def parse_hashes(s: str) -> list[int]:
    return [int(h, 16) for h in s.split()]


def download(cfg, con, dry) -> dict:
    counts = {}
    for s in con.execute("SELECT * FROM sources WHERE state = 'selected' ORDER BY fetched_at").fetchall():
        if dry:
            log.info("dry-run: would download %s", s["shortcode"])
            continue
        try:
            path = download_one(cfg, s)
            hashes = video.video_hashes(path, cfg["dedup"]["frames"])
        except Exception as e:  # noqa: BLE001
            log.warning("source %s failed: %s", s["shortcode"], e)
            state, fields = "failed", {"reason": str(e)[:300]}
        else:
            # ponytail: linear scan over all hashes; fine for tens of thousands, add a BK-tree if it gets slow
            dup = next((o["shortcode"] for o in con.execute(
                "SELECT shortcode, phash FROM sources WHERE phash IS NOT NULL AND state != 'duplicate' AND id != ?",
                (s["id"],)) if video.hash_distance(hashes, parse_hashes(o["phash"])) <= cfg["dedup"]["max_distance"]),
                None)
            if dup:
                path.unlink(missing_ok=True)
                state, fields = "duplicate", {"reason": f"dup_of:{dup}"}
            else:
                state, fields = "ready", {"file_path": str(path), "phash": " ".join(f"{h:016x}" for h in hashes)}
        counts[state] = counts.get(state, 0) + 1
        with con:
            con.execute(f"UPDATE sources SET state = ?, {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (state, *fields.values(), s["id"]))
    log.info("download: %s", counts)
    return counts
