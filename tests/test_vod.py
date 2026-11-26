import json
import shutil
from types import SimpleNamespace as NS

from clips import kick, telegram, video, vod


def camp(cfg, review=True):
    cfg["clipping"]["campaigns"] = [{"name": "ako", "source": "video", "videos": ["https://youtu.be/AbCdEf12"],
                                     "subject": "AKO", "language": "en", "bar": "none", "review": review,
                                     "caption": "{hook}", "caption_tags": [], "post_window_hours": 72}]
    cfg["clipping"]["speech_check"] = False
    return cfg["clipping"]["campaigns"][0]


def fake_llm(monkeypatch, moments):
    calls = []

    def create(**kw):
        calls.append(kw)
        return NS(status="completed", output_text=json.dumps({"moments": moments}))

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(vod.openai, "OpenAI", lambda api_key: NS(responses=NS(create=create)))
    return calls


def test_pick_moments_keeps_only_valid(cfg, monkeypatch):
    c = camp(cfg)
    calls = fake_llm(monkeypatch, [
        {"start": 10, "end": 40, "hook": "AKO prices his rarest Patek", "why": "w"},
        {"start": 30, "end": 60, "hook": "AKO overlaps the first one", "why": "w"},      # overlaps
        {"start": 100, "end": 105, "hook": "AKO too short", "why": "w"},                  # 5 s
        {"start": 200, "end": 240, "hook": "No subject in this hook", "why": "w"},        # brief: must name AKO
        {"start": 300, "end": 340, "hook": "Why AKO never sells this Rolex", "why": "w"}])
    segs = [(0.0, 5.0, "hi"), (395.0, 400.0, "bye")]
    got = vod.pick_moments(cfg, c, "Watch collection", [{"title": "Intro", "start_time": 0}], segs)
    assert [m["start"] for m in got] == [10, 300]
    kw = calls[0]
    assert kw["text"]["format"]["type"] == "json_schema" and kw["model"] == "gpt-6-luna" and "AKO" in kw["input"]
    assert "[395.0-400.0] bye" in kw["input"]


def test_process_cuts_waits_for_review_then_plans(cfg, con, media, monkeypatch):
    c = camp(cfg)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert vod.process(cfg, con, False) == {}                       # no key: skipped, the kick run goes on
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    def fake_download(cfg_, url, out_dir):
        p = out_dir / "vod.mp4"
        shutil.copy(media["src25"], p)
        return p, {"title": "Inside My Watch Collection", "chapters": []}

    monkeypatch.setattr(vod, "download", fake_download)
    monkeypatch.setattr(vod, "transcript", lambda *a: [(0.0, 25.0, "talk")])
    monkeypatch.setattr(vod, "pick_moments", lambda *a: [{"start": 1.0, "end": 22.0, "hook": "AKO shows a $1M watch"}])
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, **kw: sent.append(text) or True)
    assert vod.process(cfg, con, False) == {"cut": 1}
    assert vod.process(cfg, con, False) == {}                       # the same video is not cut twice
    row = con.execute("SELECT * FROM kick_clips").fetchone()
    assert (row["id"], row["hook"], row["title"], row["streamer"]) == ("vod:AbCdEf12:1", "", "AKO shows a $1M watch", "ako")
    assert "ждут проверки хуков" in sent[0] and "vod:AbCdEf12:1" in sent[0]
    cfg["clipping"]["min_duration_sec"] = 12
    assert kick.download(cfg, con, False) == {"ready": 1}           # same checks as Kick clips
    assert 18 < video.probe(row["video_url"])["duration"] < 23
    assert vod.approve(con, ["vod:AbCdEf12:1"]) == 1
    assert con.execute("SELECT hook FROM kick_clips").fetchone()[0] == "AKO shows a $1M watch"
    assert c["bar"] == "none"


def test_no_bar_campaign(cfg, media, tmp_path):
    out = tmp_path / "n.mp4"
    meta = video.build_clip(media["src15_noaudio"], "AKO shows his watch", "ako", cfg, out, "s", camp={"bar": "none"})
    assert video.check_file(out, cfg, meta["duration"]) == []
    L = cfg["clipping"]["layout"]
    y = L["video_y"] + L["video_h"] + L["bar_h"] // 2
    strip = video.grab_frame(out, 1.0).crop((600, y - 10, 1050, y + 10)).convert("L")
    assert max(strip.getextrema()) < 200  # no white KICK.COM text where the bar would be


def test_campaign_layout_keeps_whole_frame(cfg, media, tmp_path):
    camp = {"bar": "none", "layout": {"video_h": 608, "video_y": 640}}
    assert video.clip_layout(cfg, camp)["video_h"] == 608 and video.clip_layout(cfg)["video_h"] == 1080
    out = tmp_path / "w.mp4"
    video.build_clip(media["src25"], "AKO wide shot", "ako", cfg, out, "s", camp=camp)
    assert video.check_file(out, cfg, video.probe(media["src25"])["duration"]) == []


def test_local_file_campaign_without_subject_in_hook(cfg, media, monkeypatch):
    path, info = vod.download(cfg, str(media["src25"]), cfg["data"] / "sources")
    assert path == media["src25"] and info["title"] == "src25" and vod.video_id(str(media["src25"])) == "src25"
    c = {**camp(cfg), "subject": "Club Cumming", "hook_names_subject": False, "about": "A nightclub TV show."}
    calls = fake_llm(monkeypatch, [{"start": 10, "end": 40, "hook": "Nobody expected this vacuum pitch", "why": "w",
                                    "score": 8}])
    got = vod.pick_moments(cfg, c, "Episode 5", [], [(0.0, 50.0, "talk")])
    assert [m["hook"] for m in got] == ["Nobody expected this vacuum pitch"]  # no subject needed in the hook
    assert "A nightclub TV show." in calls[0]["input"] and "names Club Cumming" not in calls[0]["input"]
