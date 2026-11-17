"""SQLite: schema, connection, time helpers. Single source of truth."""
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

-- Any change of a field mirrored to Notion marks the row dirty. notion-sync clears the flag.
CREATE TRIGGER IF NOT EXISTS publications_notion_dirty
AFTER UPDATE OF state, published_at, ig_url, last_views, growth_48h_pct, submit_status,
                amount_rub, reject_reason, banner_type ON publications
BEGIN
  UPDATE publications SET notion_dirty = 1 WHERE id = NEW.id;
END;
"""

FMT = "%Y-%m-%dT%H:%M:%SZ"


def connect(path, readonly=False) -> sqlite3.Connection:
    """Read-only (dry-run) never creates or writes the file: missing DB -> empty in-memory copy."""
    if readonly:
        if Path(path).exists():
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        else:
            con = sqlite3.connect(":memory:")
            con.executescript(SCHEMA)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        return con
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
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
