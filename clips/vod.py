"""Long campaign videos (a brief's YouTube link) -> short clips, picked and hooked by an LLM (docs/CLIPPING_PLAN.md).

The video is downloaded once, transcribed with timestamps (faster-whisper, already used for the speech check),
and the model (OpenAI, [clipping.vod]) picks the moments where the campaign's subject is the focus and writes a hook that names the subject
(clipping.net briefs ban generic "I CAN'T BELIEVE THAT HAPPENED" posts). Each moment is cut and goes into
kick_clips like a Kick clip, so the same checks, plan and posting apply. With review = true the AI hook waits in
`title` until the operator approves it (`clip-approve`), because a clip without a hook is never planned.
"""
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import openai

from clips import config, db, telegram, trends

log = logging.getLogger(__name__)
SCHEMA = {
    "type": "object",
    "properties": {"moments": {"type": "array", "items": {
        "type": "object",
        "properties": {"start": {"type": "number"}, "end": {"type": "number"}, "hook": {"type": "string"},
                       "why": {"type": "string"}, "score": {"type": "integer"}},
        "required": ["start", "end", "hook", "why", "score"], "additionalProperties": False}}},
    "required": ["moments"], "additionalProperties": False,
}
PROMPT = """You are the editor of a viral short-video page. From a long video you pick moments to post as vertical \
clips (TikTok, YouTube Shorts) for a paid clipping campaign about {subject}. Clips are paid per view, so only \
moments that make a stranger stop scrolling are worth posting.

Campaign brief rules: {rules} Posts that add no value ("random moment + I CAN'T BELIEVE THAT HAPPENED") are \
disqualified. No gambling, drugs, politics or violence.

About the content: {about}

The best clip is a mini story with a setup, a conflict and a payoff, not an isolated line. What stops the scroll, \
strongest first: big money and luxury, conflict (an argument, a chase, a refusal, a deal falling apart), the \
funniest or most outrageous moment, a surprising reveal, a savage or bold line, a celebrity moment. Skip small \
talk, filler and low-stakes chatter even if it is pleasant.

Pick {n} moments. Each moment:
- {min_s}-{max_s} seconds, start and end on the transcript timestamps below, self-contained: a viewer with no \
context understands it, it opens on the hook moment (no slow lead-in) and ends on the payoff;
- hook: the on-screen text above the video, in {language}, at most 60 characters{hook_rule}. Write it like the top \
hooks on TikTok: stakes or curiosity in the first three words, the single most striking true fact of the clip (a \
dollar amount, the boldest quote, what is at stake), something the viewer wants to see resolved. Never use flat \
verbs like compares, considers, weighs, explains, discusses, talks about. Only facts that happen in the clip: no \
invented numbers. No emoji, no dashes, no swearing;
- why: one short sentence on why a stranger would watch to the end;
- score: 1-10, how likely this clip gets views (10 = everyone stops scrolling). Be strict.
Moments must not overlap. Prefer the strongest moments over even coverage of the video.

Video title: {title}
Chapters: {chapters}

Transcript ([start-end] seconds):
{transcript}"""


def video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", url)
    return m.group(1) if m else re.sub(r"\W", "_", Path(url).stem if Path(url).is_file() else url)[-32:]


def download(cfg, url, out_dir: Path) -> tuple[Path, dict]:
    """Whole video + its metadata. On a datacenter IP YouTube asks to sign in: set download.proxy.
    A local file (a brief's source from MediaSilo, Drive...) is used as it is."""
    if Path(url).is_file():
        return Path(url), {"title": Path(url).stem, "chapters": []}
    proxy = cfg["download"]["proxy"]
    base = [sys.executable, "-m", "yt_dlp", "-q", *(["--proxy", proxy] if proxy else [])]
    info = json.loads(subprocess.run(base + ["--skip-download", "--dump-json", url], check=True,
                                     capture_output=True, text=True, timeout=300).stdout)
    path = out_dir / f"vod_{video_id(url)}.mp4"
    if not path.exists():
        subprocess.run(base + ["-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
                               "--merge-output-format", "mp4", "-o", str(path), url],
                       check=True, capture_output=True, timeout=3600)
    return path, info


def transcript(path, cfg, language) -> list[tuple[float, float, str]]:
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le",
                          "-"], capture_output=True, check=True).stdout
    segs, _ = trends._whisper(cfg["filter"]["speech_model"]).transcribe(
        np.frombuffer(raw, np.float32), language=language, beam_size=1, vad_filter=True)
    return [(round(s.start, 1), round(s.end, 1), s.text.strip()) for s in segs]


