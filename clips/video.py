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

def make_frame_png(background, cfg, out_path):
    """Background with a transparent circle where the video shows through, plus a thin ring."""
    c = cfg["compose"]
    W, H, D, cy = c["width"], c["height"], c["circle_diameter"], c["circle_center_y"]
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


def banner_size(cfg) -> tuple[int, int]:
    """Banner size after scaling to top_height_px (aspect kept, width even). Raises if too wide."""
    b = cfg["banners"]
    if is_image(b["top"]):
        w, h = Image.open(b["top"]).size
    else:
        v = probe(b["top"])["video"]
        w, h = v["width"], v["height"]
    bh = b["top_height_px"]
    bw = round(w * bh / h / 2) * 2
    if bw > cfg["compose"]["width"]:
        raise ValueError(f"banner too wide at top_height_px={bh}: {bw}px; lower top_height_px "
                         f"(not below top_min_height_px)")
    return bw, bh


def banner_x(cfg, bw) -> int:
    return (cfg["compose"]["width"] - bw) // 2


def compose_base(src, frame_png, cfg, out, crf, preset, text=None, workdir=None):
    """Background + video in a circle + top banner sticker (+ optional text plate). One ffmpeg call."""
    c, b = cfg["compose"], cfg["banners"]
    W, H, D, CY, fps = c["width"], c["height"], c["circle_diameter"], c["circle_center_y"], c["fps"]
    info = probe(src)
    dur = info["duration"]
    bw, bh = banner_size(cfg)
    args = ["ffmpeg", "-v", "error", "-y", "-i", src, "-loop", "1", "-framerate", fps, "-i", frame_png]
    args += (["-loop", "1", "-framerate", fps] if is_image(b["top"]) else ["-stream_loop", "-1"]) + ["-i", b["top"]]
    audio = "[0:a]"
    if info["audio"] is None:
        args += ["-f", "lavfi", "-t", dur, "-i", "anullsrc=r=48000:cl=stereo"]
        audio = "[3:a]"
    drawtext = ""
    if text:
        txt = Path(workdir or Path(out).parent) / "overlay.txt"
        txt.write_text(text, encoding="utf-8")  # textfile= avoids escaping user text inside the filter
        drawtext = (f",drawtext=fontfile={c['font_file']}:textfile={txt}:fontsize={c['text_size']}:"
                    f"fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=20:x=(w-text_w)/2:y={c['text_y']}")
    graph = (
        f"[0:v]fps={fps},scale={D}:{D}:force_original_aspect_ratio=increase,crop={D}:{D},setsar=1,"
        f"pad={W}:{H}:{(W - D) // 2}:{CY - D // 2}:black[c];"
        f"[c][1:v]overlay=0:0:shortest=1[f];"
        f"[2:v]fps={fps},scale={bw}:{bh}[b];"
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


def insert_midroll(base, t, cfg, out) -> float:
    """Freeze the frame at t, play the whole voiced banner with its own sound, then continue. Returns M."""
    b, c = cfg["banners"], cfg["compose"]
    fps, box = c["fps"], b["midroll_box"]
    m = stream_duration(b["midroll"])
    pre_in = ["-c:v", "libvpx-vp9"] if b["midroll_has_alpha"] else []
    graph = (
        "[0:v]split=2[v0][v1];[0:a]asplit=2[a0][a1];"
        f"[v0]trim=end={t},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={m}[pre];"
        f"[1:v]fps={fps},scale={box['w']}:-2,setpts=PTS-STARTPTS+{t}/TB[mid];"
        f"[pre][mid]overlay={box['x']}:{box['y']}:eof_action=pass[va];"
        f"[v1]trim=start={t},setpts=PTS-STARTPTS[vb];"
        "[va][vb]concat=n=2:v=1:a=0,format=yuv420p[v];"
        f"[a0]atrim=end={t},asetpts=PTS-STARTPTS[ap];"
        f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=end={m},"
        "asetpts=PTS-STARTPTS[am];"
        f"[a1]atrim=start={t},asetpts=PTS-STARTPTS[aq];"
        "[ap][am][aq]concat=n=3:v=0:a=1[a]")
    run(["ffmpeg", "-v", "error", "-y", "-i", base, *pre_in, "-i", b["midroll"], "-filter_complex", graph,
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
    if not is_image(b["top"]):
        log.info("banner check skipped: animated banner")
        return errors
    bw, bh = banner_size(cfg)
    banner = Image.open(b["top"]).convert("RGBA").resize((bw, bh), Image.LANCZOS)
    mask = banner.getchannel("A").point(lambda x: 255 if x == 255 else 0)  # compare opaque pixels only
    x0 = banner_x(cfg, bw)
    band = (x0, b["top_y"], x0 + bw, b["top_y"] + bh)
    times = [0.5, p["duration"] / 2, p["duration"] - 0.5]
    if midroll_window:
        times.append(sum(midroll_window) / 2)
    for t in times:
        d = region_diff(grab_frame(path, t).crop(band), banner.convert("RGB"), mask=mask)
        if d > c["banner_check_max_diff"]:
            errors.append(f"banner mismatch at t={t:.1f}s: diff={d:.1f}")
    return errors


# ---------- build ----------

def build(src, account: dict, cfg, out, text=None, use_midroll=True) -> dict:
    """Build one reel for one account, validate it, return metadata. Raises ValueError if invalid."""
    c, b = cfg["compose"], cfg["banners"]
    info = probe(src)
    if not info["video"]:
        raise ValueError(f"no video stream in {src}")
    t = midroll_at(info["duration"], b) if use_midroll else None
    out = Path(out)
    part = out.with_name(out.name + ".part.mp4")
    with tempfile.TemporaryDirectory(dir=cfg["data"] / "tmp") as tmp:
        frame = Path(tmp) / "frame.png"
        make_frame_png(account["background"], cfg, frame)
        m = 0.0
        if t is None:
            dur = compose_base(src, frame, cfg, part, c["crf"], c["preset"], text, tmp)
        else:
            base = Path(tmp) / "base.mp4"  # high-quality intermediate, re-encoded once more below
            dur = compose_base(src, frame, cfg, base, 12, "veryfast", text, tmp)
            m = insert_midroll(base, t, cfg, part)
    os.replace(part, out)
    final = dur + m
    problems = validate(out, cfg, final, (t, t + m) if t is not None else None)
    if problems:
        out.unlink(missing_ok=True)
        raise ValueError("; ".join(problems))
    return {"duration": round(final, 3), "midroll_at": t, "midroll_dur": round(m, 3),
            "banner_type": "voiced" if t is not None else "static",
            "banner_file": Path(b["top"]).name, "background": Path(account["background"]).name}
