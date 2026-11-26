"""SQLite: schema, connection, time helpers. Single source of truth."""
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  nick           TEXT PRIMARY KEY,              -- Instagram username, same as config accounts[].nick
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','paused','check','banned')),
  status_reason  TEXT,                           -- e.g. 'checkpoint', 'action_block', 'logged_out', 'manual'
  paused_until   TEXT,                           -- only for status 'paused'
  followers      INTEGER,
  bio_set_at     TEXT,                           -- set-bio done (Task 3.5)
  bio_caption_id INTEGER,
  notion_page_id TEXT,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id          INTEGER PRIMARY KEY,
  shortcode   TEXT NOT NULL UNIQUE,              -- Instagram shortcode of the donor reel
  donor       TEXT NOT NULL,
  url         TEXT NOT NULL,                     -- https://www.instagram.com/reel/<shortcode>/
  video_url   TEXT,                              -- direct mp4 from Apify (expires)
  caption     TEXT,
  views       INTEGER,
  likes       INTEGER,
  duration    REAL,
  posted_at   TEXT,
  fetched_at  TEXT NOT NULL,                     -- last time Apify returned it
  state       TEXT NOT NULL DEFAULT 'new'
              CHECK (state IN ('new','rejected','selected','ready','duplicate','failed')),
  reason      TEXT,                              -- why rejected / failed / waiting
  score       REAL,                              -- views / donor median at selection time
  file_path   TEXT,
  phash       TEXT                               -- space-separated 16-hex-char dHashes of N frames
);
CREATE INDEX IF NOT EXISTS sources_donor_posted ON sources(donor, posted_at);
CREATE INDEX IF NOT EXISTS sources_state ON sources(state);

CREATE TABLE IF NOT EXISTS captions (            -- caption pool, addendum 1a (Task 3.5)
  id          INTEGER PRIMARY KEY,
  lang        TEXT NOT NULL CHECK (lang IN ('japanese','russian')),
  text        TEXT NOT NULL UNIQUE,              -- cleaned text
  source      TEXT,                              -- caption donor username
  source_name TEXT,                              -- caption donor display name
  source_url  TEXT,
  fetched_at  TEXT NOT NULL,
  used_at     TEXT,                              -- NULL = free; each caption is used once in the network
  used_for    TEXT                               -- 'pub:<id>' or 'bio:<nick>'
);
CREATE INDEX IF NOT EXISTS captions_free ON captions(lang, used_at);

CREATE TABLE IF NOT EXISTS publications (
  id               INTEGER PRIMARY KEY,
  source_id        INTEGER REFERENCES sources(id),   -- NULL only for manual `publish-file`
  account          TEXT NOT NULL REFERENCES accounts(nick),
  attempt          INTEGER NOT NULL DEFAULT 1,       -- 1 = first post of this source, 2+ = repost
  state            TEXT NOT NULL DEFAULT 'planned'
                   CHECK (state IN ('planned','built','published','unconfirmed','failed')),
  scheduled_at     TEXT NOT NULL,
  file_path        TEXT,
  background       TEXT,                             -- background file name (for reports)
  banner_type      TEXT CHECK (banner_type IN ('static','voiced')),
  banner_file      TEXT,                             -- top banner file name (for reports)
  caption          TEXT,                             -- '' when the account's caption_lang is 'none'
  caption_id       INTEGER REFERENCES captions(id),
  caption_lang     TEXT,                             -- copied from the account at planning time
  ru_audience_pct  INTEGER,                          -- % of views from Russia, entered by the operator (/geo)
  share_clicked_at TEXT,                             -- set right after clicking Share
  published_at     TEXT,
  ig_shortcode     TEXT UNIQUE,
  ig_url           TEXT,
  error            TEXT,
  last_views       INTEGER,
  growth_48h_pct   REAL,
  submit_status    TEXT CHECK (submit_status IN
                   ('low','candidate','submit_now','submitted','approved','rejected','expired')),
  submit_reason    TEXT,                             -- 'age' | 'stalled' | 'account_in_danger' | 'waiting_next_step'
  notified_at      TEXT,                             -- when "submit now" was sent
  submitted_at     TEXT,
  submitted_views  INTEGER,
  decided_at       TEXT,                             -- approved/rejected time
  amount_rub       INTEGER,
  reject_reason    TEXT,
  needs_screen_recording INTEGER NOT NULL DEFAULT 0,
  notion_page_id   TEXT,
  notion_dirty     INTEGER NOT NULL DEFAULT 1,
  created_at       TEXT NOT NULL,
  UNIQUE (source_id, attempt)                        -- the same source attempt is never planned twice
);
CREATE INDEX IF NOT EXISTS publications_account_sched ON publications(account, scheduled_at);
CREATE INDEX IF NOT EXISTS publications_state ON publications(state);

