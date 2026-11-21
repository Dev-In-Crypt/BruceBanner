import subprocess

import pytest
from PIL import Image, ImageDraw

from clips import config, db


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Synthetic sources, background, banners and midroll (ffmpeg lavfi + Pillow)."""
    d = tmp_path_factory.mktemp("media")
    m = {k: d / f"{k}.mp4" for k in ("src25", "src8", "src15_noaudio", "midroll")}
    enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    ff("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=25", "-f", "lavfi",
       "-i", "sine=frequency=440:duration=25", "-shortest", *enc, "-c:a", "aac", m["src25"])
    ff("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=8", "-f", "lavfi",
       "-i", "sine=frequency=440:duration=8", "-shortest", *enc, "-c:a", "aac", m["src8"])
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=15", *enc, m["src15_noaudio"])
    ff("-f", "lavfi", "-i", "smptebars=size=1080x608:rate=30:duration=3", "-f", "lavfi",
       "-i", "sine=frequency=880:duration=3", "-shortest", *enc, "-c:a", "aac", m["midroll"])
    bg = Image.new("RGB", (1080, 1920), (20, 30, 70))
    ImageDraw.Draw(bg).rectangle([0, 1400, 1080, 1920], fill=(30, 90, 90))
    bg.save(d / "bg.png")
    for name, color in (("banner", (200, 40, 200, 255)), ("banner2", (40, 200, 40, 255))):
        b = Image.new("RGBA", (600, 480), (0, 0, 0, 0))  # transparent sticker, like the real banners
        dr = ImageDraw.Draw(b)
        dr.polygon([(300, 0), (380, 170), (600, 200), (420, 320), (480, 480), (300, 380), (120, 480),
                    (180, 320), (0, 200), (220, 170)], fill=color)
        dr.rectangle([150, 200, 450, 280], fill=(255, 255, 255, 255))
        b.save(d / f"{name}.png")
    m.update(bg=d / "bg.png", banner=d / "banner.png", banner2=d / "banner2.png")
    return m


@pytest.fixture
def cfg(tmp_path, media):
    c = config.load(config.ROOT / "config.example.toml", check_files=False)
    c["data"] = tmp_path
    for sub in config.DATA_SUBDIRS:
        (tmp_path / sub).mkdir(exist_ok=True)
    c["banners"]["top"] = str(media["banner"])
    c["banners"]["midroll"] = str(media["midroll"])
    c["banners"]["sets"] = [{"name": "test", "top": c["banners"]["top"], "midroll": c["banners"]["midroll"],
                             "chroma_key": ""}]
    c["accounts"][0]["template"] = "circle"  # most layout tests were written for the circle
    c["accounts"][0]["background"] = str(media["bg"])
    c["compose"]["preset"] = "veryfast"  # speed only; output format is the same
    return c


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "t.db")