def pick_moments(cfg, camp, title, chapters, segs) -> list[dict]:
    """The model's moments, checked: length in range, inside the video, hook names the subject, no overlaps."""
    v = cfg["clipping"]["vod"]
    text = "\n".join(f"[{a:.1f}-{b:.1f}] {t}" for a, b, t in segs)
    named = camp.get("hook_names_subject", True)  # some briefs require the subject in the on-screen text
    prompt = PROMPT.format(subject=camp["subject"], n=v["moments"], min_s=v["min_sec"], max_s=v["max_sec"],
                           rules=camp.get("rules") or "use only the provided content.", about=camp.get("about", ""),
                           hook_rule=f", names {camp['subject']}" if named else "",
                           language=camp["language"], title=title, transcript=text,
                           chapters=", ".join(f"{c.get('title')} @{int(c.get('start_time', 0))}s" for c in chapters)
                           or "none")
    client = openai.OpenAI(api_key=config.secret("OPENAI_API_KEY"))
    r = client.responses.create(
        model=v["model"], input=prompt, reasoning={"effort": v.get("effort", "medium")},
        text={"format": {"type": "json_schema", "name": "moments", "strict": True, "schema": SCHEMA}})
    if not r.output_text:  # refusal or cut off: nothing to parse
        raise RuntimeError(f"model gave no moments (status {r.status})")
    moments = json.loads(r.output_text)["moments"]
    end_of_video, out = segs[-1][1] if segs else 0, []
    for m in sorted(moments, key=lambda m: m["start"]):
        ok = (v["min_sec"] - 2 <= m["end"] - m["start"] <= v["max_sec"] + 2 and 0 <= m["start"] < m["end"] <= end_of_video + 1
              and (not named or camp["subject"].lower() in m["hook"].lower())
              and (not out or m["start"] >= out[-1]["end"]))
        if ok:
            out.append({**m, "hook": m["hook"].strip()[:90]})
        else:
            log.info("vod: moment dropped %s", m)
    return out


def cut(src, start, end, out) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k",
                    str(out)], check=True, capture_output=True, timeout=600)


def process(cfg, con, dry, only=None) -> dict:
    """Every not yet processed video of every `source = "video"` campaign -> cut clips in state selected
    (kick.download then checks speech, topics and duplicates, as for Kick clips)."""
    counts = {}
    camps = [c for c in cfg["clipping"]["campaigns"] if c.get("source") == "video" and only in (None, c["name"])]
    if camps and not os.environ.get("OPENAI_API_KEY"):  # config.secret would stop the whole kick run
        log.warning("vod: OPENAI_API_KEY is empty, video campaigns skipped")
        return counts
    for camp in camps:
        for url in camp.get("videos", []):
            vid = video_id(url)
            if con.execute("SELECT 1 FROM kick_clips WHERE id LIKE ?", (f"vod:{vid}:%",)).fetchone():
                continue  # done earlier; a new pass needs the rows deleted
            if dry:
                log.info("dry-run: would cut %s for %s", url, camp["name"])
                continue
            try:
                src, info = download(cfg, url, cfg["data"] / "sources")
                segs = transcript(src, cfg, camp["language"])
                moments = pick_moments(cfg, camp, info.get("title", ""), info.get("chapters") or [], segs)
            except Exception as e:  # noqa: BLE001
                log.warning("vod %s failed: %s", url, e)
                telegram.send(f"Не удалось нарезать видео кампании {camp['name']}: {str(e)[:200]}")
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            lines = []
            for m in moments:
                cid = f"vod:{vid}:{int(m['start'])}"
                out = cfg["data"] / "sources" / f"cut_{vid}_{int(m['start'])}.mp4"
                cut(src, m["start"], m["end"], out)
                with con:
                    con.execute(
                        "INSERT OR IGNORE INTO kick_clips (id, campaign, streamer, title, duration, video_url, fetched_at,"
                        " state, score, hook) VALUES (?,?,?,?,?,?,?, 'selected', ?, ?)",
                        (cid, camp["name"], camp["subject"].lower(), m["hook"], round(m["end"] - m["start"], 1),
                         str(out), db.now_iso(), m.get("score", 0), "" if camp.get("review", True) else m["hook"]))
                lines.append(f"{m.get('score', 0)}/10  {cid}  {int(m['start']) // 60}:{int(m['start']) % 60:02d}  "
                             f"{m['hook']}")
            counts["cut"] = counts.get("cut", 0) + len(moments)
            if camp.get("review", True) and lines:
                telegram.send(f"Кампания {camp['name']}: {len(lines)} клипов из «{info.get('title', url)}» ждут "
                              "проверки хуков (clip-approve):\n" + "\n".join(lines))
    log.info("vod: %s", counts)
    return counts


def approve(con, ids: list[str]) -> int:
    """The AI hook (kept in title) becomes the hook, so the clip can be planned."""
    with con:
        return sum(con.execute("UPDATE kick_clips SET hook = title WHERE id = ? AND COALESCE(title, '') != ''",
                               (i,)).rowcount for i in ids)