CREATE TABLE IF NOT EXISTS view_snapshots (
  publication_id INTEGER NOT NULL REFERENCES publications(id),
  taken_at       TEXT NOT NULL,
  views          INTEGER NOT NULL,
  likes          INTEGER,
  PRIMARY KEY (publication_id, taken_at)
);

CREATE TABLE IF NOT EXISTS author_limit (       -- Playerok author limit, one for the whole network (addendum 3)
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  total       INTEGER NOT NULL,                  -- as shown in the bot under "Мои лимиты", entered by the operator
  remaining   INTEGER NOT NULL,
  updated_at  TEXT NOT NULL,
  alerted_at  TEXT                               -- "ask for a higher limit" sent for this entry
);

CREATE TABLE IF NOT EXISTS kick_clips (       -- clipping.net (docs/CLIPPING_PLAN.md): Kick clips of campaign streamers
  id          TEXT PRIMARY KEY,                  -- Kick clip id, or "file:<name>" / "url:<hash>" for add-clip
  campaign    TEXT NOT NULL,                     -- clipping.campaigns[].name
  streamer    TEXT NOT NULL,                     -- Kick channel slug (the KICK.COM/<STREAMER> bar)
  title       TEXT,
  category    TEXT,                              -- Kick category slug
  views       INTEGER,
  duration    REAL,
  created_at  TEXT,
  video_url   TEXT NOT NULL,                     -- HLS playlist, a page for yt-dlp, or a local file
  fetched_at  TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'selected'
              CHECK (state IN ('selected','rejected','ready','duplicate','failed')),
  reason      TEXT,
  score       REAL,
  hook        TEXT,                              -- text above the video; '' = none
  file_path   TEXT,
  phash       TEXT
);
CREATE INDEX IF NOT EXISTS kick_clips_state ON kick_clips(state, campaign);

CREATE TABLE IF NOT EXISTS clip_posts (         -- one Kick clip on one TikTok / YouTube account
  id           INTEGER PRIMARY KEY,
  clip_id      TEXT NOT NULL REFERENCES kick_clips(id),
  account      TEXT NOT NULL,                    -- clipping.accounts[].nick
  platform     TEXT NOT NULL CHECK (platform IN ('tiktok','youtube')),
  campaign     TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'planned' CHECK (state IN ('planned','built','published','failed')),
  scheduled_at TEXT NOT NULL,
  file_path    TEXT,
  caption      TEXT,
  post_url     TEXT UNIQUE,
  published_at TEXT,
  error        TEXT,
  last_views   INTEGER,
  views_at     TEXT,
  notified_at  TEXT,                             -- link sent to the operator for clipping.net
  submitted_at TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (clip_id, account)                      -- rule: one clip once per account
);
CREATE INDEX IF NOT EXISTS clip_posts_account ON clip_posts(account, scheduled_at);

CREATE TABLE IF NOT EXISTS comment_actions (    -- comment moderation log (addendum 3)
  id             INTEGER PRIMARY KEY,
  publication_id INTEGER NOT NULL REFERENCES publications(id),
  author         TEXT,
  text           TEXT,
  rule           TEXT NOT NULL,                  -- which rule matched: link / spam / bait / bot
  deleted_at     TEXT,                           -- NULL in dry-run or when the delete failed
  created_at     TEXT NOT NULL
);

