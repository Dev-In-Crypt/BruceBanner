"""clipping.net source (docs/CLIPPING_PLAN.md): Kick clips of the campaign streamers -> kick_clips.

Kick's public web API gives a streamer's top clips with views, duration, title and category, free and without a
login. Everything is decided at fetch time (the data is complete), then `download` fetches the HLS stream, checks the
speech language and topics, and drops re-uploads of the same moment (perceptual hash).
"""
import hashlib
import logging
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from clips import db, trends, video

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def campaign(cfg, name) -> dict:
    for c in cfg["clipping"]["campaigns"]:
        if c["name"] == name:
            return c
    raise SystemExit(f"campaign {name!r} not in clipping.campaigns")


def streamer_clips(cfg, slug) -> list[dict]:
    k = cfg["clipping"]
    r = requests.get(f"{k['kick_api']}/channels/{slug}/clips", timeout=30,
                     params={"sort": "view", "time": k["kick_period"]},
                     headers={"User-Agent": UA, "Accept": "application/json"})
    r.raise_for_status()
    return r.json().get("clips") or []


def blocked_reason(text: str, category: str, cfg) -> str | None:
    """Gambling, drugs, violence etc.: TikTok and YouTube cut it, clipping.net briefs forbid it."""
    k = cfg["clipping"]
    if category in k["blocked_categories"]:
        return f"category:{category}"
    t = (text or "").lower()
    return next((f"blocked_keyword:{p}" for p in k["blocked_patterns"] if re.search(p, t)), None)


def hook_of(title: str | None) -> str:
    """The clip title as the hook when it reads like a sentence (Kick titles are often 'asdfgh'), else ''."""
    t = re.sub(r"[^\w\s'?!.,:&$%#@-]", "", title or "").strip()  # emoji: the font has none
    words = re.findall(r"[A-Za-z]{2,}", t)
    real = [w for w in words if re.search(r"[aeiouy]", w, re.I) and not re.search(r"[^aeiouy]{5}", w, re.I)]
    return t[:90] if len(real) >= 3 and len(real) >= len(words) * 0.7 else ""


def decide(c: dict, cfg, now) -> tuple[str, str | None]:
    k = cfg["clipping"]
    reason = blocked_reason(c.get("title"), (c.get("category") or {}).get("slug", ""), cfg)
    age_days = (now - datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).total_seconds() / 86400
    if reason:
        return "rejected", reason
    if c.get("is_mature"):
        return "rejected", "mature"
    if not k["min_duration_sec"] <= (c.get("duration") or 0) <= k["max_duration_sec"]:
        return "rejected", "duration"
    if age_days > k["max_age_days"]:
        return "rejected", "too_old"
    if (c.get("views") or 0) < k["min_views"]:
        return "rejected", "low_views"
    return "selected", None


def fetch(cfg, con, dry, now=None) -> dict:
    now, counts = now or datetime.now(timezone.utc), {}
    for camp in [c for c in cfg["clipping"]["campaigns"] if c.get("source", "kick") == "kick"]:
        for slug in camp.get("streamers", []):
            try:
                clips = streamer_clips(cfg, slug)[: cfg["clipping"]["clips_per_streamer"]]
            except Exception as e:  # noqa: BLE001
                log.warning("kick: %s skipped: %s", slug, e)
                continue
            for c in clips:
                state, reason = decide(c, cfg, now)
                counts[state] = counts.get(state, 0) + 1
                if dry:
                    continue
                with con:  # views update; a decided clip keeps its state unless it only lacked views
                    con.execute(
                        "INSERT INTO kick_clips (id, campaign, streamer, title, category, views, duration, created_at,"
                        " video_url, fetched_at, state, reason, score, hook) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(id) DO UPDATE SET views = excluded.views, fetched_at = excluded.fetched_at,"
                        " score = excluded.score, state = CASE WHEN kick_clips.reason = 'low_views'"
                        " THEN excluded.state ELSE kick_clips.state END, reason = CASE WHEN"
                        " kick_clips.reason = 'low_views' THEN excluded.reason ELSE kick_clips.reason END",
                        (c["id"], camp["name"], slug.lower(), c.get("title"), (c.get("category") or {}).get("slug"),
                         c.get("views"), c.get("duration"), c["created_at"], c["video_url"], db.now_iso(), state,
                         reason, c.get("views"), hook_of(c.get("title"))))
    log.info("kick fetch: %s%s", counts, " (dry-run)" if dry else "")
    return counts


