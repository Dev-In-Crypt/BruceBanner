"""ffprobe/ffmpeg helpers, reel composition (spec module 7) and validation (module 8)."""
import io
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

log = logging.getLogger(__name__)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


class NoBannerSet(ValueError):
    """No Playerok banner set is valid today: building stops until a new set is in the config."""


def run(cmd: list[str]) -> str:
    log.debug("run: %s", " ".join(map(str, cmd)))
    p = subprocess.run(list(map(str, cmd)), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {' '.join(map(str, cmd))}\n{p.stderr[-2000:]}")
    return p.stdout + p.stderr


def probe(path) -> dict:
    j = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]))
    first = lambda kind: next((s for s in j["streams"] if s["codec_type"] == kind), None)  # noqa: E731
    return {"duration": float(j["format"]["duration"]), "size": int(j["format"]["size"]),
            "video": first("video"), "audio": first("audio")}


def stream_duration(path) -> float:
    p = probe(path)
    return float(p["video"].get("duration") or p["duration"])


def grab_frame(path, t) -> Image.Image:
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"cannot grab frame at {t}s from {path}: {p.stderr[-500:]!r}")
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


def region_diff(a, b, box=None, mask=None) -> float:
    """Mean absolute RGB difference (0-255), optionally inside box and only where mask is 255."""
    if box:
        a, b = a.crop(box), b.crop(box)
    return sum(ImageStat.Stat(ImageChops.difference(a, b), mask=mask).mean) / 3


# ---------- layout ----------

def video_box(cfg, template) -> tuple[int, int, int]:
    """(size, x, y) of the square area the source video is cropped into, for both templates."""
    c = cfg["compose"]
    if template == "square":
        return c["square_size"], (c["width"] - c["square_size"]) // 2, c["square_top_y"]
    d = c["circle_diameter"]
    return d, (c["width"] - d) // 2, c["circle_center_y"] - d // 2


