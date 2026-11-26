"""Load .env and config.toml, resolve paths, validate. Config is a plain dict."""
import math
import os
import re
import tomllib
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_SUBDIRS = ("raw", "sources", "build", "screenshots", "backups", "locks", "tmp")
CAPTION_LANGS = ("japanese", "russian", "none")
TEMPLATES = ("circle", "square")
DONOR_GROUPS = ("memes", "movies")


def load_env(path=ROOT / ".env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing secret {name}: set it in .env")
    return value


def _abs(p: str) -> str:
    return p if not p or Path(p).is_absolute() else str(ROOT / p)


def load(path=None, check_files=True) -> dict:
    load_env()
    p = Path(path or os.environ.get("CLIPS_CONFIG") or ROOT / "config.toml")
    if not p.exists():
        raise SystemExit(f"config not found: {p} (copy config.example.toml to config.toml)")
    cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    b = cfg["banners"]
    # addendum 3: dated banner sets; an old config with plain top/midroll is one set without dates
    b["sets"] = b.get("sets") or [{"name": "default", "top": b.get("top", ""), "midroll": b.get("midroll", "")}]
    for st in b["sets"]:
        st.setdefault("platform", "instagram")
        mids = st.get("midrolls") or ([st["midroll"]] if st.get("midroll") else [])
        st["top"], st["midrolls"] = banner_file(st["top"]), [banner_file(m) for m in mids]
        for f in [st["top"], *st["midrolls"]]:  # old set-level chroma_key: for .mp4 files of the set
            if st.get("chroma_key") and not f["chroma"] and f["file"].lower().endswith(".mp4"):
                f["chroma"] = st["chroma_key"]
    use_banner_set(cfg, active_set(cfg))
    for a in cfg["accounts"]:
        a.setdefault("template", "square")  # addendum 3: a decorative background is a moderation risk
        a.setdefault("bind_code", "")
        a.setdefault("donor_group", "memes")
        a.setdefault("theme", "")
        a["background"] = _abs(a.get("background", ""))
        a["cover"] = _abs(a.get("cover", ""))
    k = cfg.setdefault("clipping", {})  # clipping.net part (docs/CLIPPING_PLAN.md); absent in old configs
    k.setdefault("campaigns", [])
    k.setdefault("accounts", [])
    if "layout" in k:
        k["layout"]["bar_logo"] = _abs(k["layout"].get("bar_logo", ""))
    cfg["data"] = Path(_abs(cfg["data_dir"]))
    validate(cfg, check_files)
    for d in DATA_SUBDIRS:
        (cfg["data"] / d).mkdir(parents=True, exist_ok=True)
    return cfg


def banner_file(spec) -> dict:
    """A banner file entry: a path, or a table {file, chroma, similarity, blend, duration}."""
    d = {"file": spec} if isinstance(spec, str) else dict(spec)
    d["file"] = _abs(d.get("file", ""))
    d.setdefault("chroma", "")         # background color of an .mp4 without transparency, "" for .mov/.webm/.png
    d.setdefault("similarity", 0.15)   # ffmpeg chromakey tolerance
    d.setdefault("blend", 0.05)
    return d


def today(cfg) -> date:
    return datetime.now(ZoneInfo(cfg.get("timezone", "Europe/Moscow"))).date()


def active_set(cfg, day: date | None = None) -> dict | None:
    """The banner set valid on `day` (first match), or None: then building stops (addendum 3)."""
    day = day or today(cfg)
    return next((st for st in cfg["banners"]["sets"] if st["platform"] == "instagram"
                 and st.get("start", date.min) <= day <= st.get("end", date.max)), None)


def use_banner_set(cfg, st: dict | None) -> None:
    """Copy the set's files into banners.top / banners.midroll, which the build reads. None -> top = ''."""
    st = st or {"name": "", "top": banner_file(""), "midrolls": []}
    mids = [m["file"] for m in st["midrolls"]]
    cfg["banners"].update(top=st["top"]["file"], midrolls=mids, midroll=mids[0] if mids else "", set_name=st["name"],
                          files={f["file"]: f for f in [st["top"], *st["midrolls"]]})