-- Any change of a field mirrored to Notion marks the row dirty. notion-sync clears the flag.
CREATE TRIGGER IF NOT EXISTS publications_notion_dirty
AFTER UPDATE OF state, published_at, ig_url, last_views, growth_48h_pct, submit_status,
                amount_rub, reject_reason, banner_type ON publications
BEGIN
  UPDATE publications SET notion_dirty = 1 WHERE id = NEW.id;
END;
"""

FMT = "%Y-%m-%dT%H:%M:%SZ"

# Columns added after the first schema (addendum 2). Added to existing databases on connect.
MIGRATIONS = [
    ("sources", "donor_group", "TEXT NOT NULL DEFAULT 'memes'"),   # memes | movies
    ("sources", "shares", "INTEGER"),                               # Apify sharesCount, if available
    ("captions", "topic", "TEXT NOT NULL DEFAULT ''"),              # account theme the caption belongs to
    ("publications", "template", "TEXT"),                           # circle | square, for reports
    # addendum 3
    ("accounts", "bound_at", "TEXT"),                               # Playerok binding confirmed by the operator
    ("accounts", "settings_check", "TEXT"),                         # JSON {check: ok | problem | unknown}
    ("accounts", "settings_checked_at", "TEXT"),
    ("publications", "is_bind", "INTEGER NOT NULL DEFAULT 0"),      # the reel carrying the binding code
    ("publications", "last_likes", "INTEGER"),
    ("publications", "last_comments", "INTEGER"),
    ("publications", "comments_checked_at", "TEXT"),
    ("publications", "midroll_file", "TEXT"),                       # which midroll variant (they take turns)
]


def missing_columns(con) -> list:
    return [m for m in MIGRATIONS if m[1] not in {r[1] for r in con.execute(f"PRAGMA table_info({m[0]})")}]


def missing_tables(con) -> list[str]:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [t for t in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA) if t not in have]


def migrate(con) -> None:
    for table, column, decl in missing_columns(con):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.commit()


def connect(path, readonly=False) -> sqlite3.Connection:
    """Read-only (dry-run) never creates or writes the file: missing DB -> empty in-memory copy."""
    if readonly:
        if Path(path).exists():
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
            if missing_columns(con) or missing_tables(con):  # older DB: migrate an in-memory copy, never the file
                mem = sqlite3.connect(":memory:")
                con.backup(mem)
                con.close()
                con = mem
                con.executescript(SCHEMA)
                migrate(con)
        else:
            con = sqlite3.connect(":memory:")
            con.executescript(SCHEMA)
            migrate(con)
        con.execute("PRAGMA query_only = ON")  # any write in dry-run fails loudly, copy or not
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        return con
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    migrate(con)
    return con


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def set_account_status(con, nick, status, reason=None, paused_until=None):
    with con:
        con.execute("UPDATE accounts SET status=?, status_reason=?, paused_until=?, updated_at=? WHERE nick=?",
                    (status, reason, paused_until, now_iso(), nick))


def resume_paused_accounts(con) -> list[str]:
    now = now_iso()
    nicks = [r["nick"] for r in con.execute(
        "SELECT nick FROM accounts WHERE status='paused' AND paused_until <= ?", (now,))]
    for n in nicks:
        set_account_status(con, n, "active")
    return nicks


def sync_accounts(con, cfg) -> None:
    """Config holds static account fields, the DB holds runtime state. Never deletes, never resets status."""
    import logging
    with con:
        for a in cfg["accounts"]:
            con.execute("INSERT OR IGNORE INTO accounts (nick, status, updated_at) VALUES (?, 'active', ?)",
                        (a["nick"], now_iso()))
    configured = {a["nick"] for a in cfg["accounts"]}
    for r in con.execute("SELECT nick FROM accounts"):
        if r["nick"] not in configured:
            logging.getLogger(__name__).warning("account %s is in the DB but not in config", r["nick"])
