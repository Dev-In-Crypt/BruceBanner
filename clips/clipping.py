"""clipping.net posting (docs/CLIPPING_PLAN.md): ready Kick clips -> clip_posts on TikTok / YouTube accounts.

plan: today's slots (US time window) per clipping account, best ready clip of its campaign that this account has not
posted, clips nobody posted yet first. build: the kick template. publish: shorts.py through AdsPower. After publish
the operator gets the links to submit on clipping.net within the campaign's post window. stats: views via yt-dlp.
"""
import json
import logging
import random
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from clips import db, kick, planner, telegram, video

log = logging.getLogger(__name__)


def accounts(cfg) -> list[dict]:
    return [a for a in cfg["clipping"]["accounts"] if a.get("adspower_id")]


def account(cfg, nick) -> dict:
    for a in cfg["clipping"]["accounts"]:
        if a["nick"] == nick:
            return a
    raise SystemExit(f"clipping account {nick!r} not in config")


def caption(camp: dict, hook: str, streamer: str) -> str:
    text = camp.get("caption", "{hook}").format(hook=hook, streamer=streamer).strip()
    tags = [t for t in camp.get("caption_tags", []) if t.lower() not in text.lower()]
    return " ".join([text, *tags]).strip()


def next_clip(con, camp: str, nick: str):
    """Best ready clip of the campaign this account never had; clips no account posted yet come first."""
    return con.execute(
        "SELECT k.* FROM kick_clips k WHERE k.state = 'ready' AND k.campaign = ? AND COALESCE(k.hook, '') != '' "
        "AND NOT EXISTS (SELECT 1 FROM clip_posts p WHERE p.clip_id = k.id AND p.account = ?) "
        "ORDER BY (SELECT COUNT(*) FROM clip_posts p WHERE p.clip_id = k.id AND p.state != 'failed'), "
        "k.score DESC LIMIT 1", (camp, nick)).fetchone()


def plan(cfg, con, dry, now=None, rng=None) -> list[dict]:
    now, rng = now or datetime.now(timezone.utc), rng or random.Random()
    k = cfg["clipping"]
    tz, out = ZoneInfo(k["timezone"]), []
    lo, hi = planner.day_bounds(now, tz)
    accs = accounts(cfg)
    rng.shuffle(accs)
    for a in accs:
        existing = [db.parse_iso(r[0]) for r in con.execute(
            "SELECT scheduled_at FROM clip_posts WHERE account = ? AND state != 'failed' AND scheduled_at >= ? "
            "AND scheduled_at < ?", (a["nick"], lo, hi))]
        for _ in range(a["daily_limit"] - len(existing)):
            clip = next_clip(con, a["campaign"], a["nick"])
            slot = clip and planner.pick_slot(now, now.astimezone(tz).date(), existing, k, tz, rng)
            if not slot:
                break
            camp = kick.campaign(cfg, a["campaign"])
            item = {"account": a["nick"], "platform": a["platform"], "clip": clip["id"],
                    "local_time": slot.astimezone(tz).strftime("%H:%M"), "hook": clip["hook"]}
            if not dry:
                try:
                    with con:
                        item["post_id"] = con.execute(
                            "INSERT INTO clip_posts (clip_id, account, platform, campaign, scheduled_at, caption,"
                            " created_at) VALUES (?,?,?,?,?,?,?)",
                            (clip["id"], a["nick"], a["platform"], a["campaign"], db.iso(slot),
                             caption(camp, clip["hook"], clip["streamer"]), db.now_iso())).lastrowid
                except sqlite3.IntegrityError:  # this account already had the clip (a failed post keeps its row)
                    continue
            existing.append(slot)
            out.append(item)
    for it in out:
        print(f"{it['account']:20} {it['platform']:8} {it['local_time']}  {it['clip']}  {it['hook']}")
    if not dry:
        build_planned(cfg, con)
    return out


