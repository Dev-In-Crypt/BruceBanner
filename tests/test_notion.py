from clips import db, notion


def pub_row(con, **kw):
    cols = {"account": "acc1", "state": "published", "scheduled_at": "x", "created_at": "x",
            "published_at": "2026-11-01T10:00:00Z", "ig_shortcode": "AAA",
            "ig_url": "https://www.instagram.com/reel/AAA/", "last_views": 80000, "submit_status": "submit_now",
            "banner_type": "voiced", **kw}
    con.execute(f"INSERT INTO publications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()))
    con.commit()


def test_pub_properties(cfg, con):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    pub_row(con)
    props = notion.pub_properties(con.execute("SELECT * FROM publications").fetchone(), None, cfg)
    assert props["Статус"] == {"select": {"name": "Пора подавать"}}
    assert props["Дедлайн"]["date"]["start"] == "2026-11-22" and props["Подать до"]["date"]["start"] == "2026-11-20"
    assert props["Баннер"]["select"]["name"] == "с озвучкой" and "Сумма ₽" not in props   # None values skipped


def test_sync_only_dirty_rows(cfg, con, monkeypatch):
    calls = []

    def fake_req(c, method, path, payload=None):
        calls.append((method, path))
        return {"id": f"page{len(calls)}"}

    monkeypatch.setattr(notion, "_req", fake_req)
    monkeypatch.setenv("NOTION_TOKEN", "t")
    monkeypatch.setenv("NOTION_PUBLICATIONS_DB", "db1")
    monkeypatch.setenv("NOTION_ACCOUNTS_DB", "db2")
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    pub_row(con)
    notion.sync(cfg, con, False)
    assert ("POST", "/pages") in calls and con.execute("SELECT notion_dirty FROM publications").fetchone()[0] == 0
    calls.clear()
    notion.sync(cfg, con, False)                       # nothing changed: no publication calls at all
    assert all(path != "/pages" or method != "POST" for method, path in calls) and len(calls) == 1  # 1 account PATCH
    con.execute("UPDATE publications SET submit_status = 'submitted'")
    con.commit()
    calls.clear()
    notion.sync(cfg, con, False)                       # dirty again via the trigger -> PATCH the same page
    assert ("PATCH", "/pages/page1") in calls


def test_sync_skipped_without_token(cfg, con, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert notion.sync(cfg, con, False) == {"publications": 0, "accounts": 0}