def add(cfg, con, src: str, camp: str, streamer: str, hook: str = "") -> str:
    """Manual source: a local file (campaign Assets) or any link yt-dlp can read."""
    campaign(cfg, camp)
    is_file = Path(src).is_file()
    cid = f"file:{Path(src).name}" if is_file else "url:" + hashlib.sha1(src.encode()).hexdigest()[:16]
    with con:
        con.execute("INSERT INTO kick_clips (id, campaign, streamer, video_url, fetched_at, state, hook, score)"
                    " VALUES (?,?,?,?,?, 'selected', ?, 0) ON CONFLICT(id) DO NOTHING",
                    (cid, camp, streamer.lower(), str(Path(src).resolve()) if is_file else src, db.now_iso(), hook))
    return cid


def download_one(cfg, clip) -> Path:
    path = cfg["data"] / "sources" / f"{re.sub(r'[^\w.-]', '_', clip['id'])}.mp4"
    part = path.with_name(path.stem + ".part.mp4")
    url = clip["video_url"]
    if Path(url).is_file():
        shutil.copy(url, part)
    elif ".m3u8" in url:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-user_agent", UA, "-i", url, "-c", "copy",
                        "-bsf:a", "aac_adtstoasc", str(part)], check=True, capture_output=True, timeout=300)
    else:
        subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-f", "mp4/best", "-o", str(part), url],
                       check=True, capture_output=True, timeout=300)
    p = video.probe(part)
    if not p["video"] or p["duration"] <= 0:
        part.unlink(missing_ok=True)
        raise ValueError("downloaded file has no video")
    part.replace(path)
    return path


def download(cfg, con, dry) -> dict:
    k, counts = cfg["clipping"], {}
    for clip in con.execute("SELECT * FROM kick_clips WHERE state = 'selected' ORDER BY score DESC").fetchall():
        if dry:
            log.info("dry-run: would download %s", clip["id"])
            continue
        try:
            path = download_one(cfg, clip)
            duration = video.probe(path)["duration"]
            f = {**cfg["filter"], "speech_langs": [campaign(cfg, clip["campaign"])["language"]]}
            lang, said = trends.speech(path, f) if k["speech_check"] else (None, "")
            topic = blocked_reason(said, "", cfg)
            hashes = video.video_hashes(path, cfg["dedup"]["frames"])
        except Exception as e:  # noqa: BLE001
            log.warning("kick clip %s failed: %s", clip["id"], e)
            state, fields = "failed", {"reason": str(e)[:300]}
        else:
            dup = next((o["id"] for o in con.execute(
                "SELECT id, phash FROM kick_clips WHERE phash IS NOT NULL AND state = 'ready' AND id != ?",
                (clip["id"],)) if video.hash_distance(hashes, trends.parse_hashes(o["phash"]))
                <= cfg["dedup"]["max_distance"]), None)  # ponytail: linear scan, fine for thousands of clips
            if not k["min_duration_sec"] <= duration <= k["max_duration_sec"]:
                state, fields = "rejected", {"reason": "duration", "duration": duration}
            elif lang or topic:
                state, fields = "rejected", {"reason": f"speech_{lang or topic}"}
            elif dup:
                state, fields = "duplicate", {"reason": f"dup_of:{dup}"}
            else:
                state, fields = "ready", {"file_path": str(path), "duration": duration,
                                          "phash": " ".join(f"{h:016x}" for h in hashes)}
            if state != "ready":
                path.unlink(missing_ok=True)
        counts[state] = counts.get(state, 0) + 1
        with con:
            con.execute(f"UPDATE kick_clips SET state = ?, {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                        (state, *fields.values(), clip["id"]))
    log.info("kick download: %s", counts)
    return counts
