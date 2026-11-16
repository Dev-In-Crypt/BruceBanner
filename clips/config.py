"""Load .env and config.toml, resolve paths, validate. Config is a plain dict."""
import math
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_SUBDIRS = ("raw", "sources", "build", "screenshots", "backups", "locks", "tmp")
CAPTION_LANGS = ("japanese", "russian", "none")


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
    b["top"], b["midroll"] = _abs(b["top"]), _abs(b["midroll"])
    for a in cfg["accounts"]:
        a["background"] = _abs(a["background"])
    cfg["data"] = Path(_abs(cfg["data_dir"]))
    validate(cfg, check_files)
    for d in DATA_SUBDIRS:
        (cfg["data"] / d).mkdir(parents=True, exist_ok=True)
    return cfg


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
        elif a["caption_lang"] != "none" and not cap.get(f"donors_{a['caption_lang']}"):
            errors.append(f"account {a['nick']}: caption_lang {a['caption_lang']} but captions.donors_{a['caption_lang']} is empty")
        if check_files and not Path(a["background"]).is_file():
            errors.append(f"account {a['nick']}: background not found: {a['background']}")
    if check_files and not Path(b["top"]).is_file():
        errors.append(f"banners.top not found: {b['top']}")
    if check_files and b["midroll"] and not Path(b["midroll"]).is_file():
        errors.append(f"banners.midroll not found: {b['midroll']}")
    if b["top_min_height_px"] < math.ceil(c["height"] / 6):
        errors.append(f"banners.top_min_height_px must be >= {math.ceil(c['height'] / 6)} (1/6 of the frame)")
    if b["top_height_px"] < b["top_min_height_px"]:
        errors.append("banners.top_height_px must be >= banners.top_min_height_px")
    banner_bottom = b["top_y"] + b["top_height_px"]  # H8: nothing may cover the banner
    if b["midroll_box"]["y"] < banner_bottom:
        errors.append(f"banners.midroll_box.y must be >= {banner_bottom} (below the top banner)")
    if c["circle_center_y"] - c["circle_diameter"] / 2 < banner_bottom:
        errors.append(f"circle top must be >= {banner_bottom} (below the top banner)")
    if c["text_overlay"] and c["text_y"] < banner_bottom:
        errors.append(f"compose.text_y must be >= {banner_bottom} (below the top banner)")
    if c["circle_diameter"] > c["width"] or c["circle_center_y"] + c["circle_diameter"] / 2 > c["height"]:
        errors.append("circle does not fit the frame")
    if cfg["playerok"]["submit_by_day"] >= cfg["playerok"]["deadline_days"]:
        errors.append("playerok.submit_by_day must be < deadline_days")
    if cfg["planner"]["window_start"] >= cfg["planner"]["window_end"]:
        errors.append("planner.window_start must be < window_end")
    for pat in cfg["filter"]["blocked_patterns"]:
        try:
            re.compile(pat)
        except re.error as e:
            errors.append(f"filter.blocked_patterns: bad regex {pat!r}: {e}")
    if errors:
        raise SystemExit("config errors:\n- " + "\n- ".join(errors))
