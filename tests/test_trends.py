import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from clips import db, trends, video

NOW = datetime.now(timezone.utc)


def item(code, donor="donor_a", views=100_000, hours_ago=100, duration=20.0, caption="смешной мем"):
    return {"shortCode": code, "ownerUsername": donor, "videoUrl": f"https://cdn.example/{code}.mp4",
            "caption": caption, "videoPlayCount": views, "likesCount": 10, "videoDuration": duration,
            "timestamp": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def test_fetch_from_file_is_idempotent(cfg, con, tmp_path):
    f = tmp_path / "dump.json"
    f.write_text(json.dumps([item("A1"), item("A2"), item("A3")]))
    trends.fetch(cfg, con, False, f)
    f.write_text(json.dumps([item("A1", views=999), item("A2"), item("A3")]))
    trends.fetch(cfg, con, False, f)
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 3
    assert con.execute("SELECT views FROM sources WHERE shortcode='A1'").fetchone()[0] == 999


def test_blocked(cfg):
    b = lambda c, d="d": trends.blocked_reason(c, d, cfg)  # noqa: E731
    assert b("Лучший мем #юмор") is None
    assert b("Это ВОЙНА мемов")
    assert b("Хачапури по-аджарски") is None      # no false positive
    assert b("жидкость для линз") is None         # no false positive
    assert b("ну ты и сука")
    cfg["trends"]["blocked_donors"] = ["BadDonor"]
    assert b("ok", "baddonor") == "blocked_donor"


def test_decide(cfg):
    t = cfg["trends"]
    assert trends.decide(500_000, 10, 20, 100_000, t) == ("selected", None)
    assert trends.decide(150_000, 10, 20, 100_000, t)[0] == "new"         # below 2x median
    assert trends.decide(500_000, 50, 20, 100_000, t) == ("rejected", "too_old")
    assert trends.decide(500_000, 10, 200, 100_000, t) == ("rejected", "duration")
    assert trends.decide(500_000, 10, 20, None, t) == ("new", "wait:no_baseline")
    assert trends.decide(90_000, 10, 20, 10_000, t)[0] == "new"           # below absolute min_views


def test_select_flow(cfg, con, tmp_path):
    history = [item(f"H{i}", views=100_000 + i * 1000, hours_ago=100 + i) for i in range(10)]
    fresh = [item("HIT", views=500_000, hours_ago=10), item("MEH", views=150_000, hours_ago=10),
             item("OLD", views=900_000, hours_ago=60), item("BAD", views=900_000, hours_ago=5, caption="политика")]
    f = tmp_path / "d.json"
    f.write_text(json.dumps(history + fresh))
    trends.fetch(cfg, con, False, f)
    trends.select(cfg, con, False)
    st = {r[0]: (r[1], r[2]) for r in con.execute("SELECT shortcode, state, reason FROM sources")}
    assert st["HIT"][0] == "selected" and st["MEH"] == ("new", "wait:below_threshold")
    assert st["OLD"] == ("rejected", "too_old") and st["BAD"][0] == "rejected"
    score = con.execute("SELECT score FROM sources WHERE shortcode='HIT'").fetchone()[0]
    assert 8 < score < 9      # views/median ~4.8, x1.75 because a 20 s source gets the voiced banner


def test_dhash_same_video_reencoded(media, tmp_path):
    small = tmp_path / "small.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media["src25"]), "-vf", "scale=540:960",
                    "-crf", "35", str(small)], check=True)
    assert video.hash_distance(video.video_hashes(media["src25"], 5), video.video_hashes(small, 5)) <= 6
    assert video.hash_distance(video.video_hashes(media["src25"], 5), video.video_hashes(media["midroll"], 5)) > 20


def test_download_dedup(cfg, con, media, tmp_path, monkeypatch):
    copy = tmp_path / "reupload.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media["src25"]), "-vf", "scale=540:960",
                    "-crf", "33", str(copy)], check=True)
    files = {"ORIG": media["src25"], "REUP": copy, "OTHER": media["midroll"]}

    def fake_download(c, src):
        p = c["data"] / "sources" / f"{src['shortcode']}.mp4"
        shutil.copy(files[src["shortcode"]], p)
        return p

    monkeypatch.setattr(trends, "download_one", fake_download)
    for i, code in enumerate(("ORIG", "REUP", "OTHER")):
        con.execute("INSERT INTO sources (shortcode, donor, url, video_url, fetched_at, state) VALUES (?,?,?,?,?, 'selected')",
                    (code, "d", "u", "v", f"2026-11-17T00:00:0{i}Z"))
    con.commit()
    assert trends.download(cfg, con, False) == {"ready": 2, "duplicate": 1}
    st = dict(con.execute("SELECT shortcode, state FROM sources"))
    assert st == {"ORIG": "ready", "REUP": "duplicate", "OTHER": "ready"}
    assert not (cfg["data"] / "sources" / "REUP.mp4").exists()


