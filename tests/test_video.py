import re
import subprocess

import pytest

from clips import video

B = {"midroll": "x.mp4", "midroll_min_source_sec": 12, "midroll_short_until_sec": 20,
     "midroll_short_insert_sec": 10, "midroll_long_window_sec": [10, 20], "midroll_long_insert_sec": 12}


def test_probe(media):
    p = video.probe(media["src25"])
    assert abs(p["duration"] - 25) < 0.1 and p["audio"] and p["video"]["width"] == 720


def test_probe_no_audio(media):
    assert video.probe(media["src15_noaudio"])["audio"] is None


def test_midroll_at():
    assert video.midroll_at(11.9, B) is None
    assert video.midroll_at(12.0, B) == 9.0        # never in the last 3 s
    assert video.midroll_at(15.0, B) == 10.0
    assert video.midroll_at(25.0, B) == 12.0       # inside 10-20
    assert video.midroll_at(600.0, B) == 12.0
    assert video.midroll_at(25.0, {**B, "midroll": ""}) is None


def test_make_frame_png(cfg, media, tmp_path):
    from PIL import Image
    c = cfg["compose"]
    video.make_frame_png(media["bg"], cfg, tmp_path / "f.png")
    f = Image.open(tmp_path / "f.png")
    assert f.getpixel((540, c["circle_center_y"]))[3] == 0                                   # hole
    assert f.getpixel((540, c["circle_center_y"] - c["circle_diameter"] // 2 + 1))[:3] == (255, 255, 255)  # ring
    assert f.getpixel((10, 10)) == (20, 30, 70, 255)                                          # background


def test_compose_adds_silence_when_source_has_no_audio(cfg, media, tmp_path):
    video.make_frame_png(media["bg"], cfg, tmp_path / "f.png")
    out = tmp_path / "base.mp4"
    video.compose_base(media["src15_noaudio"], tmp_path / "f.png", cfg, out, 23, "veryfast")
    p = video.probe(out)
    assert p["audio"] and abs(p["duration"] - 15) < 0.1
    assert (p["video"]["width"], p["video"]["height"]) == (1080, 1920)


def test_banner_too_wide(cfg, tmp_path):
    from PIL import Image
    Image.new("RGBA", (2000, 300)).save(tmp_path / "wide.png")
    cfg["banners"]["top"] = str(tmp_path / "wide.png")
    with pytest.raises(ValueError, match="too wide"):
        video.banner_size(cfg)


@pytest.fixture
def voiced(cfg, media, tmp_path):
    out = tmp_path / "reel.mp4"
    return out, video.build(media["src25"], cfg["accounts"][0], cfg, out)


def mean_volume(path, start, dur, freq):
    p = subprocess.run(["ffmpeg", "-ss", str(start), "-t", str(dur), "-i", str(path), "-vn", "-af",
                        f"bandpass=f={freq}:width_type=h:w=50,volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", p.stderr).group(1))


def test_build_voiced(voiced, cfg):
    out, meta = voiced
    assert meta["banner_type"] == "voiced" and meta["midroll_at"] == 12.0 and meta["midroll_dur"] == 3.0
    assert abs(video.probe(out)["duration"] - 28) < 0.1                       # 25 s + 3 s midroll, no speed-up
    f = lambda t: video.grab_frame(out, t)                                    # noqa: E731
    still = (250, 1300, 830, 1450)                                            # inside circle, below midroll box
    assert video.region_diff(f(12.5), f(14.5), still) < 1.0                   # frame frozen during midroll
    assert video.region_diff(f(11), f(16), still) > 3.0                       # video moves outside it
    assert video.region_diff(f(13.5), f(11.9), (0, 700, 1080, 1200)) > 30     # midroll visible
    inside, outside = mean_volume(out, 12.2, 2.6, 440), mean_volume(out, 2, 8, 440)
    assert outside - inside >= 15                                             # original sound stops
    assert mean_volume(out, 12.2, 2.6, 880) - mean_volume(out, 2, 8, 880) >= 15  # midroll sound plays
    assert video.validate(out, cfg, 28.0, (12, 15)) == []


def test_validate_banner_mismatch(voiced, cfg, media):
    out, _ = voiced
    cfg["banners"]["top"] = str(media["banner2"])
    assert any("banner" in e for e in video.validate(out, cfg, 28.0, (12, 15)))


def test_validate_wrong_resolution(cfg, media):
    assert any("1080" in e for e in video.validate(media["src25"], cfg, 25.0))


def test_build_short_is_static(cfg, media, tmp_path):
    meta = video.build(media["src8"], cfg["accounts"][0], cfg, tmp_path / "s.mp4")
    assert meta["banner_type"] == "static" and meta["midroll_at"] is None
    assert abs(meta["duration"] - 8) < 0.1


def test_build_with_text(cfg, media, tmp_path):
    cfg["compose"]["text_overlay"] = True
    meta = video.build(media["src8"], cfg["accounts"][0], cfg, tmp_path / "t.mp4", text="Смотри до конца")
    assert meta["banner_type"] == "static"


def test_failed_rebuild_keeps_previous_file(cfg, media, tmp_path):
    out = tmp_path / "s.mp4"
    video.build(media["src8"], cfg["accounts"][0], cfg, out)
    before = out.read_bytes()
    cfg["compose"]["banner_check_max_diff"] = -1      # force validation to fail
    with pytest.raises(ValueError, match="banner"):
        video.build(media["src8"], cfg["accounts"][0], cfg, out)
    assert out.read_bytes() == before and not list(tmp_path.glob("*.part.mp4"))


def test_build_square_template(cfg, media, tmp_path):
    out = tmp_path / "sq.mp4"
    acc = {**cfg["accounts"][0], "template": "square", "background": ""}
    meta = video.build(media["src8"], acc, cfg, out)
    assert meta["template"] == "square" and meta["background"] == "black"
    f = video.grab_frame(out, 2)
    size, x, y = video.video_box(cfg, "square")
    assert f.getpixel((10, y + size + 50)) == (0, 0, 0) or max(f.getpixel((10, y + size + 50))) < 20  # black below
    assert max(f.getpixel((540, y + size // 2))) > 40                                                  # video inside


def test_square_text_plate_is_below_the_video(cfg, media, tmp_path):
    cfg["compose"]["text_overlay"] = True
    out = tmp_path / "sqt.mp4"
    acc = {**cfg["accounts"][0], "template": "square", "background": ""}
    video.build(media["src8"], acc, cfg, out, text="テスト")
    size, x, y = video.video_box(cfg, "square")
    f = video.grab_frame(out, 2)
    plate = f.crop((0, y + size, 1080, 1920))
    assert max(hi for _, hi in plate.getextrema()) > 100           # the white text is in the black margin


# ---------- addendum 3: banner sets, .mov / chroma key banners, loop rule, safe zone ----------

def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def moving_banner(path, seconds, codec):
    """A red square moving right on a transparent 600x480 canvas: like an animated Playerok banner."""
    bg = "black@0.0" if codec == "qtrle" else "0x00FF00"
    ff("-f", "lavfi", "-i", f"color=c={bg}:s=600x480:r=30:d={seconds},format=rgba",
       "-f", "lavfi", "-i", f"color=c=red:s=200x200:r=30:d={seconds},format=rgba",
       "-filter_complex", "[0][1]overlay=x='t*80':y=140,format=" + ("argb" if codec == "qtrle" else "yuv420p"),
       "-c:v", codec, path)
    return str(path)


def test_active_banner_set_by_date(cfg):
    from datetime import date
    from clips import config
    cfg["banners"]["sets"] = [{"name": "nov", "start": date(2026, 11, 1), "end": date(2026, 11, 30)},
                              {"name": "dec", "start": date(2026, 12, 1), "end": date(2026, 12, 31)}]
    assert config.active_set(cfg, date(2026, 11, 30))["name"] == "nov"
    assert config.active_set(cfg, date(2026, 12, 1))["name"] == "dec"
    assert config.active_set(cfg, date(2027, 1, 1)) is None


def test_build_stops_without_banner_set(cfg, media, tmp_path):
    from clips import config
    config.use_banner_set(cfg, None)
    with pytest.raises(video.NoBannerSet):
        video.build(media["src8"], cfg["accounts"][0], cfg, tmp_path / "x.mp4")


def test_animated_mov_banner_with_transparency(cfg, media, tmp_path):
    cfg["banners"]["top"] = moving_banner(tmp_path / "b.mov", 4, "qtrle")
    out = tmp_path / "m.mp4"
    meta = video.build(media["src25"], cfg["accounts"][0], cfg, out)   # validated inside, midroll included
    assert meta["banner_type"] == "voiced"
    f = video.grab_frame(out, 0.5)
    bw, bh = video.banner_size(cfg)
    x0, y0 = video.banner_x(cfg, bw), cfg["banners"]["top_y"]
    assert f.getpixel((x0 + bw - 5, y0 + 5)) != (0, 0, 0)          # transparent corner shows the background
    cfg["banners"]["top"] = moving_banner(tmp_path / "b2.mov", 3, "qtrle")  # other loop length -> mismatch
    assert any("banner mismatch" in e for e in video.validate(out, cfg, meta["duration"], (12, 15)))


def test_source_shorter_than_banner_loop_is_rejected(cfg, media, tmp_path):
    cfg["banners"]["top"] = moving_banner(tmp_path / "long.mov", 10, "qtrle")
    with pytest.raises(ValueError, match="shorter than the banner"):
        video.build(media["src8"], cfg["accounts"][0], cfg, tmp_path / "x.mp4")


def test_mp4_banner_on_green_is_keyed(cfg, media, tmp_path):
    cfg["banners"]["top"] = moving_banner(tmp_path / "g.mp4", 4, "libx264")
    cfg["banners"]["chroma_key"] = "0x00FF00"
    out = tmp_path / "k.mp4"
    video.build(media["src8"], cfg["accounts"][0], cfg, out)
    bw, _ = video.banner_size(cfg)
    r, g, b = video.grab_frame(out, 1).getpixel((video.banner_x(cfg, bw) + bw - 5, cfg["banners"]["top_y"] + 5))
    assert not (g > 200 and r < 80 and b < 80)                      # no green box around the banner


def test_banner_outside_safe_zone(cfg, tmp_path):
    from PIL import Image
    Image.new("RGBA", (1000, 360)).save(tmp_path / "w.png")
    cfg["banners"]["top"] = str(tmp_path / "w.png")
    with pytest.raises(ValueError, match="safe zone"):
        video.check_banner_place(cfg)
