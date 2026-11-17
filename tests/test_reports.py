from datetime import datetime, timedelta, timezone

from clips import db, reports

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)


def test_weekly(cfg, con):
    db.sync_accounts(con, {"accounts": [{"nick": "acc1"}, {"nick": "acc2"}]})
    t = db.iso(NOW - timedelta(days=2))
    rows = [("acc1", 120_000, "japanese", 80), ("acc1", 10_000, "japanese", None),
            ("acc2", 60_000, "none", 95), ("acc2", 5_000, "none", None)]
    for acc, views, lang, geo in rows:
        con.execute("INSERT INTO publications (account, state, scheduled_at, created_at, published_at, last_views,"
                    " caption_lang, ru_audience_pct, banner_type) VALUES (?, 'published', ?, ?, ?, ?, ?, ?, 'voiced')",
                    (acc, t, t, t, views, lang, geo))
    con.execute("UPDATE publications SET submit_status = 'approved', amount_rub = 700, decided_at = ? WHERE id = 1",
                (t,))
    con.commit()
    text = reports.weekly(con, cfg, NOW)
    assert "роликов 4" in text and "просмотров 195 000" in text and "50k+ 50%" in text and "одобрено 700 ₽" in text
    assert "РФ 80% (n=1)" in text and "РФ 95% (n=1)" in text and len(text) <= 4096
