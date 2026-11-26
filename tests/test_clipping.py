import random
import shutil
from datetime import datetime, timedelta, timezone

from clips import clipping, kick, telegram, video

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def kclip(cid, views=50_000, duration=30, days_ago=2, title="He can't believe what chat just did", **kw):
    return {"id": cid, "title": title, "views": views, "duration": duration, "is_mature": False,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
            "video_url": f"https://clips.kick.com/{cid}/playlist.m3u8", "category": {"slug": "just-chatting"}, **kw}


def setup_campaign(cfg, streamers=("larry",)):
    cfg["clipping"]["campaigns"] = [{"name": "kick", "streamers": list(streamers), "language": "en",
                                     "caption": "{hook}", "caption_tags": ["#kick"], "post_window_hours": 72}]
    cfg["clipping"]["speech_check"] = False


def test_hook_and_decide(cfg):
    assert kick.hook_of("etaztjgzyhrfxsj") == "" and kick.hook_of("clip1") == ""
    assert kick.hook_of("Omg he so strong 😳 lol") == "Omg he so strong  lol"
    d = lambda **kw: kick.decide(kclip("c", **kw), cfg, NOW)  # noqa: E731
    assert d() == ("selected", None)
    assert d(category={"slug": "slots"}) == ("rejected", "category:slots")
    assert d(title="biggest casino win ever")[1].startswith("blocked_keyword")
    assert d(duration=90) == ("rejected", "duration") and d(days_ago=30) == ("rejected", "too_old")
    assert d(views=10) == ("rejected", "low_views") and d(is_mature=True) == ("rejected", "mature")


def test_fetch_rechecks_low_views(cfg, con, monkeypatch):
    setup_campaign(cfg)
    clips = [kclip("A"), kclip("B", views=10)]
    monkeypatch.setattr(kick, "streamer_clips", lambda c, slug: clips)
    kick.fetch(cfg, con, False, NOW)
    clips[1]["views"] = 90_000  # B went viral after the first look
    kick.fetch(cfg, con, False, NOW)
    st = dict(con.execute("SELECT id, state FROM kick_clips"))
    assert st == {"A": "selected", "B": "selected"}


def test_download_ready_and_dedup(cfg, con, media, monkeypatch):
    setup_campaign(cfg)
    monkeypatch.setattr(kick, "streamer_clips", lambda c, slug: [kclip("A"), kclip("B", views=40_000)])
    kick.fetch(cfg, con, False, NOW)

    def fake(c, clip):  # both Kick clips are the same moment
        p = c["data"] / "sources" / f"{clip['id']}.mp4"
        shutil.copy(media["src25"], p)
        return p

    monkeypatch.setattr(kick, "download_one", fake)
    assert kick.download(cfg, con, False) == {"ready": 1, "duplicate": 1}
    assert con.execute("SELECT state FROM kick_clips WHERE id = 'A'").fetchone()[0] == "ready"  # higher views first


def test_build_clip_format(cfg, media, tmp_path):
    out = tmp_path / "c.mp4"
    meta = video.build_clip(media["src15_noaudio"], "When chat thought he was joking", "larrywheels", cfg, out, "s")
    assert video.check_file(out, cfg, meta["duration"]) == []  # 1080x1920, h264, aac (silent track added)
    frame = video.grab_frame(out, 1.0)
    L = cfg["clipping"]["layout"]
    bar = frame.crop((0, L["video_y"] + L["video_h"] + 5, 1080, L["video_y"] + L["video_h"] + L["bar_h"] - 5))
    assert max(bar.convert("L").getextrema()) > 200  # white bar text on black
    assert video.build_clip(media["src15_noaudio"], "", "x", cfg, tmp_path / "d.mp4", "s")["duration"] > 0  # no hook


def test_plan_build_notify(cfg, con, media, monkeypatch):
    setup_campaign(cfg)
    cfg["clipping"]["accounts"] = [
        {"nick": "tt1", "platform": "tiktok", "adspower_id": "a1", "campaign": "kick", "daily_limit": 1},
        {"nick": "yt1", "platform": "youtube", "adspower_id": "a2", "campaign": "kick", "daily_limit": 1}]
    for cid, score in (("A", 9), ("B", 5), ("NOHOOK", 99)):
        con.execute("INSERT INTO kick_clips (id, campaign, streamer, video_url, fetched_at, state, score, hook,"
                    " file_path) VALUES (?, 'kick', 'larry', 'u', 'x', 'ready', ?, ?, ?)",
                    (cid, score, "" if cid == "NOHOOK" else f"hook {cid}", str(media["src8"])))
    con.commit()
    now = datetime(2026, 11, 26, 14, 0, tzinfo=timezone.utc)  # 09:00 New York, before the window
    got = clipping.plan(cfg, con, False, now, random.Random(1))
    assert sorted(i["clip"] for i in got) == ["A", "B"]  # every account a different clip, no hook-less clip
    assert {r[0] for r in con.execute("SELECT state FROM clip_posts")} == {"built"}
    assert con.execute("SELECT caption FROM clip_posts WHERE clip_id = 'A'").fetchone()[0] == "hook A #kick"
    assert clipping.plan(cfg, con, False, now, random.Random(1)) == []  # daily limit reached
    con.execute("UPDATE clip_posts SET state = 'published', published_at = '2026-11-26T15:00:00Z', "
                "post_url = 'https://www.tiktok.com/@tt1/video/' || id")
    con.commit()
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, **kw: sent.append(text) or True)
    assert clipping.notify(cfg, con, False, now + timedelta(hours=2)) == 2
    assert "2 ссылок" in sent[0] and "/video/" in sent[0] and "~71 ч" in sent[0]
    assert clipping.notify(cfg, con, False, now) == 0  # sent once


def test_stats_alerts_on_gone_posts(cfg, con, monkeypatch):
    con.execute("INSERT INTO kick_clips (id, campaign, streamer, video_url, fetched_at) VALUES ('A','kick','l','u','x')")
    for i, url in enumerate(("https://t/ok", "https://t/gone")):
        con.execute("INSERT INTO clip_posts (clip_id, account, platform, campaign, state, scheduled_at, post_url,"
                    " published_at, created_at) VALUES ('A', ?, 'tiktok', 'kick', 'published', 'x', ?, ?, 'x')",
                    (f"acc{i}", url, datetime.now(timezone.utc).isoformat()))
    con.commit()
    monkeypatch.setattr(clipping, "views_of", lambda url: 1234 if url.endswith("ok") else None)
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, **kw: sent.append(text) or True)
    assert clipping.stats(cfg, con, False) == {"ok": 1, "gone": 1}
    assert "https://t/gone" in sent[0]
    assert con.execute("SELECT last_views FROM clip_posts WHERE post_url = 'https://t/ok'").fetchone()[0] == 1234
