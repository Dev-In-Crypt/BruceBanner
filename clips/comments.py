"""Daily comment moderation under our bannered reels (addendum 3, mandatory by the Playerok rules).

Deletes spam, bait, links and bot comments by the rules in config [comments]. Never hides comments,
never limits or turns commenting off (forbidden by the rules), never touches the post itself (H1):
a menu that offers post actions (edit, archive, ...) is closed at once and the run stops.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from clips import db, instagram, telegram

log = logging.getLogger(__name__)

# Selectors of the comment list on a reel page. Guesses: verify in the dry-run screenshots, fix only here.
LOAD_MORE = ['svg[aria-label="Load more comments"]', 'svg[aria-label="Загрузить другие комментарии"]',
             'button:has-text("View all")', 'button:has-text("Посмотреть все")']
COMMENT_ITEM = 'ul li:has(a[href^="/"]):has(span[dir="auto"])'
COMMENT_AUTHOR = 'a[href^="/"]'
COMMENT_TEXT = 'span[dir="auto"]'
COMMENT_OPTIONS = ['svg[aria-label="Comment options"]', 'svg[aria-label="Параметры комментария"]',
                   'svg[aria-label="More options"]', 'svg[aria-label="Другие параметры"]']
COMMENT_DELETE = ['div[role="dialog"] button:text-is("Delete")', 'div[role="dialog"] button:text-is("Удалить")']
# Items that exist only in the menu of the post itself: seeing one means a wrong menu opened.
POST_MENU_TEXT = ("archive", "edit", "turn off commenting", "hide like count", "архив", "редактировать",
                  "выключить комментарии", "скрыть число")


class WrongMenu(Exception):
    """The options menu of the post opened instead of a comment's: stop, touch nothing."""


def classify(author: str, text: str, cfg) -> str | None:
    """Which rule a comment breaks: 'link' | 'spam' | 'bait' | 'bot', or None to keep it."""
    c, t = cfg["comments"], (text or "").lower().replace("ё", "е")
    if re.search(c["link_pattern"], t, re.I):
        return "link"
    if any(re.search(p, t) for p in c["spam_patterns"]):
        return "spam"
    if any(re.search(p, t) for p in c["bait_patterns"]):
        return "bait"
    if author and re.fullmatch(c["bot_name_pattern"], author, re.I):
        return "bot"
    return None


def read_comments(page, cfg) -> list[tuple[object, str, str]]:
    for _ in range(cfg["comments"]["load_more_clicks"]):
        more = instagram.maybe(page, LOAD_MORE, 3_000)
        if not more:
            break
        more.click()
        page.wait_for_timeout(1500)
    out = []
    items = page.locator(COMMENT_ITEM)
    for i in range(items.count()):
        item = items.nth(i)
        try:
            author = (item.locator(COMMENT_AUTHOR).first.get_attribute("href") or "").strip("/")
            text = item.locator(COMMENT_TEXT).last.inner_text()
        except Exception:  # noqa: BLE001  (a row without author or text: not a comment)
            continue
        out.append((item, author, text))
    return out


def delete_comment(page, item) -> None:
    item.hover()
    opts = item.locator(", ".join(COMMENT_OPTIONS)).first  # the options button inside this comment only
    opts.click()
    menu = instagram.first(page, ['div[role="dialog"]'], 5_000)
    if any(s in menu.inner_text().lower() for s in POST_MENU_TEXT):
        page.keyboard.press("Escape")
        raise WrongMenu("the post menu opened instead of the comment menu")
    instagram.first(page, COMMENT_DELETE, 5_000).click()
    page.wait_for_timeout(1000)


def moderate_account(cfg, con, account, dry, now=None) -> dict:
    nick, c = account["nick"], cfg["comments"]
    now = now or datetime.now(timezone.utc)
    pubs = con.execute("SELECT id, ig_url FROM publications WHERE account = ? AND state = 'published' "
                       "AND ig_url IS NOT NULL AND published_at >= ? ORDER BY published_at DESC",
                       (nick, db.iso(now - timedelta(days=c["days"])))).fetchall()
    stats = {"reels": 0, "matched": 0, "deleted": 0}
    if not pubs or instagram.db_status(con, nick) != "active":  # H5: never open accounts in check/banned
        return stats
    with instagram.browser_lock(cfg), instagram.session(cfg, account) as page:
        page.goto(instagram.IG + "/", wait_until="domcontentloaded")
        instagram.pause(cfg)
        health = instagram.check_health(page)
        if health != "ok":
            instagram.apply_health(con, cfg, nick, health, instagram.save_debug(cfg, page, nick, "health"), dry)
            return stats
        for pub in pubs:
            page.goto(pub["ig_url"], wait_until="domcontentloaded")
            instagram.pause(cfg)
            stats["reels"] += 1
            for item, author, text in read_comments(page, cfg):
                rule = classify(author, text, cfg)
                if not rule:
                    continue
                stats["matched"] += 1
                deleted = None
                if not dry and stats["deleted"] < c["max_deletes_per_run"]:
                    try:
                        delete_comment(page, item)
                        deleted = db.now_iso()
                        stats["deleted"] += 1
                    except WrongMenu:
                        shot = instagram.save_debug(cfg, page, nick, "comments_wrong_menu")
                        telegram.send(f"⚠️ Модерация комментариев @{nick} остановлена: открылось меню поста. "
                                      "Проверьте селекторы.", photo=shot)
                        return stats
                    except Exception as e:  # noqa: BLE001  (one stubborn comment must not stop the run)
                        log.warning("pub=%s comment by %s not deleted: %r", pub["id"], author, e)
                        instagram.save_debug(cfg, page, nick, "comments_delete")
                if not dry:
                    with con:
                        con.execute("INSERT INTO comment_actions (publication_id, author, text, rule, deleted_at,"
                                    " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                    (pub["id"], author, text[:500], rule, deleted, db.now_iso()))
                log.info("pub=%s %s comment by %s: %.60r%s", pub["id"], rule, author, text,
                         "" if deleted else " (not deleted)")
            if not dry:
                with con:
                    con.execute("UPDATE publications SET comments_checked_at = ? WHERE id = ?",
                                (db.now_iso(), pub["id"]))
    return stats


def run_moderation(cfg, con, dry) -> dict:
    out = {}
    for account in cfg["accounts"]:
        try:
            out[account["nick"]] = moderate_account(cfg, con, account, dry)
        except Exception as e:  # noqa: BLE001
            log.exception("comment moderation failed for %s", account["nick"])
            telegram.send(telegram.msg_error(f"moderate-comments @{account['nick']}", e), dry=dry)
    log.info("moderate-comments: %s%s", out, " (dry-run)" if dry else "")
    return out
