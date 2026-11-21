"""One-time import of a Playerok banner set (addendum 3).

Playerok ships ProRes 4444 .mov files with transparency, ~100 MB each. They are re-encoded once into
light VP9 WebM files that keep the transparency (frame width, 30 fps), and the sources are deleted:
they are not kept on the server. The printed block goes into config.toml under [banners].
"""
import logging
import os
import re
from pathlib import Path

from clips import config, video

log = logging.getLogger(__name__)


def has_alpha(path) -> bool:
    return "a" in (video.probe(path)["video"].get("pix_fmt") or "").replace("gray", "")


def transcode(src, out, cfg) -> dict:
    """ProRes 4444 (or any video with transparency) -> VP9 WebM with transparency. Returns its file entry."""
    c, info = cfg["compose"], video.probe(src)
    if not has_alpha(src):
        raise ValueError(f"{Path(src).name} has no transparency: use the .mov version, or keep the .mp4 and set "
                         f"its chroma color in the banner set")
    part = Path(out).with_name(Path(out).stem + ".part.webm")
    audio = ["-c:a", "libopus", "-b:a", "128k"] if info["audio"] else ["-an"]
    video.run(["ffmpeg", "-v", "error", "-y", "-i", src,
               "-vf", f"scale={c['width']}:-2,fps={c['fps']},format=yuva420p",
               "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", "30", "-b:v", "0", "-row-mt", "1",
               "-auto-alt-ref", "0", *audio, part])
    dur, src_dur = video.stream_duration(part), video.stream_duration(src)
    probe_cfg = {"banners": {"top": str(part), "files": {}, "top_fit": "width"}, "compose": c}
    alpha = video.banner_frame(probe_cfg, 0).getchannel("A").getextrema()
    if abs(dur - src_dur) > 0.15 or alpha[0] == 255:  # length kept, transparency kept
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{Path(src).name}: re-encoded file is wrong (duration {dur:.2f}s vs {src_dur:.2f}s, "
                           f"alpha range {alpha})")
    os.replace(part, out)
    log.info("%s -> %s (%.1f MB -> %.1f MB)", Path(src).name, out, Path(src).stat().st_size / 2**20,
             Path(out).stat().st_size / 2**20)
    return {"file": str(Path(out).relative_to(config.ROOT)) if Path(out).is_relative_to(config.ROOT) else str(out),
            "duration": round(dur, 2)}


def import_set(cfg, name, top, midrolls, start=None, end=None, keep_source=False) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "set"
    d = config.ROOT / "assets" / "banners" / slug
    d.mkdir(parents=True, exist_ok=True)
    entries = [transcode(top, d / "top.webm", cfg)]
    entries += [transcode(m, d / f"midroll{i}.webm", cfg) for i, m in enumerate(midrolls, 1)]
    if not keep_source:  # Playerok's rule for this project: the heavy sources are not kept on the server
        for f in [top, *midrolls]:
            Path(f).unlink()
            log.info("source deleted: %s", f)
    entry = lambda e: f'{{ file = "{e["file"]}", duration = {e["duration"]} }}'  # noqa: E731
    lines = ["[[banners.sets]]", f'name = "{name}"', 'platform = "instagram"']
    lines += [f"start = {start}"] if start else []
    lines += [f"end = {end}"] if end else []
    lines += [f"top = {entry(entries[0])}", "midrolls = ["] + [f"  {entry(e)}," for e in entries[1:]] + ["]"]
    return "\n".join(lines)
