"""Posting queue (spec module 9): sources -> publications on accounts at random times, then build."""
import logging
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from clips import captions, config, db, telegram, trends, video

log = logging.getLogger(__name__)


class NoCaption(Exception):
    pass


def hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def pick_slot(now_utc, day_local, existing: list[datetime], p, tz, rng) -> datetime | None:
    """Random minute inside today's window, at least min_gap away from this account's other posts."""
    start = datetime.combine(day_local, hm(p["window_start"]), tz)
    end = datetime.combine(day_local, hm(p["window_end"]), tz)
    start = max(start, now_utc.astimezone(tz) + timedelta(minutes=p["lead_minutes"]))
    if start >= end:
        return None
    span = int((end - start).total_seconds() // 60)
    gap = timedelta(minutes=p["min_gap_minutes"])
    for _ in range(50):
        t = start + timedelta(minutes=rng.randint(0, span))
        if all(abs(t - e) >= gap for e in existing):
            return t.astimezone(timezone.utc)
    return None


def day_bounds(now_utc, tz) -> tuple[str, str]:
    day = now_utc.astimezone(tz).date()
    start = datetime.combine(day, dtime(0), tz)
    return db.iso(start), db.iso(start + timedelta(days=1))


# A failed publication frees its source, so the next attempt number must count failed rows too
# (UNIQUE(source_id, attempt) would otherwise reject the retry forever).
MAX_ATTEMPT = "COALESCE((SELECT MAX(x.attempt) FROM publications x WHERE x.source_id = s.id), 0) AS max_attempt"


def fresh_sources(con, cfg, now) -> list:
    min_posted = db.iso(now - timedelta(hours=cfg["planner"]["max_source_age_hours"]))
    return con.execute(
        "SELECT s.*, " + MAX_ATTEMPT + " FROM sources s WHERE s.state = 'ready' AND s.posted_at >= ? AND NOT EXISTS "
        "(SELECT 1 FROM publications p WHERE p.source_id = s.id AND p.state != 'failed') ORDER BY s.score DESC",
        # ponytail: one query for all groups, split in Python; fine at a few hundred ready sources
        (min_posted,)).fetchall()


def repost_source(con, cfg, nick, now, group="memes"):
    """A flop (every post under repost_max_views) that may get another chance on a new account."""
    p = cfg["planner"]
    last_ok = db.iso(now - timedelta(days=max(p["repost_after_days"], p["cross_account_gap_days"])))
    return con.execute(
        "SELECT s.*, " + MAX_ATTEMPT + " FROM sources s "
        "JOIN publications p ON p.source_id = s.id AND p.state != 'failed' WHERE s.state = 'ready' "
        "AND s.donor_group = ? "
        "GROUP BY s.id HAVING SUM(p.state != 'published' OR p.last_views IS NULL OR p.last_views >= ?) = 0 "
        "AND SUM(p.account = ?) = 0 AND COUNT(*) < ? AND MAX(p.published_at) <= ? ORDER BY s.score DESC LIMIT 1",
        (group, p["repost_max_views"], nick, 1 + p["max_reposts"], last_ok)).fetchone()


def plan(cfg, con, dry, now=None, rng=None) -> list[dict]:
    now, rng = now or datetime.now(timezone.utc), rng or random.Random()
    tz, p = ZoneInfo(cfg["timezone"]), cfg["planner"]
    if not dry:
        db.sync_accounts(con, cfg)
        for nick in db.resume_paused_accounts(con):
            telegram.send(f"Аккаунт @{nick} снова активен")
        expire_stale(cfg, con, now)
    lo, hi = day_bounds(now, tz)
    fresh, planned = {}, []  # donor group -> ready sources, best first (addendum 2b)
    for s in fresh_sources(con, cfg, now):
        fresh.setdefault(s["donor_group"], []).append(s)
    accounts = [a for a in cfg["accounts"] if instagram_status(con, a["nick"]) == "active"]
    rng.shuffle(accounts)  # the best sources should not always go to the same account
    for a in accounts:
        rows = con.execute("SELECT scheduled_at FROM publications WHERE account = ? AND state != 'failed' "
                           "AND scheduled_at >= ? AND scheduled_at < ?", (a["nick"], lo, hi)).fetchall()
        existing = [db.parse_iso(r[0]) for r in rows]
        for _ in range(a["daily_limit"] - len(existing)):
            group = a.get("donor_group", "memes")
            src = fresh[group].pop(0) if fresh.get(group) else repost_source(con, cfg, a["nick"], now, group)
            slot = src and pick_slot(now, now.astimezone(tz).date(), existing, p, tz, rng)
            if not slot:
                break
            attempt = src["max_attempt"] + 1
            item = {"account": a["nick"], "local_time": slot.astimezone(tz).strftime("%H:%M"),
                    "source": src["shortcode"], "score": round(src["score"] or 0, 1), "attempt": attempt}
            if not dry:
                try:
                    item["pub_id"] = insert_publication(con, a, src["id"], attempt, slot)
                except NoCaption:
                    log.warning("no free %s caption: slot skipped for @%s", a["caption_lang"], a["nick"])
                    break
                except sqlite3.IntegrityError:
                    log.info("source %s attempt %s already planned", src["shortcode"], attempt)
                    continue
            existing.append(slot)
            planned.append(item)
    for it in planned:
        print(f"{it['account']:20} {it['local_time']}  {it['source']:14} score={it['score']} attempt={it['attempt']}")
    if not dry:
        build_planned(cfg, con)
    return planned


def instagram_status(con, nick) -> str:
    row = con.execute("SELECT status FROM accounts WHERE nick = ?", (nick,)).fetchone()
    return row["status"] if row else "active"


def needs_bind_post(con, account) -> bool:
    """Addendum 3: an unbound account with a code from the Playerok bot posts one reel with that code."""
    code = account.get("bind_code")
    if not code:
        return False
    row = con.execute("SELECT bound_at FROM accounts WHERE nick = ?", (account["nick"],)).fetchone()
    if row and row["bound_at"]:
        return False
    return con.execute("SELECT 1 FROM publications WHERE account = ? AND is_bind = 1 AND caption = ? "
                       "AND state != 'failed'", (account["nick"], code)).fetchone() is None


def insert_publication(con, account, source_id, attempt, slot) -> int:
    """One transaction: reserve a caption and insert the publication (a rollback frees the caption)."""
    with con:
        bind = needs_bind_post(con, account)
        cap = (None, account["bind_code"]) if bind else \
            captions.take_caption(con, account["caption_lang"], "pub:pending", account.get("theme", ""))
        if cap is None:
            raise NoCaption
        cid, text = cap
        pid = con.execute(
            "INSERT INTO publications (source_id, account, attempt, state, scheduled_at, caption, caption_id,"
            " caption_lang, is_bind, created_at) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?)",
            (source_id, account["nick"], attempt, db.iso(slot), text, cid, account["caption_lang"], int(bind),
             db.now_iso())).lastrowid
        if cid:
            con.execute("UPDATE captions SET used_for = ? WHERE id = ?", (f"pub:{pid}", cid))
    return pid


def expire_stale(cfg, con, now):
    cutoff = db.iso(now - timedelta(hours=cfg["publish"]["max_delay_hours"]))
    for r in con.execute("SELECT id, file_path FROM publications WHERE state IN ('planned','built') "
                         "AND scheduled_at < ?", (cutoff,)).fetchall():
        with con:
            con.execute("UPDATE publications SET state = 'failed', error = 'slot_expired' WHERE id = ?", (r["id"],))
        if r["file_path"]:
            Path(r["file_path"]).unlink(missing_ok=True)


def build_planned(cfg, con):
    by_nick = {a["nick"]: a for a in cfg["accounts"]}
    if not cfg["banners"]["top"]:  # addendum 3: the dated banner set ended; planned reels wait for a new one
        waiting = con.execute("SELECT COUNT(*) FROM publications WHERE state = 'planned'").fetchone()[0]
        last = max((st["end"] for st in cfg["banners"]["sets"] if "end" in st), default=None)
        mark, today = cfg["data"] / "tmp" / "no_banner_set_alert", str(config.today(cfg))
        if not mark.exists() or mark.read_text() != today:  # once a day, not on every hourly plan run
            if telegram.send(f"⛔️ Сборка остановлена: набор баннеров Playerok закончился ({last}). "
                             f"Загрузите новый набор в banners.sets. Ждут сборки: {waiting}."):
                mark.write_text(today)
        log.warning("no active banner set, %s planned reels wait", waiting)
        return
    for pub in con.execute("SELECT * FROM publications WHERE state = 'planned' ORDER BY scheduled_at").fetchall():
        account = by_nick.get(pub["account"])
        if not account:
            continue
        src = con.execute("SELECT * FROM sources WHERE id = ?", (pub["source_id"],)).fetchone()
        out = cfg["data"] / "build" / f"pub_{pub['id']}.mp4"
        texts = account.get("overlay_texts") or []
        text = random.choice(texts) if cfg["compose"]["text_overlay"] and texts else None
        try:
            path = src["file_path"]
            if not path or not Path(path).exists():  # deleted by maintenance before a repost
                path = str(trends.download_one(cfg, src))
                with con:
                    con.execute("UPDATE sources SET file_path = ? WHERE id = ?", (path, src["id"]))
            meta = video.build(path, account, cfg, out, text=text)
        except Exception as e:  # noqa: BLE001
            log.exception("build failed pub=%s source=%s", pub["id"], src["shortcode"])
            with con:
                con.execute("UPDATE publications SET state = 'failed', error = ? WHERE id = ?",
                            (f"build: {str(e)[:300]}", pub["id"]))
                con.execute("UPDATE sources SET state = 'failed', reason = ? WHERE id = ?",
                            (f"build: {str(e)[:300]}", src["id"]))
            telegram.send(f"⚠️ Не удалось собрать ролик pub#{pub['id']} ({src['shortcode']}): {str(e)[:200]}")
            continue
        with con:
            con.execute("UPDATE publications SET state = 'built', file_path = ?, background = ?, banner_type = ?,"
                        " banner_file = ?, template = ? WHERE id = ?",
                        (str(out), meta["background"], meta["banner_type"], meta["banner_file"], meta["template"],
                         pub["id"]))
        log.info("built pub=%s %s (%s)", pub["id"], out.name, meta["banner_type"])
