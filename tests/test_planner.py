import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from clips import captions, db, planner, video

TZ = ZoneInfo("Europe/Moscow")


def local(h, m=0, d=date(2026, 11, 17)):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def test_pick_slot_respects_window_and_gap(cfg):
    p, rng = cfg["planner"], random.Random(1)
    now, busy = local(9).astimezone(timezone.utc), local(12)
    for _ in range(100):
        t = planner.pick_slot(now, local(9).date(), [busy], p, TZ, rng).astimezone(TZ)
        assert local(10) <= t <= local(22) and t >= local(9, 15)
        assert abs(t - busy) >= timedelta(minutes=p["min_gap_minutes"])


def test_pick_slot_none_when_window_passed(cfg):
    now = local(23).astimezone(timezone.utc)
    assert planner.pick_slot(now, local(23).date(), [], cfg["planner"], TZ, random.Random(1)) is None


@pytest.fixture
def world(cfg, con, monkeypatch, tmp_path):
    """Two Japanese-caption accounts, ready sources, a caption pool; build is faked."""
    base = cfg["accounts"][0]
    cfg["accounts"] = [{**base, "nick": "acc1"}, {**base, "nick": "acc2"}]
    db.sync_accounts(con, cfg)

    def fake_build(src, account, c, out, text=None, use_midroll=True):
        out.write_bytes(b"x")
        return {"duration": 8.0, "midroll_at": None, "midroll_dur": 0, "banner_type": "static",
                "banner_file": "top.png", "background": "bg.png"}

    monkeypatch.setattr(video, "build", fake_build)
    now = datetime.now(timezone.utc)
    f = tmp_path / "src.mp4"
    f.write_bytes(b"x")

    def add_sources(n, posted_hours_ago=5):
        for i in range(n):
            con.execute("INSERT INTO sources (shortcode, donor, url, video_url, fetched_at, posted_at, state, score,"
                        " file_path) VALUES (?, 'd', 'u', 'v', 'x', ?, 'ready', ?, ?)",
                        (f"S{i}", db.iso(now - timedelta(hours=posted_hours_ago)), 10 - i, str(f)))
        con.commit()

    def add_captions(n):
        for i in range(n):
            con.execute("INSERT INTO captions (lang, text, fetched_at) VALUES ('japanese', ?, 'x')", (f"海の朝 {i}",))
        con.commit()

    return add_sources, add_captions, local(9).astimezone(timezone.utc)


def pubs(con):
    return con.execute("SELECT * FROM publications ORDER BY id").fetchall()


def test_plan_daily_limit_idempotent_and_unique(cfg, con, world):
    add_sources, add_captions, now = world
    add_sources(5)
    add_captions(5)
    planner.plan(cfg, con, False, now=now, rng=random.Random(2))
    planner.plan(cfg, con, False, now=now, rng=random.Random(3))     # second run: nothing new
    p = pubs(con)
    assert len(p) == 2 and {r["account"] for r in p} == {"acc1", "acc2"}
    assert len({r["source_id"] for r in p}) == 2                      # distinct sources
    assert len({r["caption"] for r in p}) == 2 and all(r["state"] == "built" for r in p)
    used = dict(con.execute("SELECT id, used_for FROM captions WHERE used_at IS NOT NULL"))
    assert sorted(used.values()) == sorted(f"pub:{r['id']}" for r in p)


def test_same_source_never_on_two_accounts(cfg, con, world):
    add_sources, add_captions, now = world
    add_sources(1)
    add_captions(5)
    planner.plan(cfg, con, False, now=now, rng=random.Random(1))
    assert len(pubs(con)) == 1


def test_paused_account_not_planned(cfg, con, world):
    add_sources, add_captions, now = world
    add_sources(5)
    add_captions(5)
    db.set_account_status(con, "acc2", "check", "checkpoint")
    planner.plan(cfg, con, False, now=now, rng=random.Random(1))
    assert {r["account"] for r in pubs(con)} == {"acc1"}


def test_no_caption_skips_slot(cfg, con, world):
    add_sources, add_captions, now = world
    add_sources(5)
    add_captions(1)
    planner.plan(cfg, con, False, now=now, rng=random.Random(1))
    assert len(pubs(con)) == 1
    assert con.execute("SELECT COUNT(*) FROM captions WHERE used_at IS NULL").fetchone()[0] == 0


def test_dry_run_writes_nothing(cfg, con, world):
    add_sources, add_captions, now = world
    add_sources(5)
    add_captions(5)
    ro = db.connect(cfg["data"] / "t.db", readonly=True)
    assert len(planner.plan(cfg, ro, True, now=now, rng=random.Random(1))) == 2
    assert pubs(con) == []


def test_repost_of_a_flop_goes_to_another_account(cfg, con, world):
    add_sources, add_captions, now = world
    add_sources(1)
    add_captions(5)
    old = db.iso(now - timedelta(days=20))
    con.execute("INSERT INTO publications (source_id, account, attempt, state, scheduled_at, published_at, last_views,"
                " created_at) VALUES (1, 'acc1', 1, 'published', ?, ?, 3000, ?)", (old, old, old))
    con.commit()
    planner.plan(cfg, con, False, now=now, rng=random.Random(1))
    new = [r for r in pubs(con) if r["attempt"] == 2]
    assert len(new) == 1 and new[0]["account"] == "acc2"              # never the same account again