def build_planned(cfg, con) -> dict:
    counts = {}
    for p in con.execute("SELECT p.*, k.file_path AS src, k.hook, k.streamer FROM clip_posts p "
                         "JOIN kick_clips k ON k.id = p.clip_id WHERE p.state = 'planned'").fetchall():
        out = cfg["data"] / "build" / f"clip_{p['id']}_{p['account']}.mp4"
        try:
            video.build_clip(p["src"], p["hook"], p["streamer"], cfg, out, seed=f"{p['account']}:{p['clip_id']}",
                             bar=kick.campaign(cfg, p["campaign"]).get("bar", "kick"))
            state, fields = "built", {"file_path": str(out)}
        except Exception as e:  # noqa: BLE001
            log.warning("clip post %s build failed: %s", p["id"], e)
            state, fields = "failed", {"error": str(e)[:300]}
        counts[state] = counts.get(state, 0) + 1
        with con:
            con.execute(f"UPDATE clip_posts SET state = ?, {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                        (state, *fields.values(), p["id"]))
    log.info("clip build: %s", counts)
    return counts


def set_hook(con, clip_id, hook) -> None:
    """Operator's own hook for a clip (Kick titles are often junk); rebuilds nothing already built."""
    with con:
        if not con.execute("UPDATE kick_clips SET hook = ? WHERE id = ?", (hook.strip(), clip_id)).rowcount:
            raise SystemExit(f"clip {clip_id!r} not found")


def notify(cfg, con, dry, now=None) -> int:
    """Links the operator still has to submit on clipping.net, grouped by campaign, with the window deadline."""
    now = now or datetime.now(timezone.utc)
    rows = con.execute("SELECT * FROM clip_posts WHERE state = 'published' AND notified_at IS NULL "
                       "ORDER BY campaign, published_at").fetchall()
    by_camp = {}
    for r in rows:
        by_camp.setdefault(r["campaign"], []).append(r)
    for camp, posts in by_camp.items():
        hours = kick.campaign(cfg, camp)["post_window_hours"]
        for i in range(0, len(posts), 25):  # clipping.net takes up to 25 links at once
            chunk = posts[i:i + 25]
            deadline = min(db.parse_iso(p["published_at"]) for p in chunk) + timedelta(hours=hours)
            left = max(0, int((deadline - now).total_seconds() // 3600))
            text = (f"clipping.net, кампания {camp}: сабмитни {len(chunk)} ссылок (осталось ~{left} ч)\n"
                    + "\n".join(p["post_url"] for p in chunk))
            if telegram.send(text, dry=dry) and not dry:
                with con:
                    con.executemany("UPDATE clip_posts SET notified_at = ? WHERE id = ?",
                                    [(db.now_iso(), p["id"]) for p in chunk])
    return len(rows)


def views_of(url: str) -> int | None:
    """Public view count via yt-dlp (no login). None: the post is gone, private or unreadable."""
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--skip-download", "--dump-json", url],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        log.info("views of %s: %s", url, (r.stderr or "").strip()[-200:])
        return None
    return json.loads(r.stdout).get("view_count")


def stats(cfg, con, dry, days=30) -> dict:
    """Views of our posts; an alert when one disappears (clipping.net does not pay for deleted or private posts)."""
    since = db.iso(datetime.now(timezone.utc) - timedelta(days=days))
    counts, gone = {"ok": 0, "gone": 0}, []
    for p in con.execute("SELECT * FROM clip_posts WHERE state = 'published' AND published_at >= ?", (since,)):
        v = views_of(p["post_url"])
        if v is None:
            counts["gone"] += 1
            gone.append(p["post_url"])
            continue
        counts["ok"] += 1
        if not dry:
            with con:
                con.execute("UPDATE clip_posts SET last_views = ?, views_at = ? WHERE id = ?", (v, db.now_iso(), p["id"]))
    if gone:
        telegram.send("Не читаются (удалены, приватные или сбой), clipping.net их не оплатит:\n" + "\n".join(gone),
                      dry=dry)
    log.info("clip stats: %s", counts)
    return counts

