import pytest

from clips import db, reports, telegram

PUB = {"id": 7, "account": "acc1", "ig_url": "https://www.instagram.com/reel/AAA/", "last_views": 120_000,
       "growth_48h_pct": 3.2, "submit_reason": "stalled", "banner_type": "voiced",
       "published_at": "2026-11-01T10:00:00Z", "needs_screen_recording": 0}


def test_msg_submit(cfg):
    text, buttons = telegram.msg_submit(PUB, cfg)
    assert PUB["ig_url"] in text and "700 ₽" in text and "Подать до: 20.11" in text and "дедлайн 22.11" in text
    assert "120 000" in text and "запись экрана" not in text and chr(0x2014) not in text
    assert [b[1] for row in buttons for b in row] == ["sub:7", "ok:7", "rej:7"]
    big, _ = telegram.msg_submit({**PUB, "last_views": 600_000, "needs_screen_recording": 1}, cfg)
    assert "запись экрана" in big and "/geo 7" in big


@pytest.fixture
def bot(con, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OPERATOR_IDS", "111")
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    con.execute("INSERT INTO publications (account, state, scheduled_at, created_at, last_views) "
                "VALUES ('acc1', 'published', 'x', 'x', 77000)")
    con.commit()

    def press(data, user=111):
        return {"callback_query": {"id": "q", "from": {"id": user}, "data": data, "message": {"chat": {"id": 5}}}}

    def say(text, user=111):
        return {"message": {"from": {"id": user}, "chat": {"id": 5}, "text": text}}

    return press, say


def row(con):
    return con.execute("SELECT * FROM publications WHERE id = 1").fetchone()


def test_bot_flow(cfg, con, bot):
    press, say = bot
    pending = {}
    h = lambda u: telegram.handle_update(con, cfg, u, pending)  # noqa: E731
    assert h(press("sub:1"))[0][1].startswith("Отмечено: pub#1 подан (77 000")
    assert row(con)["submit_status"] == "submitted" and row(con)["submitted_views"] == 77000
    h(press("ok:1"))
    assert "Нужна сумма" in h(say("abc"))[0][1] and pending                 # still waiting for a number
    h(say("1 400 ₽"))
    assert (row(con)["submit_status"], row(con)["amount_rub"]) == ("approved", 1400) and not pending
    h(press("rej:1"))
    h(say("аудитория не из России"))
    assert row(con)["reject_reason"] == "аудитория не из России"
    assert h(say("/geo 1 83"))[0][1] == "pub#1: Россия 83%" and row(con)["ru_audience_pct"] == 83
    assert "Формат" in h(say("/geo 1 150"))[0][1]
    db.set_account_status(con, "acc1", "check", "checkpoint")
    h(press("acc:acc1"))
    assert con.execute("SELECT status FROM accounts").fetchone()[0] == "active"
    assert "Сводка" in h(say("/status"))[0][1]


def test_bot_ignores_strangers(cfg, con, bot):
    press, say = bot
    assert telegram.handle_update(con, cfg, press("sub:1", user=999), {}) == []
    assert row(con)["submit_status"] is None


def test_send_without_token_does_not_raise(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    telegram.send("test")


def test_summary(cfg, con):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}, {"nick": "acc2"}]})
    db.set_account_status(con, "acc2", "check", "checkpoint")
    text = reports.summary_text(con, cfg)
    assert "активны 1" in text and "проверка 1 (@acc2)" in text and "Свободных подписей: ⚠️ japanese/anime 0" in text


def test_token_never_in_error(monkeypatch):
    import requests
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")

    def boom(url, **kw):
        raise requests.ConnectionError(f"failed for url: {url}")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(telegram.TelegramError) as e:
        telegram._api("getMe")
    assert "SECRET" not in str(e.value) and "***" in str(e.value)
    monkeypatch.setenv("TELEGRAM_OPERATOR_IDS", "1")
    assert telegram.send("x") is False                          # failure reported, not raised


def test_failed_update_is_redelivered_then_skipped(cfg, con, bot, monkeypatch):
    press, say = bot
    calls = {"getUpdates": 0}
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)

    def fake_api(method, **kw):
        if method == "getUpdates":
            calls["getUpdates"] += 1
            return [{"update_id": 10, **press("sub:1")}]
        return {}

    monkeypatch.setattr(telegram, "_api", fake_api)
    monkeypatch.setattr(telegram, "send", lambda *a, **k: False)
    real, boom = telegram.handle_update, {"n": 0}

    def flaky(*a):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("database is locked")
        return real(*a)

    monkeypatch.setattr(telegram, "handle_update", flaky)
    pending, fails = {}, {}
    assert telegram.poll_once(con, cfg, 0, pending, fails) == 10            # failed: offset not advanced
    assert telegram.poll_once(con, cfg, 10, pending, fails) == 11           # redelivered and handled
    assert row(con)["submit_status"] == "submitted"
    monkeypatch.setattr(telegram, "handle_update", lambda *a: (_ for _ in ()).throw(RuntimeError("bad")))
    off = 11
    for _ in range(telegram.MAX_TRIES):                                      # a poison update is skipped
        off = telegram.poll_once(con, cfg, off, pending, fails)
    assert off == 11 and fails == {}
