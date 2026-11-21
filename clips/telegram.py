"""Operator bot (spec module 14): alerts out, button answers in. Plain Bot API over requests.

Submitting reels to the Playerok bot stays manual (H3).
"""
import html
import json
import logging
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from clips import config, db

log = logging.getLogger(__name__)
REASON_RU = {"stalled": "застой", "age": "19-й день", "account_in_danger": "аккаунт в опасности"}
STATUS_RU = {"paused": "ограничение действий, пауза", "check": "проверка", "banned": "бан"}
HELP = ("Кнопки под сообщениями: Подано / Одобрено / Отклонено / Проверку прошёл.\n"
        "/status: сводка\n/geo <номер> <процент>: доля просмотров из России\n"
        "/bound <ник>: аккаунт привязан в боте Playerok\n"
        "/limit <остаток> <всего>: лимит автора из раздела «Мои лимиты»")


class TelegramError(Exception):
    """A Bot API failure with the token already removed from the message."""


def _api(method, **kw):
    token = config.secret("TELEGRAM_BOT_TOKEN")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60, **kw)
        r.raise_for_status()
        return r.json()["result"]
    except (requests.RequestException, ValueError, KeyError) as e:
        # requests puts the URL, and so the token, into its messages: never let it reach a log
        raise TelegramError(f"{method}: {type(e).__name__}: {str(e).replace(token, '***')}") from None


def operators() -> list[int]:
    return [int(x) for x in os.environ.get("TELEGRAM_OPERATOR_IDS", "").split(",") if x.strip()]


def send(text: str, buttons=None, photo: Path | None = None, dry=False) -> bool:
    """buttons: list of rows, each row a list of (label, callback_data). Never raises.
    Returns True if at least one operator received it (always False in dry-run)."""
    markup = {"inline_keyboard": [[{"text": lbl, "callback_data": d} for lbl, d in row]
                                  for row in buttons]} if buttons else None
    if dry:
        print(f"[telegram dry-run] {text} {buttons or ''} {photo or ''}")
        return False
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not operators():
        log.warning("ALERT (Telegram not configured): %s", text)
        return False
    delivered = False
    for chat in operators():
        try:
            if photo and Path(photo).exists():
                with open(photo, "rb") as f:
                    _api("sendPhoto", data={"chat_id": chat, "caption": text[:1024], "parse_mode": "HTML",
                                            **({"reply_markup": json.dumps(markup)} if markup else {})},
                         files={"photo": f})
            else:
                _api("sendMessage", json={"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                                          "disable_web_page_preview": True,
                                          **({"reply_markup": markup} if markup else {})})
            delivered = True
        except Exception as e:  # noqa: BLE001  (an alert failure must not stop the pipeline)
            log.warning("telegram send to %s failed: %s; text: %s", chat, e, text)
    return delivered


# ---------- message builders (pure) ----------

def num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def msg_submit(pub: dict, cfg, warnings=()) -> tuple[str, list]:
    from clips.submit import payout_rub
    p, tz = cfg["playerok"], ZoneInfo(cfg["timezone"])
    published = db.parse_iso(pub["published_at"]).astimezone(tz)
    growth = pub.get("growth_48h_pct")
    lines = [
        f"🟢 Пора подавать: pub#{pub['id']}",
        f"@{html.escape(pub['account'])} · {html.escape(pub['ig_url'] or '')}",
        f"Просмотры: {num(pub['last_views'])}" + (f" (рост за 48 ч: {growth:.1f}%)" if growth is not None else ""),
        f"Причина: {REASON_RU.get(pub['submit_reason'], pub['submit_reason'] or '')}",
        f"Баннер: {'с озвучкой' if pub['banner_type'] == 'voiced' else 'обычный'}",
        f"Ожидаемая сумма: {num(payout_rub(pub['last_views'], pub['banner_type'] == 'voiced', p))} ₽",
        f"Подать до: {published + timedelta(days=p['submit_by_day']):%d.%m} "
        f"(дедлайн {published + timedelta(days=p['deadline_days']):%d.%m})",
    ]
    if pub.get("needs_screen_recording"):
        lines += ["⚠️ Нужна запись экрана статистики с телефона",
                  f"Запишите долю просмотров из России: /geo {pub['id']} &lt;процент&gt;"]
    lines += [html.escape(w) for w in warnings]
    buttons = [[("Подано", f"sub:{pub['id']}")], [("Одобрено", f"ok:{pub['id']}"), ("Отклонено", f"rej:{pub['id']}")]]
    return "\n".join(lines), buttons


def msg_account(nick, status, reason_ru) -> tuple[str, list | None]:
    text = f"🔴 Аккаунт @{html.escape(nick)}: {html.escape(reason_ru)}. Публикации остановлены."
    return text, [[("Проверку прошёл", f"acc:{nick}")]] if status == "check" else None


def msg_error(command, exc) -> str:
    return f"⚠️ Ошибка в команде {html.escape(command)}: {type(exc).__name__}: {html.escape(str(exc)[:300])}"


# ---------- bot (button answers) ----------

def _pub_exists(con, pid) -> bool:
    return con.execute("SELECT 1 FROM publications WHERE id = ?", (pid,)).fetchone() is not None


