"""One-way mirror of the DB into Notion for reports (spec module 15). SQLite stays the source of truth."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from clips import config, db

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
PUB_STATUS = {"low": "Мало", "candidate": "Кандидат", "submit_now": "Пора подавать", "submitted": "Подан",
              "approved": "Одобрен", "rejected": "Отклонён", "expired": "Просрочен"}
ACC_STATUS = {"active": "активен", "paused": "пауза", "check": "проверка", "banned": "бан"}


def _req(cfg, method, path, payload=None) -> dict:
    headers = {"Authorization": f"Bearer {config.secret('NOTION_TOKEN')}",
               "Notion-Version": cfg["notion"]["version"], "Content-Type": "application/json"}
    for attempt in (1, 2):
        time.sleep(0.35)  # Notion allows about 3 requests per second
        r = requests.request(method, API + path, headers=headers, json=payload, timeout=60)
        if r.status_code == 429 and attempt == 1:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()
        return r.json()


# ---------- property helpers ----------

def title(s): return {"title": [{"text": {"content": s[:2000]}}]}
def text(s): return {"rich_text": [{"text": {"content": (s or "")[:2000]}}]}
def select(s): return {"select": {"name": s}}
def url(s): return {"url": s}
def number(n): return {"number": n}
def date(d): return {"date": {"start": d.strftime("%Y-%m-%d")}}


def pub_properties(row, src, cfg) -> dict:
    tz, p = ZoneInfo(cfg["timezone"]), cfg["playerok"]
    published = db.parse_iso(row["published_at"]).astimezone(tz)
    props = {
        "Ролик": title(f"@{row['account']} {published:%d.%m.%Y} {row['ig_shortcode'] or 'pub#' + str(row['id'])}"),
        "Аккаунт": select(row["account"]),
        "Дата публикации": date(published),
        "Дедлайн": date(published + timedelta(days=p["deadline_days"])),
        "Подать до": date(published + timedelta(days=p["submit_by_day"])),
        "ID": number(row["id"]),
    }
    optional = {
        "Донор": select(src["donor"]) if src and src["donor"] else None,
        "Оригинал": url(src["url"]) if src and src["url"] else None,
        "Ссылка": url(row["ig_url"]) if row["ig_url"] else None,
        "Просмотры": number(row["last_views"]) if row["last_views"] is not None else None,
        "Рост 48ч %": number(round(row["growth_48h_pct"], 1)) if row["growth_48h_pct"] is not None else None,
        "Статус": select(PUB_STATUS[row["submit_status"]]) if row["submit_status"] else None,
        "Сумма ₽": number(row["amount_rub"]) if row["amount_rub"] is not None else None,
        "Причина отказа": text(row["reject_reason"]) if row["reject_reason"] else None,
        "Баннер": select("с озвучкой" if row["banner_type"] == "voiced" else "обычный") if row["banner_type"] else None,
    }
    return props | {k: v for k, v in optional.items() if v is not None}


def account_properties(nick, db_row, cfg, views7, views30, income) -> dict:
    acc = next((a for a in cfg["accounts"] if a["nick"] == nick), {})
    props = {"Ник": title(nick), "Профиль AdsPower": text(acc.get("adspower_id", "")),
             "Статус": select(ACC_STATUS[db_row["status"]]), "Просмотры 7д": number(views7),
             "Просмотры 30д": number(views30), "Доход ₽": number(income)}
    if db_row["followers"] is not None:
        props["Подписчики"] = number(db_row["followers"])
    return props


# ---------- setup and sync ----------

def schema_publications():
    sel = lambda *names: {"select": {"options": [{"name": n} for n in names]}}  # noqa: E731
    return {"Ролик": {"title": {}}, "Донор": {"select": {}}, "Оригинал": {"url": {}}, "Ссылка": {"url": {}},
            "Аккаунт": {"select": {}}, "Дата публикации": {"date": {}}, "Дедлайн": {"date": {}},
            "Подать до": {"date": {}}, "Просмотры": {"number": {}}, "Рост 48ч %": {"number": {}},
            "Статус": sel(*PUB_STATUS.values()), "Сумма ₽": {"number": {}}, "Причина отказа": {"rich_text": {}},
            "Баннер": sel("обычный", "с озвучкой"), "ID": {"number": {}}}


def schema_accounts():
    return {"Ник": {"title": {}}, "Профиль AdsPower": {"rich_text": {}},
            "Статус": {"select": {"options": [{"name": n} for n in ACC_STATUS.values()]}},
            "Подписчики": {"number": {}}, "Просмотры 7д": {"number": {}}, "Просмотры 30д": {"number": {}},
            "Доход ₽": {"number": {}}}


def setup(cfg, parent_page_id, dry):
    out = {}
    for env, name, schema in (("NOTION_PUBLICATIONS_DB", "Публикации", schema_publications()),
                              ("NOTION_ACCOUNTS_DB", "Аккаунты", schema_accounts())):
        payload = {"parent": {"type": "page_id", "page_id": parent_page_id},
                   "title": [{"type": "text", "text": {"content": name}}], "properties": schema}
        if dry:
            print(f"[notion dry-run] create database {name}: {list(schema)}")
            continue
        out[env] = _req(cfg, "POST", "/databases", payload)["id"]
    for env, dbid in out.items():
        print(f"{env}={dbid}")
    return out


def _upsert(cfg, page_id, database_env, props, dry) -> str | None:
    if dry:
        print(f"[notion dry-run] {'update' if page_id else 'create'} {props}")
        return page_id
    if page_id:
        _req(cfg, "PATCH", f"/pages/{page_id}", {"properties": props})
        return page_id
    return _req(cfg, "POST", "/pages", {"parent": {"database_id": config.secret(database_env)},
                                        "properties": props})["id"]


def sync(cfg, con, dry, now=None) -> dict:
    if not os.environ.get("NOTION_TOKEN"):
        log.info("notion-sync skipped: NOTION_TOKEN not set")
        return {"publications": 0, "accounts": 0}
    now = now or datetime.now(timezone.utc)
    n_pub = 0
    for row in con.execute("SELECT * FROM publications WHERE published_at IS NOT NULL AND notion_dirty = 1"
                           ).fetchall():
        src = con.execute("SELECT donor, url FROM sources WHERE id = ?", (row["source_id"],)).fetchone()
        page = _upsert(cfg, row["notion_page_id"], "NOTION_PUBLICATIONS_DB", pub_properties(row, src, cfg), dry)
        n_pub += 1
        if not dry:
            with con:  # notion_page_id / notion_dirty are not in the trigger's column list: no re-dirtying
                con.execute("UPDATE publications SET notion_dirty = 0, notion_page_id = ? WHERE id = ?",
                            (page, row["id"]))
    d7, d30 = db.iso(now - timedelta(days=7)), db.iso(now - timedelta(days=30))
    n_acc = 0
    for a in con.execute("SELECT * FROM accounts").fetchall():
        s = lambda sql, *args: con.execute(sql, (a["nick"], *args)).fetchone()[0] or 0  # noqa: E731
        v7 = s("SELECT SUM(last_views) FROM publications WHERE account = ? AND published_at >= ?", d7)
        v30 = s("SELECT SUM(last_views) FROM publications WHERE account = ? AND published_at >= ?", d30)
        income = s("SELECT SUM(amount_rub) FROM publications WHERE account = ? AND submit_status = 'approved'")
        page = _upsert(cfg, a["notion_page_id"], "NOTION_ACCOUNTS_DB",
                       account_properties(a["nick"], a, cfg, v7, v30, income), dry)
        n_acc += 1
        if not dry and page != a["notion_page_id"]:
            with con:
                con.execute("UPDATE accounts SET notion_page_id = ? WHERE nick = ?", (page, a["nick"]))
    log.info("notion-sync: %s publications, %s accounts%s", n_pub, n_acc, " (dry-run)" if dry else "")
    return {"publications": n_pub, "accounts": n_acc}
