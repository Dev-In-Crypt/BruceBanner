import json
from datetime import datetime, timedelta, timezone

import pytest

from clips import db, submit, telegram

P = dict(rate_static_rub_per_100k=400, rate_voiced_rub_per_100k=700, min_paid_views=50_000,
         step_views=50_000, max_payout_rub=10_000, deadline_days=21, submit_by_day=19,
         stall_growth_pct_48h=5.0, screen_recording_views=500_000, wait_for_next_step=True)
NOW = datetime(2026, 11, 17, 6, 20, tzinfo=timezone.utc)


def test_payout():
    assert submit.payout_rub(49_999, True, P) == 0
    assert submit.payout_rub(50_000, False, P) == 200
    assert submit.payout_rub(120_000, True, P) == 700
    assert submit.payout_rub(2_000_000, True, P) == 10_000


def test_decide():
    d = submit.decide
    assert d(40_000, 5, 30_000, "active", P) == ("low", None)
    assert d(80_000, 5, 50_000, "active", P) == ("candidate", None)
    assert d(80_000, 5, 79_000, "active", P) == ("submit_now", "stalled")
    assert d(98_000, 5, 95_000, "active", P) == ("candidate", "waiting_next_step")
    assert d(98_000, 18, 95_000, "active", P) == ("submit_now", "stalled")
    assert d(60_000, 19, 30_000, "active", P) == ("submit_now", "age")
    assert d(60_000, 3, None, "check", P) == ("submit_now", "account_in_danger")
    assert d(60_000, 3, None, "active", P) == ("candidate", None)
    assert d(60_000, 22, 59_000, "active", P) == ("expired", None)


class Sent(list):
    pass


@pytest.fixture
def sent(monkeypatch):
    out = Sent()
    state = {"ok": True}

    def fake_send(text, buttons=None, photo=None, dry=False):
        out.append((text, buttons))
        return state["ok"]

    monkeypatch.setattr(telegram, "send", fake_send)
    out.state = state
    return out


