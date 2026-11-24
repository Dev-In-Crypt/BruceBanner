"""Caption pool (owner addendum 1a): harvest from popular accounts, clean, use each caption once."""
import json
import logging
import re
from pathlib import Path

from clips import db, trends

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+|www\.\S+")
MENTION_RE = re.compile(r"@[\w.]+")
HASHTAG_RE = re.compile(r"#\S+")
BRACKET_RE = re.compile(r"【[^】]*】|\[[^\]]*\]")
SCRIPT = {"japanese": re.compile(r"[぀-ヿ一-鿿]"), "russian": re.compile(r"[а-яё]", re.I)}


def has_brand(s: str, words) -> bool:
    s = s.lower()
    return any(w.lower() in s for w in words if w)


def clean_caption(text, lang, brand_words, cfg) -> str | None:
    """Cleaned caption, or None if unusable. Brand names are removed from the first line."""
    c = cfg["captions"]
    t = MENTION_RE.sub("", URL_RE.sub("", text or ""))
    t = HASHTAG_RE.sub(lambda m: "" if not c["keep_hashtags"] or has_brand(m[0], brand_words) else m[0], t)
    t = BRACKET_RE.sub(lambda m: "" if has_brand(m[0], brand_words) else m[0], t)  # e.g. 【ディズニー公式】
    lines = "\n".join(" ".join(line.split()) for line in t.splitlines()).strip().split("\n")
    if has_brand(lines[0], brand_words):
        if len(lines) == 1:
            return None
        lines = lines[1:]
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    letters = [ch for ch in t if ch.isalpha()]
    if not c["min_chars"] <= len(t) <= 2200 or t.count("#") > 30 or not letters:
        return None  # Instagram limits: 2200 chars, 30 hashtags
    if sum(bool(SCRIPT[lang].match(ch)) for ch in letters) / len(letters) < 0.5:
        return None  # mostly not the expected language
    return None if trends.blocked_reason(t, "", cfg) else t


def languages_in_use(cfg) -> list[str]:
    return sorted({a["caption_lang"] for a in cfg["accounts"]} - {"none"})


def pools_in_use(cfg) -> list[tuple[str, str]]:
    """(language, theme) pairs some account posts with (addendum 1b: captions follow the account theme)."""
    return sorted({(a["caption_lang"], a["theme"]) for a in cfg["accounts"] if a["caption_lang"] != "none"})


def harvest(cfg, con, dry, from_file=None) -> dict:
    """Weekly: copy POST captions of popular accounts (never profile descriptions, H10)."""
    c, stats = cfg["captions"], {}
    if not dry:
        backfill_topics(cfg, con)
    for lang, topic in pools_in_use(cfg):
        if from_file:  # a saved dump may mix themes: keep only this theme's donors
            donors = {d.lower() for d in c[f"donors_{lang}"][topic]}
            items = [i for i in json.loads(Path(from_file).read_text(encoding="utf-8"))
                     if (i.get("ownerUsername") or "").lower() in donors]
        else:
            items = trends.reels(cfg, c[f"donors_{lang}"][topic], c["results_per_donor"], c["fresh_days"],
                                 pinned=True)
            trends.dump_raw(cfg, f"captions_{lang}_{topic}", items)
        kept = 0
        for i in items:
            likes = i.get("likesCount")
            if likes is not None and likes != -1 and likes < c["min_likes"]:
                continue
            brands = c["brand_words"] + [i.get("ownerUsername"), i.get("ownerFullName")]
            text = clean_caption(i.get("caption"), lang, brands, cfg)
            if not text:
                continue
            kept += 1
            if not dry:
                with con:
                    con.execute("INSERT OR IGNORE INTO captions (lang, topic, text, source, source_name, source_url,"
                                " fetched_at) VALUES (?,?,?,?,?,?,?)", (lang, topic, text, i.get("ownerUsername"),
                                                                        i.get("ownerFullName"), i.get("url"),
                                                                        db.now_iso()))
        stats[f"{lang}/{topic}"] = {"fetched": len(items), "kept": kept, "free": free_count(con, lang, topic)}
    log.info("harvest-captions: %s%s", stats, " (dry-run)" if dry else "")
    return stats


def backfill_topics(cfg, con) -> int:
    """Captions harvested before themes existed have topic '': give them the theme of their donor account."""
    n = 0
    for lang in ("japanese", "russian"):
        for topic, donors in cfg["captions"].get(f"donors_{lang}", {}).items():
            if not donors:
                continue
            marks = ",".join("?" * len(donors))
            with con:
                n += con.execute(f"UPDATE captions SET topic = ? WHERE topic = '' AND lang = ? "
                                 f"AND lower(source) IN ({marks})",
                                 (topic, lang, *[d.lower() for d in donors])).rowcount
    if n:
        log.info("backfilled the theme of %s older captions", n)
    return n


def free_count(con, lang, topic=None) -> int:
    if topic is None:
        return con.execute("SELECT COUNT(*) FROM captions WHERE lang = ? AND used_at IS NULL", (lang,)).fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM captions WHERE lang = ? AND topic = ? AND used_at IS NULL",
                       (lang, topic)).fetchone()[0]


def take_caption(con, lang, used_for, topic="") -> tuple[int | None, str] | None:
    """Reserve a random free caption of this language and theme. ('none' -> (None, '')). None = pool empty."""
    if lang == "none":
        return None, ""
    row = con.execute("SELECT id, text FROM captions WHERE lang = ? AND topic = ? AND used_at IS NULL "
                      "ORDER BY random() LIMIT 1", (lang, topic)).fetchone()
    if row is None:
        return None
    cur = con.execute("UPDATE captions SET used_at = ?, used_for = ? WHERE id = ? AND used_at IS NULL",
                      (db.now_iso(), used_for, row["id"]))
    return (row["id"], row["text"]) if cur.rowcount == 1 else take_caption(con, lang, used_for, topic)


def bio_candidates(con, cfg, lang, topic=""):
    """Free captions of the account theme usable as a bio: short, no hashtags, no brand anywhere (H10)."""
    brands = cfg["captions"]["brand_words"]
    for r in con.execute("SELECT id, text, source, source_name FROM captions "
                         "WHERE lang = ? AND topic = ? AND used_at IS NULL ORDER BY random()", (lang, topic)).fetchall():
        bio = re.sub(r"\n{3,}", "\n\n", HASHTAG_RE.sub("", r["text"])).strip()
        if 0 < len(bio) <= cfg["captions"]["bio_max_chars"] and \
                not has_brand(bio, brands + [r["source"], r["source_name"]]):
            yield r["id"], bio


def reserve_bio(con, cfg, lang, nick, topic="") -> tuple[int, str] | None:
    """Reserve before the browser starts: the planner takes captions without the browser lock."""
    for cid, bio in bio_candidates(con, cfg, lang, topic):
        with con:
            cur = con.execute("UPDATE captions SET used_at = ?, used_for = ? WHERE id = ? AND used_at IS NULL",
                              (db.now_iso(), f"bio:{nick}", cid))
        if cur.rowcount == 1:  # 0 = the planner took it a moment ago, try the next one
            return cid, bio
    return None


def release_bio(con, caption_id):
    with con:
        con.execute("UPDATE captions SET used_at = NULL, used_for = NULL WHERE id = ?", (caption_id,))