def test_speech_rules(cfg, media, monkeypatch):
    from types import SimpleNamespace as NS
    f = cfg["filter"]

    def model(lang, prob, speech=True):
        segs = [NS(text="привет"), NS(text="мир")] if speech else []
        return lambda name: NS(transcribe=lambda audio, **kw: (iter(segs), NS(language=lang, language_probability=prob)))

    for args, want in ((("en", 0.99), ("en", "")), (("ru", 0.95), (None, "привет мир")),   # foreign / Russian
                       (("en", 0.4), (None, "привет мир")), (("en", 0.99, False), (None, ""))):  # unsure / no speech
        monkeypatch.setattr(trends, "_whisper", model(*args))
        assert trends.speech(media["src25"], f) == want, args
    assert trends.speech(media["src15_noaudio"], f) == (None, "")      # no audio track: nothing to check


def test_forbidden_film_topics(cfg):
    b = lambda c: trends.blocked_reason(c, "d", cfg)  # noqa: E731
    for text in ("Лучшая сцена из Бригады", "Слово пацана, 5 серия", "Брат 2 концовка", "Peaky Blinders edit",
                 "дембель неизбежен", "Солдаты, 3 сезон"):
        assert b(text), text
    for text in ("Мой брат опять съел торт", "Кот против пылесоса", "Ирония судьбы, лучшая сцена"):
        assert b(text) is None, text


def test_download_rejects_foreign_speech_and_topics(cfg, con, media, monkeypatch):
    cfg["filter"]["speech_check"] = True

    def fake_download(c, src):
        p = c["data"] / "sources" / f"{src['shortcode']}.mp4"
        shutil.copy(media["src25"], p)
        return p

    monkeypatch.setattr(trends, "download_one", fake_download)
    heard = {"EN": ("en", ""), "GANG": (None, "это наша бригада, пацаны"), "OK": (None, "смотри какой кот")}
    monkeypatch.setattr(trends, "speech", lambda path, f: heard[path.stem])
    for code in heard:
        con.execute("INSERT INTO sources (shortcode, donor, url, video_url, fetched_at, state) "
                    "VALUES (?, 'd', 'u', 'v', '2026-11-17T00:00:00Z', 'selected')", (code,))
    con.commit()
    assert trends.download(cfg, con, False) == {"rejected": 2, "ready": 1}
    got = {r[0]: (r[1], r[2]) for r in con.execute("SELECT shortcode, state, reason FROM sources")}
    assert got["EN"] == ("rejected", "speech_en")
    assert got["GANG"] == ("rejected", r"speech_blocked_keyword:\bбригад[аыуе]\b")   # forbidden topic heard in the clip
    assert got["OK"][0] == "ready"
    assert not (cfg["data"] / "sources" / "EN.mp4").exists() and not (cfg["data"] / "sources" / "GANG.mp4").exists()


def test_rank_score_prefers_voiced_and_shares(cfg):
    base = trends.rank_score(300_000, 100_000, 8, None, None, cfg)
    voiced = trends.rank_score(300_000, 100_000, 20, None, None, cfg)
    assert base == 3.0 and voiced == 3.0 * 700 / 400                   # 12 s+ gets the voiced banner: worth more
    shared = trends.rank_score(300_000, 100_000, 8, 6_000, 0.01, cfg)  # 2% shares vs donor 1%: x2 (capped)
    assert shared == 6.0
    assert trends.rank_score(300_000, 100_000, 8, 30, 0.01, cfg) == 1.5  # far below: x0.5 floor


def test_fetch_tags_donor_group_and_shares(cfg, con, tmp_path):
    cfg["trends"]["donors_movies"] = ["kino_cuts"]
    f = tmp_path / "d.json"
    f.write_text(json.dumps([{**item("M1", donor="kino_cuts"), "sharesCount": 900}, item("X1")]))
    trends.fetch(cfg, con, False, f)
    got = {r[0]: (r[1], r[2]) for r in con.execute("SELECT shortcode, donor_group, shares FROM sources")}
    assert got == {"M1": ("movies", 900), "X1": ("memes", None)}


def test_donor_moved_to_movies_updates_its_sources(cfg, con, tmp_path):
    f = tmp_path / "d.json"
    f.write_text(json.dumps([item("K1", donor="kino_cuts")]))
    trends.fetch(cfg, con, False, f)
    cfg["trends"]["donors_movies"] = ["kino_cuts"]           # owner moves the donor to the film-cut list
    trends.fetch(cfg, con, False, f)
    assert con.execute("SELECT donor_group FROM sources WHERE shortcode = 'K1'").fetchone()[0] == "movies"


def test_source_shorter_than_banner_is_rejected(cfg, con, tmp_path, monkeypatch):
    history = [item(f"H{i}", views=100_000, hours_ago=100 + i) for i in range(10)]
    f = tmp_path / "d.json"
    f.write_text(json.dumps(history + [item("SHORT", views=900_000, hours_ago=5, duration=6.0)]))
    trends.fetch(cfg, con, False, f)
    monkeypatch.setattr(video, "banner_duration", lambda c: 8.0)
    trends.select(cfg, con, False)
    assert con.execute("SELECT state, reason FROM sources WHERE shortcode = 'SHORT'").fetchone()[:] == \
        ("rejected", "shorter_than_banner")