def make_frame_png(background, cfg, out_path, template="circle"):
    """Frame drawn over the video: a transparent hole where the video shows through.

    circle: account background with a round hole and a thin ring (spec module 7).
    square: plain black with a square hole (addendum 7b, for horizontal film/series cuts).
    """
    c = cfg["compose"]
    W, H = c["width"], c["height"]
    if template == "square":
        size, x, y = video_box(cfg, "square")
        frame = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        ImageDraw.Draw(frame).rectangle([x, y, x + size - 1, y + size - 1], fill=(0, 0, 0, 0))
        frame.save(out_path)
        return
    D, cy = c["circle_diameter"], c["circle_center_y"]
    bg = ImageOps.fit(Image.open(background).convert("RGBA"), (W, H))
    s = 4  # supersampling for smooth edges
    box = [(W - D) // 2 * s, (cy - D // 2) * s, (W + D) // 2 * s, (cy + D // 2) * s]
    mask = Image.new("L", (W * s, H * s), 255)
    ImageDraw.Draw(mask).ellipse(box, fill=0)
    bg.putalpha(mask.resize((W, H), Image.LANCZOS))
    if c["circle_border_px"]:
        ring = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse(box, outline=c["circle_border_color"], width=c["circle_border_px"] * s)
        bg = Image.alpha_composite(bg, ring.resize((W, H), Image.LANCZOS))
    bg.save(out_path)


def is_image(path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXT


def dec(path) -> list[str]:
    """Decoder option: VP9 WebM keeps its transparency only with libvpx (ffmpeg's own decoder drops it)."""
    return ["-c:v", "libvpx-vp9"] if Path(path).suffix.lower() == ".webm" else []


def banner_size(cfg) -> tuple[int, int]:
    """Banner size in the reel (width even). top_fit "height": scaled to top_height_px, raises if too wide;
    "width": scaled to the frame width (Playerok's full-width canvases, addendum 3)."""
    b = cfg["banners"]
    if is_image(b["top"]):
        w, h = Image.open(b["top"]).size
    else:
        v = probe(b["top"])["video"]
        w, h = v["width"], v["height"]
    if b.get("top_fit", "height") == "width":
        W = cfg["compose"]["width"]
        return W, round(h * W / w / 2) * 2
    bh = b["top_height_px"]
    bw = round(w * bh / h / 2) * 2
    if bw > cfg["compose"]["width"]:
        raise ValueError(f"banner too wide at top_height_px={bh}: {bw}px; lower top_height_px "
                         f"(not below top_min_height_px)")
    return bw, bh


def banner_x(cfg, bw) -> int:
    return (cfg["compose"]["width"] - bw) // 2


def keyed(cfg, path) -> str:
    """Filter prefix that turns the plain background of a banner without transparency into transparency
    (the file's chroma color and tolerance from the banner set; '' for .mov / .webm / .png with alpha)."""
    f = cfg["banners"].get("files", {}).get(str(path))
    if not f or not f["chroma"]:
        return ""
    return f"chromakey=0x{f['chroma'][-6:]}:{f['similarity']}:{f['blend']},"


def banner_duration(cfg) -> float:
    """One loop of an animated top banner, 0 for a still image or when no banner set is active."""
    top = cfg["banners"]["top"]
    return 0.0 if not top or is_image(top) else stream_duration(top)


def banner_frame(cfg, t=0.0) -> Image.Image:
    """The top banner as drawn in the reel (RGBA, scaled), at time t of its loop."""
    top = cfg["banners"]["top"]
    if is_image(top):
        img = Image.open(top).convert("RGBA")
    else:
        v = probe(top)["video"]
        n, d = map(int, v["r_frame_rate"].split("/"))
        t = min(t, max(0.0, banner_duration(cfg) - 1.5 * d / n))  # seeking past the last frame returns nothing
        p = subprocess.run(["ffmpeg", "-v", "error", *dec(top), "-ss", f"{t:.3f}", "-i", str(top), "-frames:v", "1",
                            "-vf", f"{keyed(cfg, top)}format=rgba", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        if p.returncode != 0 or not p.stdout:
            raise RuntimeError(f"cannot grab banner frame at {t}s: {p.stderr[-300:]!r}")
        img = Image.open(io.BytesIO(p.stdout)).convert("RGBA")
    return img.resize(banner_size(cfg), Image.LANCZOS)


def banner_bbox(cfg) -> tuple[int, int, int, int]:
    """Visible (mostly opaque) part of the banner in reel pixels, over its whole loop."""
    loop, bw = banner_duration(cfg), banner_size(cfg)[0]
    boxes = [banner_frame(cfg, loop * k / 4).getchannel("A").point(lambda a: 255 if a > 128 else 0).getbbox()
             for k in (range(4) if loop else [0])]
    boxes = [b for b in boxes if b]
    if not boxes:
        raise ValueError("the banner is fully transparent: wrong file or wrong chroma color")
    x, y = banner_x(cfg, bw), cfg["banners"]["top_y"]
    return (x + min(b[0] for b in boxes), y + min(b[1] for b in boxes),
            x + max(b[2] for b in boxes), y + max(b[3] for b in boxes))


def check_banner_place(cfg) -> None:
    """The visible banner must be at least top_min_height_px tall (1/6 of the frame) and stay clear of
    Instagram's UI (addendum 3). Raises ValueError."""
    c, b = cfg["compose"], cfg["banners"]
    x0, y0, x1, y1 = banner_bbox(cfg)
    if y1 - y0 < b["top_min_height_px"]:
        raise ValueError(f"visible banner is {y1 - y0}px tall, needs >= {b['top_min_height_px']} (1/6 of the frame)")
    z = c.get("safe_zone")
    if z and (y0 < z["top"] or y1 > c["height"] - z["bottom"] or x0 < z["left"] or x1 > c["width"] - z["right"]):
        raise ValueError(f"visible banner {x0},{y0}-{x1},{y1} leaves the safe zone (top {z['top']}, "
                         f"bottom {z['bottom']}, left {z['left']}, right {z['right']}); move or resize it")


def compose_base(src, frame_png, cfg, out, crf, preset, text=None, workdir=None, template="circle"):
    """Frame + video in a circle or square + top banner sticker (+ optional text plate). One ffmpeg call."""
    c, b = cfg["compose"], cfg["banners"]
    W, H, fps = c["width"], c["height"], c["fps"]
    D, X, Y = video_box(cfg, template)
    info = probe(src)
    dur = info["duration"]
    bw, bh = banner_size(cfg)
    args = ["ffmpeg", "-v", "error", "-y", "-i", src, "-loop", "1", "-framerate", fps, "-i", frame_png]
    args += (["-loop", "1", "-framerate", fps] if is_image(b["top"]) else ["-stream_loop", "-1", *dec(b["top"])])
    args += ["-i", b["top"]]
    audio = "[0:a]"
    if info["audio"] is None:
        args += ["-f", "lavfi", "-t", dur, "-i", "anullsrc=r=48000:cl=stereo"]
        audio = "[3:a]"
    drawtext = ""
    if text:
        txt = Path(workdir or Path(out).parent) / "overlay.txt"
        txt.write_text(text, encoding="utf-8")  # textfile= avoids escaping user text inside the filter
        drawtext = (f",drawtext=fontfile={c['font_file']}:textfile={txt}:fontsize={c['text_size']}:"
                    f"fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=20:x=(w-text_w)/2:y={c['square_text_y'] if template == 'square' else c['text_y']}")
    graph = (
        f"[0:v]fps={fps},scale={D}:{D}:force_original_aspect_ratio=increase,crop={D}:{D},setsar=1,"
        f"pad={W}:{H}:{X}:{Y}:black[c];"
        f"[c][1:v]overlay=0:0:shortest=1[f];"
        f"[2:v]{keyed(cfg, b['top'])}fps={fps},scale={bw}:{bh}[b];"
        f"[f][b]overlay={banner_x(cfg, bw)}:{b['top_y']}:shortest=1{drawtext},format=yuv420p[v];"
        f"{audio}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad[a]")
    run(args + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", dur,
                *_encode_args(cfg, crf, preset), out])
    return dur


def _encode_args(cfg, crf, preset):
    c = cfg["compose"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-r", c["fps"],
            "-c:a", "aac", "-b:a", c["audio_bitrate"], "-movflags", "+faststart"]


# ---------- voiced midroll ----------

def midroll_at(duration: float, b: dict) -> float | None:
    """Insert time in seconds (source timeline), or None if no midroll.

    Short sources (< midroll_short_until_sec): any time except the last 3 s.
    Long sources: inside midroll_long_window_sec. The default short insert (10 s)
    also satisfies the 10-20 s rule in case Playerok measures the final length.
    """
    if not b.get("midroll") or duration < b["midroll_min_source_sec"]:
        return None
    if duration < b["midroll_short_until_sec"]:
        return float(max(1, min(b["midroll_short_insert_sec"], int(duration - 3))))
    lo, hi = b["midroll_long_window_sec"]
    return float(min(max(b["midroll_long_insert_sec"], lo), hi, int(duration - 3)))


def insert_midroll(base, t, cfg, out, midroll) -> float:
    """Freeze the frame at t, play the whole voiced banner with its own sound, then continue. Returns M."""
    b, c = cfg["banners"], cfg["compose"]
    fps, box = c["fps"], b["midroll_box"]
    m = stream_duration(midroll)
    graph = (
        "[0:v]split=2[v0][v1];[0:a]asplit=2[a0][a1];"
        f"[v0]trim=end={t},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={m}[pre];"
        f"[1:v]{keyed(cfg, midroll)}fps={fps},scale={box['w']}:-2,setpts=PTS-STARTPTS+{t}/TB[mid];"
        f"[pre][mid]overlay={box['x']}:{box['y']}:eof_action=pass[va];"
        f"[v1]trim=start={t},setpts=PTS-STARTPTS[vb];"
        "[va][vb]concat=n=2:v=1:a=0,format=yuv420p[v];"
        f"[a0]atrim=end={t},asetpts=PTS-STARTPTS[ap];"
        f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=end={m},"
        "asetpts=PTS-STARTPTS[am];"
        f"[a1]atrim=start={t},asetpts=PTS-STARTPTS[aq];"
        "[ap][am][aq]concat=n=3:v=0:a=1[a]")
    run(["ffmpeg", "-v", "error", "-y", "-i", base, *dec(midroll), "-i", midroll, "-filter_complex", graph,
         "-map", "[v]", "-map", "[a]", *_encode_args(cfg, c["crf"], c["preset"]), out])
    return m


# ---------- validation (module 8) ----------

def validate(path, cfg, expected_duration, midroll_window=None) -> list[str]:
    """Human-readable problems; empty list means the reel may be published (H4)."""
    c, b = cfg["compose"], cfg["banners"]
    if not Path(path).is_file():
        return [f"file not found: {path}"]
    p = probe(path)
    errors = []
    if p["size"] > c["max_file_mb"] * 1024 * 1024:
        errors.append(f"file too big: {p['size'] / 1048576:.1f} MB > {c['max_file_mb']} MB")
    v, a = p["video"], p["audio"]
    if not v:
        return errors + ["no video stream"]
    if v["codec_name"] != "h264":
        errors.append(f"video codec {v['codec_name']}, expected h264")
    if v.get("pix_fmt") != "yuv420p":
        errors.append(f"pix_fmt {v.get('pix_fmt')}, expected yuv420p")
    if (v["width"], v["height"]) != (c["width"], c["height"]):
        errors.append(f"resolution {v['width']}x{v['height']}, expected {c['width']}x{c['height']}")
    if v.get("r_frame_rate") != f"{c['fps']}/1":
        errors.append(f"frame rate {v.get('r_frame_rate')}, expected {c['fps']}/1")
    if not a:
        errors.append("no audio stream")
    elif a["codec_name"] != "aac":
        errors.append(f"audio codec {a['codec_name']}, expected aac")
    if abs(p["duration"] - expected_duration) > 0.25:
        errors.append(f"duration {p['duration']:.2f}s, expected {expected_duration:.2f}s")
    if errors:
        return errors
    bw, bh = banner_size(cfg)
    x0 = banner_x(cfg, bw)
    band = (x0, b["top_y"], x0 + bw, b["top_y"] + bh)
    loop = banner_duration(cfg)
    times = [0.5, p["duration"] / 2, p["duration"] - 0.5]
    if midroll_window:  # during the midroll the Playerok midroll creative may cover the banner
        times = [t if not midroll_window[0] - 0.2 <= t <= midroll_window[1] + 0.2 else midroll_window[1] + 0.5
                 for t in times]
    for t in times:
        base = t  # time in the reel before the midroll was inserted (the frame, banner included, freezes)
        if midroll_window and t >= midroll_window[0]:
            base = midroll_window[0] if t < midroll_window[1] else t - (midroll_window[1] - midroll_window[0])
        # an animated banner: compare with its frame at that point of the loop, +-1 frame for rounding
        frames = [banner_frame(cfg)] if not loop else \
            [banner_frame(cfg, (base + k / c["fps"]) % loop) for k in (-1, 0, 1)]
        reel = grab_frame(path, t).crop(band)
        d = min(region_diff(reel, f.convert("RGB"), mask=f.getchannel("A").point(lambda x: 255 if x == 255 else 0))
                for f in frames)  # opaque banner pixels only
        if d > c["banner_check_max_diff"]:
            errors.append(f"banner mismatch at t={t:.1f}s: diff={d:.1f}")
    return errors


# ---------- build ----------

def save_previews(cfg, reel, name, midroll_window) -> list[str]:
    """Frames for a visual check of the banner edges (chroma key artifacts), kept with the debug files."""
    d = cfg["data"] / "screenshots"
    out = [d / f"preview_{name}_banner.png"]
    grab_frame(reel, 1.0).save(out[0])
    if midroll_window:
        out.append(d / f"preview_{name}_midroll.png")
        grab_frame(reel, sum(midroll_window) / 2).save(out[1])
    return [str(p) for p in out]


def build(src, account: dict, cfg, out, text=None, use_midroll=True, midroll_index=0) -> dict:
    """Build one reel for one account, validate it, return metadata. Raises ValueError if invalid."""
    c, b = cfg["compose"], cfg["banners"]
    if not b["top"]:
        raise NoBannerSet("no Playerok banner set is valid today: add a new set to banners.sets")
    check_banner_place(cfg)
    info = probe(src)
    if not info["video"]:
        raise ValueError(f"no video stream in {src}")
    if info["duration"] < banner_duration(cfg):  # addendum 3: the banner plays in full at least once
        raise ValueError(f"source {info['duration']:.1f}s is shorter than the banner loop {banner_duration(cfg):.1f}s")
    mids = b.get("midrolls") or ([b["midroll"]] if b.get("midroll") else [])
    midroll = mids[midroll_index % len(mids)] if mids and use_midroll else None  # variants 1-3 take turns
    t = midroll_at(info["duration"], b) if midroll else None
    out = Path(out)
    part = out.with_name(out.name + ".part.mp4")
    with tempfile.TemporaryDirectory(dir=cfg["data"] / "tmp") as tmp:
        frame = Path(tmp) / "frame.png"
        template = account.get("template", "square")
        make_frame_png(account.get("background"), cfg, frame, template)
        m = 0.0
        if t is None:
            dur = compose_base(src, frame, cfg, part, c["crf"], c["preset"], text, tmp, template)
        else:
            base = Path(tmp) / "base.mp4"  # high-quality intermediate, re-encoded once more below
            dur = compose_base(src, frame, cfg, base, 12, "veryfast", text, tmp, template)
            m = insert_midroll(base, t, cfg, part, midroll)
    final = dur + m
    problems = validate(part, cfg, final, (t, t + m) if t is not None else None)
    if problems:  # a previous good file at `out` stays untouched
        part.unlink(missing_ok=True)
        raise ValueError("; ".join(problems))
    os.replace(part, out)
    previews = save_previews(cfg, out, out.stem, (t, t + m) if t is not None else None)
    return {"duration": round(final, 3), "midroll_at": t, "midroll_dur": round(m, 3),
            "banner_type": "voiced" if t is not None else "static", "previews": previews,
            "banner_file": Path(b["top"]).name, "midroll_file": Path(midroll).name if t is not None else None,
            "template": template,
            "background": "black" if template == "square" else Path(account["background"]).name}


# ---------- perceptual hash for dedup (module 5) ----------

def dhash(img) -> int:
    """64-bit difference hash: robust to re-encoding and resizing."""
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = g.tobytes()  # one byte per pixel in mode "L"
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def video_hashes(path, n) -> list[int]:
    d = probe(path)["duration"]
    return [dhash(grab_frame(path, d * (i + 1) / (n + 1))) for i in range(n)]


def hash_distance(a: list[int], b: list[int]) -> float:
    return sum((x ^ y).bit_count() for x, y in zip(a, b)) / max(len(a), 1)