def handle_update(con, cfg, update, pending: dict) -> list[tuple[int, str]]:
    cq, msg = update.get("callback_query"), update.get("message")
    sender = (cq or msg or {}).get("from", {}).get("id")
    if sender not in operators():
        log.warning("update from non-operator %s ignored", sender)
        return []
    chat = (cq["message"]["chat"]["id"] if cq else msg["chat"]["id"])
    if cq:
        kind, _, arg = (cq.get("data") or "").partition(":")
        if kind == "acc":
            db.set_account_status(con, arg, "active")
            return [(chat, f"Аккаунт @{arg} снова активен")]
        pid = int(arg) if arg.isdigit() else None
        if pid is None or not _pub_exists(con, pid):
            return [(chat, "Публикация не найдена")]
        if kind == "sub":
            with con:
                con.execute("UPDATE publications SET submit_status = 'submitted', submitted_at = COALESCE("
                            "submitted_at, ?), submitted_views = COALESCE(submitted_views, last_views) WHERE id = ?",
                            (db.now_iso(), pid))
            views = con.execute("SELECT submitted_views FROM publications WHERE id = ?", (pid,)).fetchone()[0]
            return [(chat, f"Отмечено: pub#{pid} подан ({num(views or 0)} просмотров)")]
        if kind in ("ok", "rej"):
            pending[chat] = (kind, pid)
            return [(chat, f"Введите сумму в рублях для pub#{pid}" if kind == "ok"
                     else f"Введите причину отказа для pub#{pid}")]
        return [(chat, "Неизвестная кнопка")]
    text = (msg.get("text") or "").strip()
    if text.startswith("/status"):
        from clips import reports
        return [(chat, reports.summary_text(con, cfg))]
    if text.startswith("/geo"):
        m = re.fullmatch(r"/geo\s+(\d+)\s+(\d{1,3})\s*%?", text)
        if not m or int(m[2]) > 100 or not _pub_exists(con, int(m[1])):
            return [(chat, "Формат: /geo <номер публикации> <процент от 0 до 100>")]
        with con:
            con.execute("UPDATE publications SET ru_audience_pct = ? WHERE id = ?", (int(m[2]), int(m[1])))
        return [(chat, f"pub#{m[1]}: Россия {m[2]}%")]
    if text.startswith("/bound"):
        m = re.fullmatch(r"/bound\s+@?([\w.]+)", text)
        nicks = {a["nick"] for a in cfg["accounts"]}
        if not m or m[1] not in nicks:
            return [(chat, "Формат: /bound <ник аккаунта из конфига>")]
        with con:
            con.execute("UPDATE accounts SET bound_at = ?, updated_at = ? WHERE nick = ?",
                        (db.now_iso(), db.now_iso(), m[1]))
        return [(chat, f"@{m[1]}: привязан к Playerok")]
    if text.startswith("/limit"):
        m = re.fullmatch(r"/limit\s+(\d+)\s+(\d+)", text)
        if not m or int(m[1]) > int(m[2]) or int(m[2]) == 0:
            return [(chat, "Формат: /limit <остаток> <всего>, остаток не больше общего лимита")]
        from clips import submit
        submit.set_author_limit(con, int(m[2]), int(m[1]))
        return [(chat, submit.limit_text(con))] + \
            [(chat, t) for t in submit.limit_alerts(con, cfg["playerok"]["limit_alert_used_pct"])]
    if chat in pending:
        kind, pid = pending[chat]
        if kind == "ok":
            digits = re.sub(r"[\s₽руб.]", "", text)
            if not digits.isdigit() or int(digits) > cfg["playerok"]["max_payout_rub"]:
                return [(chat, f"Нужна сумма числом от 0 до {cfg['playerok']['max_payout_rub']}")]
            with con:
                con.execute("UPDATE publications SET submit_status = 'approved', amount_rub = ?, decided_at = ?"
                            " WHERE id = ?", (int(digits), db.now_iso(), pid))
            del pending[chat]
            return [(chat, f"pub#{pid}: одобрено, {num(digits)} ₽")]
        with con:
            con.execute("UPDATE publications SET submit_status = 'rejected', reject_reason = ?, decided_at = ?"
                        " WHERE id = ?", (text[:500], db.now_iso(), pid))
        del pending[chat]
        return [(chat, f"pub#{pid}: отклонено")]
    return [(chat, HELP)]


MAX_TRIES = 3  # a press that keeps failing is skipped after this many tries, so the bot never gets stuck


def poll_once(con, cfg, offset, pending, fails) -> int:
    """One getUpdates round. Returns the next offset; a failed update keeps it, so Telegram redelivers it."""
    try:
        updates = _api("getUpdates", json={"offset": offset, "timeout": 50,
                                           "allowed_updates": ["message", "callback_query"]})
    except TelegramError as e:
        log.warning("getUpdates failed: %s", e)
        time.sleep(5)
        return offset
    for u in updates:
        uid = u["update_id"]
        try:
            for chat, text in handle_update(con, cfg, u, pending):
                _api("sendMessage", json={"chat_id": chat, "text": text})
            if "callback_query" in u:
                _api("answerCallbackQuery", json={"callback_query_id": u["callback_query"]["id"]})
        except Exception:  # noqa: BLE001
            fails[uid] = fails.get(uid, 0) + 1
            log.exception("update %s failed (try %s of %s)", uid, fails[uid], MAX_TRIES)
            if fails[uid] < MAX_TRIES:
                time.sleep(5)
                return uid  # not acknowledged: the next getUpdates returns it again
            send(f"⚠️ Бот не смог обработать действие (update {uid}). Повторите нажатие.")
        fails.pop(uid, None)
        offset = uid + 1
    return offset


def run_bot(cfg, con):
    offset, pending, fails = 0, {}, {}
    # ponytail: pending answers live in memory; after a bot restart the operator presses the button again
    while True:
        offset = poll_once(con, cfg, offset, pending, fails)