def add_pub(con, code, days_ago, state="published", **kw):
    t = db.iso(NOW - timedelta(days=days_ago))
    cols = {"account": "acc1", "state": state, "scheduled_at": t, "published_at": t, "created_at": t,
            "ig_shortcode": code, "ig_url": f"https://www.instagram.com/reel/{code}/", "banner_type": "voiced", **kw}
    con.execute(f"INSERT INTO publications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(cols.values()))
    con.commit()


def stats_item(code, views, owner="acc1", caption="", ts=NOW):
    return {"shortCode": code, "ownerUsername": owner, "videoPlayCount": views, "likesCount": 5,
            "caption": caption, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def test_collect_history_and_unconfirmed(cfg, con, tmp_path, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "AAA", 3)
    add_pub(con, None, 0, state="unconfirmed", caption="海辺の朝、静かな時間。\n#旅行",
            share_clicked_at=db.iso(NOW - timedelta(minutes=30)), ig_url=None)
    f = tmp_path / "s.json"
    f.write_text(json.dumps([stats_item("AAA", 70_000), stats_item("NEW", 10, caption="海辺の朝、静かな時間。 #旅行"),
                             stats_item("ZZZ", 99, owner="someone_else")], ensure_ascii=False))
    submit.collect(cfg, con, False, f, now=NOW)
    submit.collect(cfg, con, False, f, now=NOW + timedelta(days=1))
    snaps = con.execute("SELECT taken_at, views FROM view_snapshots WHERE publication_id = 1").fetchall()
    assert len(snaps) == 2                                                  # history, not just the last value
    p2 = con.execute("SELECT state, ig_shortcode FROM publications WHERE id = 2").fetchone()
    assert tuple(p2) == ("published", "NEW")                               # unconfirmed resolved by caption


def test_submit_check_notifies_once_and_is_sticky(cfg, con, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "AAA", 5, last_views=80_000)
    con.execute("INSERT INTO view_snapshots VALUES (1, ?, 79000, 0)", (db.iso(NOW - timedelta(hours=47)),))
    con.commit()
    assert submit.run_submit_check(cfg, con, False, now=NOW) == [(1, "submit_now", "stalled")]
    assert submit.run_submit_check(cfg, con, False, now=NOW) == []          # sticky, not re-evaluated
    assert len(sent) == 1 and "Пора подавать" in sent[0][0]
    assert "Ожидаемая сумма: 350 ₽" in sent[0][0]                         # voiced: 50k paid * 700 / 100k


def test_account_in_danger_triggers_submit(cfg, con, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "AAA", 2, last_views=60_000)
    db.set_account_status(con, "acc1", "check", "checkpoint")
    assert submit.run_submit_check(cfg, con, False, now=NOW, nick="acc1") == [(1, "submit_now", "account_in_danger")]


def test_undelivered_alert_is_retried(cfg, con, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "AAA", 5, last_views=80_000)
    con.execute("INSERT INTO view_snapshots VALUES (1, ?, 79000, 0)", (db.iso(NOW - timedelta(hours=47)),))
    con.commit()
    sent.state["ok"] = False                                     # Telegram down
    submit.run_submit_check(cfg, con, False, now=NOW)
    assert con.execute("SELECT notified_at FROM publications").fetchone()[0] is None
    sent.state["ok"] = True                                      # Telegram back: the alert goes out once
    submit.run_submit_check(cfg, con, False, now=NOW + timedelta(days=1))
    submit.run_submit_check(cfg, con, False, now=NOW + timedelta(days=2))
    assert len(sent) == 2 and con.execute("SELECT notified_at FROM publications").fetchone()[0]
    assert con.execute("SELECT submit_reason FROM publications").fetchone()[0] == "stalled"   # decision kept


def test_unconfirmed_without_caption_takes_reel_closest_to_click(cfg, con, tmp_path, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    click = NOW - timedelta(hours=5)
    add_pub(con, None, 0, state="unconfirmed", caption="", share_clicked_at=db.iso(click), ig_url=None)
    f = tmp_path / "s.json"
    f.write_text(json.dumps([stats_item("LATER", 5, ts=NOW),                         # a later post: not ours
                             stats_item("OURS", 5, ts=click + timedelta(minutes=3)),
                             stats_item("EARLY", 5, ts=click - timedelta(hours=2))]))
    submit.collect(cfg, con, False, f, now=NOW)
    assert con.execute("SELECT ig_shortcode FROM publications").fetchone()[0] == "OURS"


def test_48h_baseline_window(con):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "AAA", 5, last_views=80_000)
    con.execute("INSERT INTO view_snapshots VALUES (1, ?, 10000, 0)", (db.iso(NOW - timedelta(hours=72)),))
    con.commit()
    assert submit.views_48h_ago(con, 1, NOW) is None             # 72 h old: not a 48 h baseline
    con.execute("INSERT INTO view_snapshots VALUES (1, ?, 79000, 0)", (db.iso(NOW - timedelta(hours=47)),))
    con.commit()
    assert submit.views_48h_ago(con, 1, NOW) == 79000


def test_missing_reel_warning_retried_until_delivered(cfg, con, tmp_path, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "GONE", 2, last_views=60_000, submit_status="candidate")
    click = NOW - timedelta(hours=60)
    add_pub(con, None, 3, state="unconfirmed", share_clicked_at=db.iso(click), ig_url=None)
    f = tmp_path / "s.json"
    f.write_text("[]")
    sent.state["ok"] = False
    submit.collect(cfg, con, False, f, now=NOW)
    assert [r[0] for r in con.execute("SELECT error FROM publications ORDER BY id")] == [None, None]
    sent.state["ok"] = True
    submit.collect(cfg, con, False, f, now=NOW)
    assert [r[0] for r in con.execute("SELECT error FROM publications ORDER BY id")] == \
        ["missing_in_stats", "unconfirmed_alerted"]
    n = len(sent)
    submit.collect(cfg, con, False, f, now=NOW)                  # delivered once: no repeats
    assert len(sent) == n


def test_reel_without_view_count_is_not_reported_missing(cfg, con, tmp_path, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "AAA", 2, last_views=60_000, submit_status="candidate")
    f = tmp_path / "s.json"
    f.write_text(json.dumps([{**stats_item("AAA", 0), "videoPlayCount": None, "videoViewCount": None}]))
    submit.collect(cfg, con, False, f, now=NOW)
    assert sent == [] and con.execute("SELECT error FROM publications").fetchone()[0] is None


def test_engagement_risk(cfg):
    p = cfg["playerok"]
    assert submit.engagement_risk(100_000, 200, 50, p)            # 0.25% < 1%
    assert not submit.engagement_risk(100_000, 3000, 100, p)
    assert not submit.engagement_risk(40_000, 0, 0, p)             # below 50k: no flag
    assert not submit.engagement_risk(100_000, None, None, p)      # no data: no flag


def test_submit_message_carries_warnings(cfg, con, sent):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}]})
    add_pub(con, "RISK1", 20, last_views=120_000, last_likes=100, last_comments=5)
    submit.run_submit_check(cfg, con, False, now=NOW)
    text = sent[0][0]
    assert "Риск отказа" in text and "не отмечен как привязанный" in text and "Лимит автора не введён" in text


def test_author_limit_alert_once_over_half(con):
    submit.set_author_limit(con, 100, 60)
    assert submit.limit_alerts(con) == []                          # 40% used
    submit.set_author_limit(con, 100, 40)
    assert "60%" in submit.limit_alerts(con)[0] and submit.limit_alerts(con) == []   # once per entry
