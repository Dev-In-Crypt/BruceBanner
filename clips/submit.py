"""Daily stats (spec module 12) and the "time to submit to Playerok" logic (module 13).

Submitting to the Playerok bot is done by a human (H3); this module only decides when and tells them.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clips import db, telegram, trends
from clips.instagram import squash

log = logging.getLogger(__name__)


# ---------- pure logic ----------

def payout_rub(views: int, voiced: bool, p: dict) -> int:
    if views < p["min_paid_views"]:
        return 0
    paid = views // p["step_views"] * p["step_views"]
    rate = p["rate_voiced_rub_per_100k"] if voiced else p["rate_static_rub_per_100k"]
    return min(paid * rate // 100_000, p["max_payout_rub"])


def growth_pct(views_now: int, views_then: int | None) -> float | None:
    if views_then is None or views_then <= 0:
        return None
    return (views_now - views_then) / views_then * 100


def decide(views, age_days, views_48h_ago, account_status, p) -> tuple[str, str | None]:
    """Return (submit_status, reason) for a published reel that is not yet submitted."""
    if age_days > p["deadline_days"]:
        return ("expired" if views >= p["min_paid_views"] else "low"), None
    if views < p["min_paid_views"]:
        return "low", None
    if account_status != "active":
        return "submit_now", "account_in_danger"
    if age_days >= p["submit_by_day"]:
        return "submit_now", "age"
    g = growth_pct(views, views_48h_ago)
    if g is None or g >= p["stall_growth_pct_48h"]:
        return "candidate", None
    # Stalled. Wait only if the same absolute growth over the next 48 h crosses the next paid step
    # and we still have 2 days before the submit-by day.
    if p["wait_for_next_step"] and age_days + 2 <= p["submit_by_day"]:
        next_step = (views // p["step_views"] + 1) * p["step_views"]
        if views + (views - views_48h_ago) >= next_step:
            return "candidate", "waiting_next_step"
    return "submit_now", "stalled"


def views_48h_ago(con, pub_id, now) -> int | None:
    # 48 h +/- 3 h: the daily job does not run at exactly the same second every day. Older snapshots
    # (a missed run) would turn multi-day growth into "48 h growth", so they count as no baseline.
    row = con.execute("SELECT views FROM view_snapshots WHERE publication_id = ? AND taken_at <= ? "
                      "AND taken_at >= ? ORDER BY taken_at DESC LIMIT 1",
                      (pub_id, db.iso(now - timedelta(hours=45)), db.iso(now - timedelta(hours=51)))).fetchone()
    return row[0] if row else None


# ---------- stats collection (module 12) ----------

def collect(cfg, con, dry, from_file=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    p = cfg["playerok"]
    since = db.iso(now - timedelta(days=p["deadline_days"] + 1))
    nicks = [r[0] for r in con.execute(
        "SELECT DISTINCT p.account FROM publications p JOIN accounts a ON a.nick = p.account "
        "WHERE a.status != 'banned' AND p.state IN ('published','unconfirmed') AND p.created_at >= ?", (since,))]
    if from_file:
        items = json.loads(Path(from_file).read_text(encoding="utf-8"))
    elif nicks:
        items = trends.run_actor(cfg, {"username": nicks, "resultsLimit": cfg["apify"]["stats_results_per_account"],
                                       "onlyPostsNewerThan": f"{p['deadline_days'] + 1} days",
                                       "skipPinnedPosts": False})
        trends.dump_raw(cfg, "stats", items)
    else:
        items = []
    taken, seen, n = db.iso(now), set(), 0
    by_code = {r["ig_shortcode"]: r for r in con.execute(
        "SELECT id, ig_shortcode FROM publications WHERE state = 'published' AND ig_shortcode IS NOT NULL")}
    for i in items:
        code, views = i.get("shortCode"), trends.views_of(i, cfg)
        if code in by_code and views is not None:
            seen.add(code)
            n += 1
            if not dry:
                with con:
                    con.execute("INSERT OR IGNORE INTO view_snapshots (publication_id, taken_at, views, likes)"
                                " VALUES (?, ?, ?, ?)", (by_code[code]["id"], taken, views, i.get("likesCount")))
                    con.execute("UPDATE publications SET last_views = ? WHERE id = ?", (views, by_code[code]["id"]))
    resolved = resolve_unconfirmed(cfg, con, items, dry, now)
    report_missing(con, cfg, seen, dry, now)
    log.info("stats: %s items, %s snapshots, %s unconfirmed resolved%s", len(items), n, resolved,
             " (dry-run)" if dry else "")
    return {"items": len(items), "snapshots": n, "resolved": resolved}


MATCH_WINDOW = timedelta(hours=1)  # upload + processing time after the Share click


def resolve_unconfirmed(cfg, con, items, dry, now) -> int:
    known = {r[0] for r in con.execute("SELECT ig_shortcode FROM publications WHERE ig_shortcode IS NOT NULL")}
    resolved = 0
    for pub in con.execute("SELECT * FROM publications WHERE state = 'unconfirmed'").fetchall():
        clicked = db.parse_iso(pub["share_clicked_at"]) if pub["share_clicked_at"] else None
        if not clicked:
            continue  # nothing to anchor the match to
        want = squash(pub["caption"])[:30]
        # Only reels posted right around our Share click; with no caption to compare, take the closest one.
        candidates = sorted((
            (abs(datetime.fromisoformat(i["timestamp"]) - clicked), i) for i in items
            if i.get("shortCode") and i["shortCode"] not in known and i.get("timestamp")
            and (i.get("ownerUsername") or "").lower() == pub["account"].lower()
            and clicked - timedelta(minutes=10) <= datetime.fromisoformat(i["timestamp"]) <= clicked + MATCH_WINDOW
            and (not want or squash(i.get("caption")).startswith(want))), key=lambda c: c[0])
        for _, i in candidates[:1]:
            code, posted = i["shortCode"], i["timestamp"]
            known.add(code)
            resolved += 1
            if not dry:
                with con:
                    con.execute("UPDATE publications SET state = 'published', published_at = ?, ig_shortcode = ?,"
                                " ig_url = ?, error = NULL WHERE id = ?",
                                (trends.ts(posted), code, f"https://www.instagram.com/reel/{code}/", pub["id"]))
            break
        else:
            if clicked and now - clicked > timedelta(hours=48) and pub["error"] != "unconfirmed_alerted":
                telegram.send(f"Не найден рилс pub#{pub['id']} @{pub['account']}, проверьте вручную", dry=dry)
                if not dry:
                    with con:
                        con.execute("UPDATE publications SET error = 'unconfirmed_alerted' WHERE id = ?", (pub["id"],))
    return resolved


def report_missing(con, cfg, seen, dry, now):
    since = db.iso(now - timedelta(days=cfg["playerok"]["deadline_days"]))
    for pub in con.execute("SELECT * FROM publications WHERE state = 'published' AND published_at >= ?",
                           (since,)).fetchall():
        if pub["ig_shortcode"] in seen:
            continue
        log.warning("pub=%s %s not in today's stats", pub["id"], pub["ig_url"])
        if pub["submit_status"] in ("candidate", "submit_now") and pub["error"] != "missing_in_stats":
            telegram.send(f"⚠️ Рилс pub#{pub['id']} @{pub['account']} не найден в статистике "
                          f"(возможно, удалён Instagram): {pub['ig_url']}", dry=dry)
            if not dry:
                with con:
                    con.execute("UPDATE publications SET error = 'missing_in_stats' WHERE id = ?", (pub["id"],))


# ---------- submit decision (module 13) ----------

def run_submit_check(cfg, con, dry, now=None, nick=None) -> list:
    now, p = now or datetime.now(timezone.utc), cfg["playerok"]
    rows = con.execute(
        "SELECT p.*, COALESCE(a.status, 'active') AS account_status FROM publications p "
        "LEFT JOIN accounts a ON a.nick = p.account WHERE p.state = 'published' AND p.last_views IS NOT NULL "
        "AND (p.submit_status IS NULL OR p.submit_status IN ('low','candidate')"
        " OR (p.submit_status = 'submit_now' AND p.notified_at IS NULL))"  # retry an undelivered alert
        + (" AND p.account = ?" if nick else ""), (nick,) if nick else ()).fetchall()
    out = []
    for r in rows:
        age = (now - db.parse_iso(r["published_at"])).total_seconds() / 86400
        before = views_48h_ago(con, r["id"], now)
        status, reason = decide(r["last_views"], age, before, r["account_status"], p)
        if r["submit_status"] == "submit_now":  # sticky: only the alert is retried, the decision stays
            status, reason = "submit_now", r["submit_reason"]
        growth = growth_pct(r["last_views"], before)
        rec = int(r["last_views"] >= p["screen_recording_views"])
        out.append((r["id"], status, reason))
        if dry:
            print(f"pub#{r['id']} @{r['account']} views={r['last_views']} age={age:.1f}d -> {status} {reason or ''}")
        else:
            with con:
                con.execute("UPDATE publications SET submit_status = ?, submit_reason = ?, growth_48h_pct = ?,"
                            " needs_screen_recording = ? WHERE id = ?", (status, reason, growth, rec, r["id"]))
        if status == "submit_now" and r["notified_at"] is None:
            pub = {**dict(r), "submit_reason": reason, "growth_48h_pct": growth, "needs_screen_recording": rec}
            text, buttons = telegram.msg_submit(pub, cfg)
            if telegram.send(text, buttons, dry=dry) and not dry:  # mark sent only when delivered
                with con:
                    con.execute("UPDATE publications SET notified_at = ? WHERE id = ?", (db.iso(now), r["id"]))
    return out


def run_stats(cfg, con, dry, from_file=None):
    from clips import notion
    collect(cfg, con, dry, from_file)
    run_submit_check(cfg, con, dry)
    notion.sync(cfg, con, dry)  # skipped when Notion is not configured