def validate(cfg: dict, check_files=True) -> None:
    errors = []
    b, c, cap = cfg["banners"], cfg["compose"], cfg["captions"]
    nicks = [a["nick"] for a in cfg["accounts"]]
    if len(nicks) != len(set(nicks)):
        errors.append("accounts: duplicate nick")
    for a in cfg["accounts"]:
        if not a.get("adspower_id"):
            errors.append(f"account {a['nick']}: empty adspower_id")
        if not 1 <= a["daily_limit"] <= 3:  # H6: hard cap 3
            errors.append(f"account {a['nick']}: daily_limit must be 1..3, got {a['daily_limit']}")
        if a.get("caption_lang") not in CAPTION_LANGS:
            errors.append(f"account {a['nick']}: caption_lang must be one of {CAPTION_LANGS}")
        elif a["caption_lang"] != "none" and not cap.get(f"donors_{a['caption_lang']}", {}).get(a["theme"]):
            errors.append(f"account {a['nick']}: theme {a['theme']!r} has no donors in "
                          f"captions.donors_{a['caption_lang']}")
        if a["template"] not in TEMPLATES:
            errors.append(f"account {a['nick']}: template must be one of {TEMPLATES}")
        if a["donor_group"] not in DONOR_GROUPS:
            errors.append(f"account {a['nick']}: donor_group must be one of {DONOR_GROUPS}")
        elif a["donor_group"] == "movies" and not cfg["trends"].get("donors_movies"):
            errors.append(f"account {a['nick']}: donor_group movies but trends.donors_movies is empty")
        if check_files and a["template"] == "circle" and not Path(a["background"]).exists():  # file or folder
            errors.append(f"account {a['nick']}: background not found: {a['background']}")
        if check_files and a["cover"] and not Path(a["cover"]).is_file():
            errors.append(f"account {a['nick']}: cover not found: {a['cover']}")
    for st in b["sets"]:
        if st.get("start", date.min) > st.get("end", date.max):
            errors.append(f"banner set {st['name']!r}: start is after end")
        for f in [st["top"], *st["midrolls"]]:
            name = f"banner set {st['name']!r}: {Path(f['file']).name}"
            if f["chroma"] and not re.fullmatch(r"(#|0x)[0-9A-Fa-f]{6}", f["chroma"]):
                errors.append(f"{name}: chroma must look like #13FF08")
            if not check_files:
                continue
            if not Path(f["file"]).is_file():
                errors.append(f"{name}: file not found: {f['file']}")
            elif f.get("duration"):  # catches a wrong or broken file
                from clips.video import stream_duration
                real = stream_duration(f["file"])
                if abs(real - f["duration"]) > 0.15:
                    errors.append(f"{name}: duration {real:.2f}s, config says {f['duration']}s")
    if b["top_min_height_px"] < math.ceil(c["height"] / 6):
        errors.append(f"banners.top_min_height_px must be >= {math.ceil(c['height'] / 6)} (1/6 of the frame)")
    if b.get("top_fit", "height") not in ("height", "width"):
        errors.append("banners.top_fit must be height or width")
    if b.get("top_fit", "height") == "height" and b["top_height_px"] < b["top_min_height_px"]:
        errors.append("banners.top_height_px must be >= banners.top_min_height_px")
    # H8: nothing of ours may cover the banner. The midroll (a Playerok creative itself) may, for its 3 s.
    # With top_fit = "width" the visible banner is measured from its transparency at build time.
    banner_bottom = b["top_y"] + (b["top_height_px"] if b.get("top_fit", "height") == "height" else 0)
    if c["circle_center_y"] - c["circle_diameter"] / 2 < banner_bottom:
        errors.append(f"circle top must be >= {banner_bottom} (below the top banner)")
    if c["text_overlay"] and c["text_y"] < banner_bottom:
        errors.append(f"compose.text_y must be >= {banner_bottom} (below the top banner)")
    if c["circle_diameter"] > c["width"] or c["circle_center_y"] + c["circle_diameter"] / 2 > c["height"]:
        errors.append("circle does not fit the frame")
    if c["square_top_y"] < banner_bottom:
        errors.append(f"compose.square_top_y must be >= {banner_bottom} (below the top banner)")
    if c["square_size"] > c["width"] or c["square_top_y"] + c["square_size"] > c["height"]:
        errors.append("square does not fit the frame")
    if c["text_overlay"]:  # the text plate must never cover the video either
        if c["square_text_y"] < c["square_top_y"] + c["square_size"]:
            errors.append(f"compose.square_text_y must be >= {c['square_top_y'] + c['square_size']} (below the square)")
        if c["square_text_y"] + c["text_size"] > c["height"]:
            errors.append("compose.square_text_y: the text plate does not fit below the square")
    if cfg["playerok"]["submit_by_day"] >= cfg["playerok"]["deadline_days"]:
        errors.append("playerok.submit_by_day must be < deadline_days")
    if cfg["planner"]["window_start"] >= cfg["planner"]["window_end"]:
        errors.append("planner.window_start must be < window_end")
    cm = cfg.get("comments", {})
    for pat in [cm.get("link_pattern", ""), cm.get("bot_name_pattern", ""), *cm.get("spam_patterns", []),
                *cm.get("bait_patterns", [])]:
        try:
            re.compile(pat)
        except re.error as e:
            errors.append(f"comments: bad regex {pat!r}: {e}")
    for pat in cfg["filter"]["blocked_patterns"]:
        try:
            re.compile(pat)
        except re.error as e:
            errors.append(f"filter.blocked_patterns: bad regex {pat!r}: {e}")
    k = cfg["clipping"]
    names = [c["name"] for c in k["campaigns"]]
    for c in k["campaigns"]:
        if c.get("source", "kick") not in ("kick", "video") or c.get("bar", "kick") not in ("kick", "none"):
            errors.append(f"campaign {c['name']}: source must be kick|video, bar kick|none")
        if c.get("source") == "video" and not c.get("subject"):
            errors.append(f"campaign {c['name']}: a video campaign needs subject (who the clips are about)")
    for a in k["accounts"]:
        if a.get("platform") not in ("tiktok", "youtube"):
            errors.append(f"clipping account {a.get('nick')}: platform must be tiktok or youtube")
        if a.get("campaign") not in names:
            errors.append(f"clipping account {a.get('nick')}: campaign {a.get('campaign')!r} not in clipping.campaigns")
    for pat in k.get("blocked_patterns", []):
        try:
            re.compile(pat)
        except re.error as e:
            errors.append(f"clipping.blocked_patterns: bad regex {pat!r}: {e}")
    if errors:
        raise SystemExit("config errors:\n- " + "\n- ".join(errors))
