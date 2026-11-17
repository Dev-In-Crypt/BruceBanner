# Playerok Clips Pipeline: Implementation Plan

**Goal:** an unattended pipeline on one Ubuntu server that finds trending Russian meme reels, rebuilds them in the Playerok format (account background + video in a circle + Playerok banner + voiced midroll), publishes them to a network of Instagram accounts through AdsPower, tracks views, and tells a human operator in Telegram exactly when to submit each reel to the Playerok bot.

**Architecture:** one small Python package (`clips/`) with one CLI (`python -m clips <command>`). Each spec module is one command that can be run and checked on its own. systemd timers run the commands on a schedule. SQLite is the single source of truth. Notion and Telegram only receive copies of the data.

**Tech stack:** Python 3.12 (Ubuntu 24.04 system Python), ffmpeg/ffprobe 6.1 (Ubuntu package), Pillow, requests, Playwright (sync API, only `connect_over_cdp`, no bundled browsers), yt-dlp, SQLite (stdlib `sqlite3`), config in TOML (stdlib `tomllib`), pytest for tests. External services: AdsPower Local API, Apify (`apify/instagram-reel-scraper`), Telegram Bot API, Notion API.

**Source requirements:** `docs/spec-ru.md` (owner's spec in Russian). This plan implements it. If this plan and the spec disagree, stop and ask the owner.

---

## 0. How to use this document (read first)

### 0.1 Rules for the AI coder

1. Execute phases in order (Phase 0 to Phase 6). Inside a phase, execute tasks in order.
2. Every task has: **Why** (the reason), **Files**, **Steps**, **Acceptance criteria**. A task is done only when every acceptance item passes. Paste the command output of each acceptance item into the task's commit message or PR description.
3. Do not start the next phase until all acceptance criteria of the current phase pass. Phases 2 and 6 need a human (real Instagram account). When you reach a "HUMAN CHECK" item, stop and ask the owner to perform it.
4. Keep it minimal. No classes unless they hold state that functions cannot. No interfaces with one implementation, no plugin systems, no ORMs, no async, no web frameworks. Module-level functions plus plain dicts and `sqlite3.Row` are enough.
5. Every value that the spec calls "unknown" or "changes often" goes into `config.toml`, never into code. Every secret goes into `.env`, never into git, logs, or Telegram messages.
6. Every command accepts `--dry-run`. Dry-run never writes to the DB, never publishes, never sends Telegram messages (prints them instead), never writes to Notion.
7. Every command is idempotent: running it twice in a row must not create duplicate rows, duplicate posts, or duplicate notifications.
8. Write the tests named in each task. Tests must not call real external services (Apify, Telegram, Notion, AdsPower, Instagram). Tests that need ffmpeg are fine (ffmpeg is installed on the server).
9. If an external API behaves differently from this plan (field names, endpoints), trust the real response, update the code and this plan, and note it in the commit message.
10. Do not commit: `.env`, `config.toml`, `data/`, `assets/` (private media), `.venv/`.

### 0.2 Hard rules (never violate, they come from the spec and from Playerok)

| ID | Rule | Where it is enforced |
|----|------|----------------------|
| H1 | The pipeline NEVER deletes or edits a published reel and never touches its banner (Playerok rule: 30 days after payment). There is no delete/edit/archive code for Instagram posts at all. | Task 2.4, acceptance grep |
| H2 | No traffic to Instagram from the server IP inside an account session. Every account session goes through its AdsPower profile (proxy is configured in AdsPower, not in code). | Task 2.2 |
| H3 | Submission to the Playerok Telegram bot is done by a human. Never automate it. | Phase 4 |
| H4 | A reel that fails validation is never published. | Task 1.5, 3.7 |
| H5 | An account with status `check` or `banned` is never opened by the pipeline until the operator resolves it. | Tasks 2.3, 3.6, 3.7 |
| H6 | At most 3 posts per account per day (hard cap, even if config says more). | Task 0.2 config validation |
| H7 | At most one AdsPower profile is open at a time (spec allows 1-3; we use 1 for simplicity and safety). | Task 2.2 browser lock |
| H8 | The top banner is visible for the whole reel (first to last frame, including the midroll), at least 1/6 of the frame height, never darkened or covered (not by the circle, the midroll, or the text plate). "На весь ролик" in the spec means "for the whole duration", NOT "full width". | Tasks 0.2, 1.2, 1.3, 1.5 |
| H9 | Any browser error saves a screenshot + HTML dump and logs the traceback. | Task 2.2 |
| H10 | Never copy profile descriptions (bios) of other accounts, and never put brand names into our bios: Instagram may treat it as impersonating a brand and ban the account. Bios come only from cleaned post captions without any brand word. | Task 3.5 |

### 0.3 Glossary

- **Donor**: an Instagram account we take source videos from.
- **Source**: one donor reel (row in `sources`).
- **Publication**: one source built for one of our accounts and posted (row in `publications`).
- **Top banner**: static (or looping) Playerok banner at the top of every reel.
- **Midroll / voiced banner**: Playerok animation with its own sound, inserted in the middle of reels that are 12 s or longer. Gives 700 RUB per 100k views instead of 400.
- **Operator**: the human who solves Instagram checks and submits reels to the Playerok bot.

---

## 1. Business rules (all values live in `config.toml` `[playerok]`)

Source: owner's spec (`docs/spec-ru.md`). Playerok changes these often, so they are config, not code.

| Rule | Default | Config key |
|------|---------|-----------|
| Rate, top banner only | 400 RUB per 100,000 views | `playerok.rate_static_rub_per_100k` |
| Rate, with voiced midroll | 700 RUB per 100,000 views | `playerok.rate_voiced_rub_per_100k` |
| Minimum paid views | 50,000 | `playerok.min_paid_views` |
| Paid in steps of | 50,000 views | `playerok.step_views` |
| Max payout per reel | 10,000 RUB | `playerok.max_payout_rub` |
| Submit deadline | 21 days after publication | `playerok.deadline_days` |
| "Submit by" day (our safety margin) | day 19 | `playerok.submit_by_day` |
| Stall threshold | growth < 5% over the last 48 h | `playerok.stall_growth_pct_48h` |
| Screen recording needed | 500,000+ views | `playerok.screen_recording_views` |
| Midroll needed | source 12 s or longer | `banners.midroll_min_source_sec` |
| Midroll position, source shorter than 20 s | any time except the last 3 s | `banners.midroll_short_insert_sec` |
| Midroll position, source 20 s or longer | between second 10 and 20 | `banners.midroll_long_window_sec`, `banners.midroll_long_insert_sec` |
| Top banner minimum height | 1/6 of 1920 = 320 px | `banners.top_min_height_px` |
| Top banner layout | transparent sticker, centered, 280 px from the top, 360 px high (section 1.1) | `banners.top_y`, `banners.top_height_px` |
| Posts per account per day | start 1-2, max 3 | `accounts[].daily_limit`, hard cap 3 |
| Audience | Russian-speaking. For 500k+ reels Playerok checks the views by country; a foreign audience cuts the payout (owner's note, addendum 1a) | `/geo` records (Task 4.4), pilot comparison (Task 6.3) |

Payout formula (implemented in Task 4.2):

```
paid_views = floor(views / step_views) * step_views      (0 if views < min_paid_views)
payout     = min(paid_views * rate / 100_000, max_payout_rub)
```

Examples: 49,999 views = 0 RUB. 50,000 static = 200 RUB. 120,000 voiced = 700 RUB. 2,000,000 voiced = 10,000 RUB (cap).

### 1.1 Reference layout (measured from two competitor reels, November 2026)

The owner shared phone screenshots of two working Playerok reels by other accounts. Measured in pixels and converted to our 1080x1920 frame (Instagram shows the reel with aspect-fill, so values are +/- 5%):

| Element | Example 1 (plain Playerok logo) | Example 2 (GTA VI campaign banner) | Our default |
|---------|------|------|------|
| Background | static photo (night sky, desert), full frame | stylized art, full frame | account image, full frame |
| Banner shape | transparent sticker, NOT a strip | transparent sticker, NOT a strip | transparent PNG sticker |
| Banner position | centered, top edge ~290 px | centered, top edge ~245 px | centered, `top_y = 280` |
| Banner size | ~380 x 290 px (35% width, ~15% height) | ~540 x 480 px (50% width, 25% height) | height `top_height_px = 360` (19%), width from aspect |
| Circle diameter | ~926 px (86% width) | ~996 px (92% width) | `circle_diameter = 940` |
| Circle center y | ~1116 px | ~1226 px | `circle_center_y = 1160` |
| Circle border | thin white ring, ~2 px | thin light ring | `circle_border_px = 3`, white |
| Banner vs circle | ~85 px gap | touching, no overlap | gap >= 0 enforced by config validation |
| Audio label | "Original audio" | "Original audio" | audio embedded in the file |
| Caption | Japanese text unrelated to the video | Japanese text + Japanese hashtags, unrelated | language per account (`japanese` / `russian` / `none`); captions harvested weekly from popular accounts, each used once (Task 3.5) |

What this changes compared with a naive reading of the spec:
- The banner is a centered sticker below Instagram's own top overlay (the "Reels"/"Posts" header and author line cover roughly the top 0-280 px on a phone), not a full-width strip at y = 0.
- Both examples keep the circle below the banner without overlapping it. Our config validation enforces this.
- The source video inside the circle looks soft in both examples: a center square crop of a 720p vertical video is upscaled about 1.3x. Expected, not a bug. Burned-in subtitles of the source stay visible near the bottom of the circle.
- Example 1 was posted on 15.11.2026 with the plain Playerok logo, while the spec says the GTA VI banner is mandatory in November. Do not copy it until the owner confirms the plain logo is still paid (Owner inputs, item 12).
- The midroll cannot be seen on a still screenshot. Its placement is still an open question (Owner inputs, item 1).
- Captions and bios (owner addendum 1a, `docs/spec-ru.md`): the working accounts use Japanese bios and captions copied from popular Japanese posts (for example Disney Japan posts about Toy Story), unrelated to the video. Why it works is unknown, no public explanation was found. We copy the practice and test it in the pilot: some accounts get Japanese captions, some get none (control group; the owner decided not to use Russian caption donors), and we compare reach and audience geography. The reason to compare: Playerok wants a Russian-speaking audience and checks country stats for 500k+ reels; if Japanese captions pull the reels to a Japanese audience, payouts get cut.

---

## 2. Architecture

### 2.1 Data flow

```
 every 3 h        trends:  Apify reels of donors -> sources (new)
                           select (freshness, views vs donor median, duration, topic filter) -> selected / rejected
                           download (Apify URL, fallback yt-dlp) + dedup (frame hashes) -> ready / duplicate
 hourly           plan:    ready sources -> publications (account, random time slot) -> build video -> validate -> built
 every 10 min     publish: due built publications -> AdsPower profile -> Playwright -> Instagram -> published (+reel URL)
                           health check on every login -> account status + Telegram alert
 daily            stats:   Apify reels of our accounts -> view_snapshots -> submit logic -> Telegram "submit now"
 always on        bot:     Telegram buttons -> submitted / approved+amount / rejected+reason / check resolved
 hourly           notion-sync: dirty rows -> Notion databases
 daily            summary  -> Telegram
 weekly           report   -> Telegram
 daily            maintenance: delete old files, backup DB, disk alert
 weekly           harvest-captions: Apify captions of popular JP/RU accounts -> cleaned -> captions pool (used once each)
```

### 2.2 Repository layout (create exactly these files, nothing more unless a task says so)

```
.gitignore
README.md                     short: what it is, how to install, how to run (Task 0.1)
requirements.txt              runtime deps
requirements-dev.txt          pytest
config.example.toml           full example config with comments (copy to config.toml)
.env.example                  secret names, empty values
clips/
  __init__.py                 empty
  __main__.py                 CLI: argparse subcommands, logging setup, error -> Telegram
  config.py                   load .env + config.toml, validate, ROOT/data paths
  db.py                       SQLite connect, schema, tiny helpers (now_iso, etc.)
  video.py                    ffprobe, frame grab, dHash, compose (module 7), validate (module 8)
  trends.py                   Apify client, fetch trends (2), select (3), topic filter (4), download (6), dedup (5)
  captions.py                 caption pool: harvest, clean, take, bio (addendum 1a)
  planner.py                  queue/planning + build trigger (9)
  adspower.py                 AdsPower Local API: active/start/stop
  ig_selectors.py             ALL Instagram selectors and health-signal texts (and nothing else)
  instagram.py                browser session, health check (11), publish flow (10), reel URL capture
  submit.py                   payout, growth, submit decision (13), stats collection (12)
  telegram.py                 send messages/photos, bot long-polling loop, button handlers (14)
  notion.py                   notion-setup and notion-sync (15)
  reports.py                  daily summary + weekly report (16)
  maintenance.py              cleanup, backup, disk alert (17)
tests/
  conftest.py                 tmp DB fixture, synthetic media fixtures (ffmpeg lavfi)
  test_video.py
  test_trends.py
  test_captions.py
  test_planner.py
  test_instagram.py           pure functions only (health classification, followers parsing)
  test_submit.py
  test_telegram.py
  test_reports.py
deploy/
  systemd/clips@.service      oneshot template: python -m clips %i
  systemd/clips-bot.service   long-running Telegram bot
  systemd/adspower.service    AdsPower headless client
  systemd/clips-*.timer       one timer per scheduled command (Task 6.1)
  journald.conf.d/clips.conf  log size limit
docs/
  spec-ru.md                  owner's spec
  IMPLEMENTATION_PLAN.md      this file
  RUNBOOK.md                  operator/admin notes (Task 6.2)
```

Runtime data (gitignored), all under `data_dir` from config (default `data/`):

```
data/clips.db          SQLite database
data/raw/              Apify JSON dumps (debug, auto-deleted)
data/sources/          downloaded donor videos
data/build/            built reels waiting for publish
data/screenshots/      browser errors and dry-run step screenshots (+ .html dumps)
data/backups/          daily DB backups
data/locks/            flock files
data/tmp/              temp files for ffmpeg
assets/backgrounds/    one image per account (owner provides)
assets/banners/        Playerok banner files (owner provides)
```

### 2.3 Conventions

- **Time:** every timestamp in the DB is UTC text in exactly this format: `YYYY-MM-DDTHH:MM:SSZ` (sortable as text). Use only `db.now_iso()` and `db.parse_iso()`. Local time (config `timezone`, default `Europe/Moscow`) is used only for posting windows and human-facing text, via `zoneinfo`.
- **Config access:** plain dicts: `cfg["playerok"]["deadline_days"]`. No config classes.
- **DB access:** `sqlite3` with `row_factory = sqlite3.Row`. Wrap each logical write in `with con:` (commits or rolls back). WAL mode, `busy_timeout` 30 s (several processes share the DB).
- **Logging:** `logging` to stdout, format `%(asctime)s %(levelname)s %(name)s %(message)s`. systemd sends stdout to journald (journald handles rotation, see Task 6.1). Always log ids: `pub=12 account=nick source=ABC123`.
- **Errors:** a command that fails logs the traceback, sends one Telegram alert (not in dry-run), and exits with code 1. Expected business outcomes (no sources today, account paused) are INFO logs, not errors.
- **Dry-run:** a `dry` boolean is passed down. Guard every side effect with `if not dry:`. `telegram.send()` prints instead of sending when `dry=True`.
- **Language:** code, logs, comments in English. Telegram messages and Notion option names in Russian (the operator is Russian-speaking). Never use the em dash character in any user-facing text.
- **Simplifications:** when you knowingly cut a corner, leave a one-line comment starting with `# ponytail:` that names the limit and the upgrade path.

### 2.4 Database schema (create in Task 0.3, exactly this)

```sql
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
```

Schema changes after the pilot starts: add numbered `ALTER TABLE` steps in `db.py` gated by `PRAGMA user_version`. Not needed before that.

### 2.5 State machines

**sources.state**

```
new --select--> selected --download--> ready      (used by planner)
 |                  |              \-> duplicate  (file deleted)
 |                  \--------------->  failed     (download/probe error)
 |--select--> rejected (reason: blocked_donor | blocked_keyword | duration | too_old)
 \--select--> stays new with reason 'wait:no_baseline' or 'wait:below_threshold' (re-checked next run)
```

**publications.state**

```
planned --build ok--> built --publish ok + URL found--> published
   |                    |  \--Share clicked, URL not found--> unconfirmed --stats job matches caption--> published
   \--build fails-->  failed   \--error before Share / slot too old--> failed
```

A `failed` publication frees its source (the planner may plan the source again). An `unconfirmed` one does NOT (the reel may be live; never risk a double post).

**publications.submit_status** (only for `published`)

```
NULL -> low | candidate | submit_now | expired     (computed daily by stats; submit_now is sticky)
submit_now / candidate --operator "Подано"--> submitted --"Одобрено"+amount--> approved
                                                       \--"Отклонено"+reason--> rejected
```

Russian names (Notion, Telegram): low = "Мало", candidate = "Кандидат", submit_now = "Пора подавать", submitted = "Подан", approved = "Одобрен", rejected = "Отклонён", expired = "Просрочен" (`expired` is an addition to the spec: 50k+ views but the 21-day deadline passed without submission).

**accounts.status**

```
active --action block--> paused (paused_until = now + publish.action_block_pause_hours) --time passes--> active
active --checkpoint / code / selfie / logged out--> check --operator "Проверку прошёл"--> active
active --disabled--> banned (terminal; operator can override with CLI)
```

### 2.6 CLI commands (all accept `--dry-run` and `--verbose`)

| Command | Phase | What it does |
|---------|-------|--------------|
| `init-db` | 0 | Create DB and tables (also done automatically by every command) |
| `sync-accounts` | 2 | Upsert accounts from config into DB (never deletes, never overwrites status) |
| `account-status NICK STATUS [--reason TEXT]` | 2 | Manual status override |
| `build --source PATH --account NICK --out PATH [--no-midroll] [--text TEXT]` | 1 | Build one reel from a local file |
| `validate PATH` | 1 | Validate a built reel, print errors, exit 1 if any |
| `health --account NICK` | 2 | Open profile, run health check, close |
| `publish-file --account NICK --file PATH [--caption TEXT]` | 2 | Publish one local file (manual test) |
| `fetch-trends [--from-file JSON]` | 3 | Apify -> sources |
| `select` | 3 | Selection + topic filter |
| `download` | 3 | Download selected + dedup |
| `trends` | 3 | `fetch-trends` + `select` + `download` |
| `harvest-captions [--from-file JSON]` | 3 | Collect, clean and store captions from popular JP/RU accounts |
| `set-bio --account NICK [--force]` | 3 | Set the profile bio from the caption pool (once per account) |
| `plan` | 3 | Plan today's slots, build and validate them |
| `publish` | 3 | Publish due built publications |
| `stats [--from-file JSON]` | 4 | Collect views, run submit logic, notify, then notion-sync |
| `submit-check` | 4 | Only the submit logic + notifications (no Apify) |
| `bot` | 4 | Long-running Telegram bot |
| `summary` | 4 | Daily summary to Telegram |
| `notion-setup --parent-page ID` | 5 | Create the two Notion databases, print their IDs |
| `notion-sync` | 5 | Push dirty rows to Notion |
| `report` | 5 | Weekly report to Telegram |
| `maintenance` | 5 | Cleanup, backup, disk alert |

---

## Phase 0: Bootstrap

### Task 0.1: Repository skeleton and server prerequisites

**Why:** everything else needs a working Python environment, ffmpeg, and a predictable layout.

**Files:** create `.gitignore`, `README.md`, `requirements.txt`, `requirements-dev.txt`, `clips/__init__.py`, `tests/conftest.py` (empty for now).

**Steps:**

1. `.gitignore`:
   ```
   .env
   config.toml
   data/
   assets/
   .venv/
   __pycache__/
   *.pyc
   .pytest_cache/
   ```
2. `requirements.txt` (no other runtime deps without owner approval):
   ```
   requests>=2.31
   Pillow>=10.0
   playwright>=1.45
   yt-dlp
   ```
   `requirements-dev.txt`:
   ```
   -r requirements.txt
   pytest>=8.0
   ```
   Note: do NOT run `playwright install`. We never launch Playwright's own browsers; we connect to AdsPower's browser over CDP.
3. Server packages (document in README):
   ```bash
   sudo apt update
   sudo apt install -y ffmpeg python3-venv python3-pip sqlite3 fonts-dejavu-core xvfb
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-dev.txt
   ```
4. `README.md`: 20-40 lines: purpose (one paragraph), install commands above, `cp config.example.toml config.toml`, `cp .env.example .env`, `python -m clips --help`, link to `docs/IMPLEMENTATION_PLAN.md` and `docs/RUNBOOK.md`.

**Acceptance criteria:**
- [ ] `ffmpeg -version` prints version 6.x; `python3 --version` prints 3.12+.
- [ ] `.venv/bin/python -c "import requests, PIL, playwright, yt_dlp; print('ok')"` prints `ok`.
- [ ] `git status` does not show `.env`, `config.toml`, `data/`, `assets/`.

### Task 0.2: Config loading and validation

**Why:** spec requires all thresholds, limits, banner paths and timings in config, secrets in `.env`. Validation at startup catches mistakes before they cost a post or an account.

**Files:** create `clips/config.py`, `config.example.toml`, `.env.example`.

**Steps:**

1. `.env.example`:
   ```
   # AdsPower Local API key (AdsPower: Account Management > Settings > Local API > Generate api-key)
   ADSPOWER_API_KEY=
   APIFY_TOKEN=
   TELEGRAM_BOT_TOKEN=
   # Comma-separated Telegram numeric user ids allowed to use the bot and receive alerts
   TELEGRAM_OPERATOR_IDS=
   NOTION_TOKEN=
   NOTION_PUBLICATIONS_DB=
   NOTION_ACCOUNTS_DB=
   ```
2. `config.example.toml` (every key below is used somewhere in this plan; keep the comments):
   ```toml
   timezone = "Europe/Moscow"
   data_dir = "data"                       # relative to repo root, or absolute

   [playerok]
   rate_static_rub_per_100k = 400
   rate_voiced_rub_per_100k = 700
   min_paid_views = 50000
   step_views = 50000
   max_payout_rub = 10000
   deadline_days = 21                      # counted from publication time (owner: confirm the reference point)
   submit_by_day = 19
   stall_growth_pct_48h = 5.0
   screen_recording_views = 500000
   wait_for_next_step = true               # stalled but projected to cross next 50k step within 48 h -> wait

   [banners]
   top = "assets/banners/top.png"          # transparent PNG sticker (or MP4/MOV, looped). Nov 2026: GTA VI banner
   top_y = 280                             # px from top edge; below Instagram's own header (section 1.1)
   top_height_px = 360                     # banner is scaled to this height, width keeps aspect, centered
   top_min_height_px = 320                 # >= 1920/6, never lower
   midroll = "assets/banners/voiced.mp4"   # voiced banner animation; must have an audio stream; "" disables
   midroll_has_alpha = false               # true if midroll is a VP9 WebM with transparency
   midroll_box = { x = 0, y = 660, w = 1080 }   # where the animation is drawn on the frozen frame; height keeps aspect
   midroll_min_source_sec = 12
   midroll_short_until_sec = 20
   midroll_short_insert_sec = 10           # clamped to <= duration - 3 (10 also satisfies the 10-20 s rule)
   midroll_long_window_sec = [10, 20]
   midroll_long_insert_sec = 12

   [compose]
   width = 1080
   height = 1920
   fps = 30
   circle_diameter = 940
   circle_center_y = 1160
   circle_border_px = 3                    # thin ring around the circle, 0 disables
   circle_border_color = "#FFFFFF"
   crf = 20
   preset = "medium"
   audio_bitrate = "128k"
   max_file_mb = 100
   font_file = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
   text_overlay = false                    # optional text plate (spec module 7, optional)
   text_y = 1660                           # below the circle
   text_size = 56
   banner_check_max_diff = 8.0             # mean abs pixel diff (0-255) between banner and its area in the reel

   [trends]
   donors = ["donor_one", "donor_two"]
   blocked_donors = []
   fresh_days = 3                          # Apify onlyPostsNewerThan for regular fetches
   seed_results = 30                       # reels fetched for a donor with too little history
   results_per_donor = 15
   max_age_hours = 48
   min_views = 100000
   views_to_median_ratio = 2.0
   median_window = 30                      # last N mature reels of the donor
   min_donor_samples = 8
   min_duration_sec = 5
   max_duration_sec = 90

   [filter]
   # Python regexes, matched against caption lowercased with "ё" replaced by "е". Starter list: tune it.
   blocked_patterns = [
     # crime, gangs
     '\bубий', '\bубив', '\bограб', '\bграбеж', '\bтюрьм', '\bзэк', '\bбандит', '\bбанд[аыу]\b', '\bауе\b', '\bкримин', '\bтеррор', '\bстрельб',
     # war, military
     '\bвойн[аыуе]\b', '\bвоенн', '\bсво\b', '\bвсу\b', '\bфронт', '\bарми[яи]', '\bмобилиз', '\bракет',
     # politics
     '\bпутин', '\bзеленск', '\bнавальн', '\bпрезидент', '\bвыбор[ыа]\b', '\bполитик', '\bгосдум', '\bмитинг', '\bсанкци', '\bтрамп', '\bкремл', '\bукраин',
     # drugs
     '\bнарко', '\bзакладк', '\bкокаин', '\bгероин', '\bмарихуан', '\bспайс', '\bмеф\b', '\bупорот', '\bобдолб',
     # swearing
     '\bху[йяеи]', '\bпизд', '\bеба', '\bебл', '\bеб[уи]', '\bвыеб', '\bзаеб', '\bуеб', '\bбля', '\bсук[аи]\b', '\bмудак', '\bмудил', '\bзалуп', '\bпид[оа]р', '\bгандон', '\bшлюх',
     # discrimination
     '\bхач(и|ик|ей|ам)?\b', '\bчурк', '\bнигг', '\bжид(ы|ов|овск\w*)?\b', '\bхохл', '\bкацап', '\bмоскал',
     # religion
     '\bбог\b', '\bаллах', '\bиисус', '\bхрист', '\bцерк', '\bмечет', '\bислам', '\bправослав', '\bмусульм', '\bрелиги', '\bмолитв',
     # Japanese (for our own caption pool; plain substrings, Japanese has no word boundaries)
     '政治', '選挙', '首相', '大統領', '戦争', '軍隊', '兵器', '犯罪', '殺人', '暴力', 'ヤクザ', '暴力団',
     '麻薬', '大麻', 'ドラッグ', '差別', '宗教', '神社', '寺院', '教会', '祈り',
   ]

   [dedup]
   frames = 5
   max_distance = 10                       # mean Hamming distance (bits of 64) to count as duplicate

   [download]
   proxy = ""                              # optional proxy for anonymous downloads; never an account proxy

   [planner]
   window_start = "10:00"                  # local time
   window_end = "22:00"
   min_gap_minutes = 120                   # between posts of one account
   lead_minutes = 15                       # do not schedule sooner than now + this
   max_source_age_hours = 96               # do not publish memes older than this
   cross_account_gap_days = 14             # same source never on two accounts within N days
   repost_after_days = 7
   repost_max_views = 50000                # only sources whose every publication got fewer views
   max_reposts = 1

   [publish]
   max_per_run = 3
   max_delay_hours = 6                     # a slot older than this is failed instead of published late
   pause_sec = [1.5, 4.0]                  # random pause between UI actions
   typing_delay_ms = [40, 120]
   warmup_sec = [5, 15]                    # scroll the feed before posting
   between_accounts_sec = [30, 90]
   upload_timeout_sec = 300
   action_block_pause_hours = 48

   [adspower]
   base_url = "http://127.0.0.1:50325"
   headless_browser = true
   request_interval_sec = 1.1              # AdsPower limits some endpoints to 1 request/second

   [apify]
   actor = "apify~instagram-reel-scraper"
   views_field = "videoPlayCount"          # verify against the Instagram app in Task 4.1
   run_timeout_sec = 900
   stats_results_per_account = 100

   [notion]
   version = "2022-06-28"

   [maintenance]
   keep_source_days = 30
   keep_debug_days = 14                    # screenshots, raw Apify dumps
   keep_backups = 14
   min_free_disk_gb = 10

   [captions]
   # Popular accounts whose post captions we copy. Approved by the owner 16.11.2026 (existence checked on
   # HypeAuditor / Social Blade / official sites; first Apify run confirms they are active and Japanese).
   donors_japanese = [
     "disneyjp", "netflixjp", "tokyodisneyresort_official",   # cinema, entertainment
     "voguejapan", "vivi_mag_official",                       # fashion
     "visitjapanjp",                                          # travel
     "kurashiru", "delishkitchen.tv",                         # food
     "onepiece_staff",                                        # anime (ONE PIECE staff account)
     # unverified, add after a manual check: "nationalpark_japan", "nihonryokan", "elle_japan"
   ]
   donors_russian = []                     # owner decision: no Russian caption donors, so no account may use "russian"
   results_per_donor = 30
   fresh_days = 60
   min_likes = 1000                        # only popular posts; hidden likes (-1) count as passing
   keep_hashtags = true
   min_chars = 15
   bio_max_chars = 150                     # Instagram bio limit
   low_pool_alert = 50                     # per language: daily summary warns below this many free captions
   # Removed from the first line of a caption; a bio must not contain any of them anywhere (H10).
   # The caption donor's own username and display name are always added to this list.
   brand_words = ["公式", "official", "オフィシャル", "®", "™", "Disney", "ディズニー", "Pixar", "ピクサー",
                  "Marvel", "マーベル", "Star Wars", "スター・ウォーズ", "Netflix", "ネットフリックス"]

   [[accounts]]
   nick = "example_account"
   adspower_id = "jabc123"                 # AdsPower profile user_id
   background = "assets/backgrounds/example_account.jpg"
   daily_limit = 1
   caption_lang = "japanese"               # japanese | russian | none (post captions and bio)
   overlay_texts = ["Смотри до конца"]     # optional text plate on the video (Russian, for viewers)
   ```
3. `clips/config.py`:
   - `ROOT = Path(__file__).resolve().parent.parent`.
   - `load_env(path)`: tiny `.env` parser (skip blank and `#` lines, split on first `=`, strip quotes, `os.environ.setdefault`). No python-dotenv.
   - `load(path=None) -> dict`: `load_env(ROOT/".env")`, read `CLIPS_CONFIG` env var or `ROOT/"config.toml"` with `tomllib`, resolve relative paths (`data_dir`, `banners.top`, `banners.midroll`, each `accounts[].background`) against `ROOT`, create data subfolders (`raw sources build screenshots backups locks tmp`), call `validate(cfg)`, return dict. Add `cfg["data"] = Path(...)` for convenience.
   - `validate(cfg)`: collect ALL problems into a list, then `raise SystemExit("config errors:\n- ...")` if any. Checks:
     - nicks unique; each account has non-empty `adspower_id` and an existing `background` file.
     - each account's `caption_lang` is `japanese`, `russian` or `none`; for every account with `japanese` or `russian`, `captions.donors_<that language>` is non-empty (`none` needs no donors).
     - `1 <= daily_limit <= 3` (H6: hard cap 3 regardless of anything).
     - `banners.top` exists; if `banners.midroll` is non-empty it exists.
     - `banners.top_min_height_px >= ceil(compose.height / 6)` and `banners.top_height_px >= banners.top_min_height_px`.
     - Let `banner_bottom = banners.top_y + banners.top_height_px`. Nothing may cover the banner (H8):
       `banners.midroll_box.y >= banner_bottom`; `circle_center_y - circle_diameter / 2 >= banner_bottom`;
       if `compose.text_overlay`: `text_y >= banner_bottom`.
     - The circle fits the frame: `circle_diameter <= width` and `circle_center_y + circle_diameter / 2 <= height`.
     - `playerok.submit_by_day < playerok.deadline_days`.
     - `planner.window_start < planner.window_end`.
     - every `filter.blocked_patterns` entry compiles with `re.compile`.
   - `secret(name) -> str`: `os.environ[name]` with a clear error if missing. Only read secrets where used (so Phase 1 works without any secrets).

**Acceptance criteria:**
- [ ] `cp config.example.toml config.toml`, put any jpg/png at the example paths, `python -c "from clips import config; print(config.load()['playerok'])"` prints the section.
- [ ] Setting `daily_limit = 4` makes `config.load()` exit with a message containing `daily_limit`.
- [ ] Deleting the background file makes `config.load()` exit with a message containing the file path.
- [ ] Two errors at once are both reported in one message.

### Task 0.3: Database module

**Why:** SQLite is the single source of truth (spec). One schema, created idempotently.

**Files:** create `clips/db.py`; add a fixture to `tests/conftest.py`.

**Steps:**

1. `clips/db.py` contents:
   - `SCHEMA`: the SQL from section 2.4, verbatim.
   - `connect(path) -> sqlite3.Connection`: `sqlite3.connect(path, timeout=30)`, `row_factory = sqlite3.Row`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=30000`, `executescript(SCHEMA)`, return. Signature `connect(path, readonly=False)`: with `readonly=True` (used for every `--dry-run`) open `file:{path}?mode=ro` (URI), or an in-memory DB with the schema if the file does not exist, and skip the WAL pragma. A dry run then never creates or migrates the DB, and any accidental write raises `sqlite3.OperationalError`. Test: `tests/test_db.py`.
   - `now_iso() -> str`: `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`.
   - `iso(dt) -> str` and `parse_iso(s) -> datetime` (aware UTC). `datetime.fromisoformat` handles the `Z` suffix on 3.11+.
   - `set_account_status(con, nick, status, reason=None, paused_until=None)`: one UPDATE, sets `updated_at`.
   - `resume_paused_accounts(con) -> list[str]`: set `active` where `status='paused' AND paused_until <= now`, return nicks (caller notifies).
2. Give `config.load` a keyword `check_files=True`; when False, skip the file-existence checks only.
3. `tests/conftest.py`: fixture `con` = `db.connect(tmp_path/"t.db")`; fixture `cfg` = `config.load(ROOT/"config.example.toml", check_files=False)`, then `cfg["data"] = tmp_path`, create the data subfolders there, and (for video tests) point `cfg["banners"]["top"]`, `cfg["banners"]["midroll"]` and the example account's `background` at the synthetic media from Task 1.1. Unit tests never need real assets.

**Acceptance criteria:**
- [ ] `python -m clips init-db` (after Task 0.4) creates `data/clips.db`; `sqlite3 data/clips.db ".tables"` lists `accounts publications sources view_snapshots`.
- [ ] Running `init-db` twice is harmless.
- [ ] `sqlite3 data/clips.db "PRAGMA journal_mode"` prints `wal`.

### Task 0.4: CLI entry point

**Why:** one entry point for humans, systemd, and tests. Uniform `--dry-run`, logging, error alerts.

**Files:** create `clips/__main__.py`.

**Steps:**

1. `argparse` with subcommands from section 2.6. Flags `--dry-run`, `--verbose`, `--config PATH` are accepted both before and after the command (`--dry-run plan` and `plan --dry-run`): register them on the root parser and on every subparser, with `default=argparse.SUPPRESS` on the subparsers so a flag given before the command is not reset to its default.
2. `main()`:
   - `logging.basicConfig(level=DEBUG if verbose else INFO, stream=sys.stdout, format=...)`.
   - `cfg = config.load(args.config)`; `con = db.connect(cfg["data"]/"clips.db", readonly=args.dry_run)`.
   - Dispatch to the module function (each module exposes `run_<command>(cfg, con, args, dry)` or a similarly obvious name). Commands not implemented yet: print "not implemented yet (phase N)" and exit 2.
   - Wrap dispatch in `try/except Exception`: `log.exception(...)`; if not dry and Telegram is configured (`TELEGRAM_BOT_TOKEN` set; `telegram.send` exists from Task 4.3, before that only log), send `⚠️ Ошибка в команде <cmd>: <ExceptionType>: <message[:300]>`; `sys.exit(1)`. Do not let a Telegram failure hide the original error (wrap the send in its own try/except).
3. `init-db`: just connect (schema is created on connect) and print the DB path.

**Acceptance criteria:**
- [ ] `python -m clips --help` lists every command from section 2.6.
- [ ] `python -m clips init-db --dry-run` exits 0.
- [ ] `python -m clips plan` prints "not implemented yet (phase 3)" and exits 2 (until Phase 3).

---

## Phase 1: Video build (spec modules 7-8)

Goal of the phase: from a local source file and an account, produce a 1080x1920 reel that satisfies every banner rule, and prove it automatically. No network, no secrets.

All ffmpeg recipes below were tested on ffmpeg 6.1.1 (the Ubuntu 24.04 package): durations, freeze, banner visibility and audio switching all checked out.

### Task 1.1: ffprobe and frame helpers

**Why:** compose and validate both need media facts; dedup needs frames.

**Files:** create `clips/video.py` (first part); `tests/test_video.py`; extend `tests/conftest.py`.

**Steps:**

1. `run(cmd: list[str]) -> str`: `subprocess.run(cmd, capture_output=True, text=True)`; on non-zero exit raise `RuntimeError` including the command and the last 2000 chars of stderr. Always pass `-v error` (ffprobe) or `-v error -y` (ffmpeg). Log the command at DEBUG.
2. `probe(path) -> dict`: `ffprobe -v error -print_format json -show_streams -show_format`; return `{"duration": float(format.duration), "size": int(format.size), "video": first video stream or None, "audio": first audio stream or None}`.
3. `grab_frame(path, t) -> PIL.Image` (RGB): `ffmpeg -v error -ss {t} -i {path} -frames:v 1 -f image2pipe -vcodec png -` with `capture_output=True` (bytes, not text), `Image.open(io.BytesIO(...)).convert("RGB")`.
4. `region_diff(a, b, box=None, mask=None) -> float`: mean absolute difference over RGB channels: `ImageStat.Stat(ImageChops.difference(a, b), mask=mask).mean` averaged over 3 channels; crop both to `box` first if given.
5. `tests/conftest.py`: session-scoped fixture `media(tmp_path_factory)` that generates with ffmpeg lavfi:
   ```bash
   ffmpeg -v error -y -f lavfi -i testsrc2=size=720x1280:rate=30:duration=25 -f lavfi -i sine=frequency=440:duration=25 -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac src25.mp4
   ffmpeg -v error -y -f lavfi -i testsrc2=size=720x1280:rate=30:duration=8 -f lavfi -i sine=frequency=440:duration=8 -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac src8.mp4
   ffmpeg -v error -y -f lavfi -i testsrc2=size=640x360:rate=25:duration=15 -c:v libx264 -pix_fmt yuv420p src15_noaudio.mp4
   ffmpeg -v error -y -f lavfi -i smptebars=size=1080x608:rate=30:duration=3 -f lavfi -i sine=frequency=880:duration=3 -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac midroll.mp4
   ```
   and with Pillow: `bg.png` 1080x1920 (two solid colors), `banner.png` 1080x360 RGBA (red with a white rectangle), `banner2.png` (a different color, for the negative test).

**Tests (`tests/test_video.py`):**
```python
def test_probe(media):
    p = video.probe(media["src25"])
    assert abs(p["duration"] - 25) < 0.1 and p["audio"] and p["video"]["width"] == 720

def test_probe_no_audio(media):
    assert video.probe(media["src15_noaudio"])["audio"] is None
```

**Acceptance criteria:**
- [ ] `.venv/bin/pytest tests/test_video.py -q` passes.

### Task 1.2: Base composition (background + circle + top banner)

**Why:** the format that pays: account background full frame, source video in a circle with a thin white ring, Playerok banner sticker centered at the top for the whole reel (spec module 7, H8). Target look: section 1.1.

**Files:** modify `clips/video.py`.

**Steps:**

1. `make_frame_png(background, cfg, out_path)`: background with a transparent circular hole where the video shows through, plus the white ring on top of the hole's edge. Smooth edges via 4x supersampling:
   ```python
   def make_frame_png(background, cfg, out_path):
       c = cfg["compose"]
       W, H, D, cy = c["width"], c["height"], c["circle_diameter"], c["circle_center_y"]
       bg = ImageOps.fit(Image.open(background).convert("RGBA"), (W, H))
       s = 4
       box = [(W - D) // 2 * s, (cy - D // 2) * s, (W + D) // 2 * s, (cy + D // 2) * s]
       mask = Image.new("L", (W * s, H * s), 255)
       ImageDraw.Draw(mask).ellipse(box, fill=0)
       bg.putalpha(mask.resize((W, H), Image.LANCZOS))
       if c["circle_border_px"]:
           ring = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
           ImageDraw.Draw(ring).ellipse(box, outline=c["circle_border_color"], width=c["circle_border_px"] * s)
           bg = Image.alpha_composite(bg, ring.resize((W, H), Image.LANCZOS))
       bg.save(out_path)
   ```
2. `compose_base(src, frame_png, cfg, out, crf, preset, text=None)`: one ffmpeg call. Inputs in this fixed order: `0` source, `1` frame PNG (`-loop 1 -framerate {fps}`), `2` top banner (PNG/JPG: `-loop 1 -framerate {fps}`; MP4/MOV/WEBM: `-stream_loop -1`), `3` silent audio ONLY when the source has no audio (`-f lavfi -t {dur} -i anullsrc=r=48000:cl=stereo`). Every overlay uses `shortest=1`, and the output has `-t {dur}`, so looped inputs never extend the video. Filter graph (D = diameter, CY = center y, TOPY = `banners.top_y`, TOPH = `banners.top_height_px`):
   ```
   [0:v]fps=30,scale=D:D:force_original_aspect_ratio=increase,crop=D:D,setsar=1,pad=1080:1920:(1080-D)/2:CY-D/2:black[c];
   [c][1:v]overlay=0:0:shortest=1[f];
   [2:v]fps=30,scale=-2:TOPH[b];
   [f][b]overlay=(main_w-overlay_w)/2:TOPY:shortest=1,format=yuv420p[v];
   [A]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a]
   ```
   The banner is scaled by height and centered horizontally (tested: a 600x480 sticker becomes 450x360 at x = 315, y = 280).
   where `[A]` is `[0:a]` or `[3:a]`. If `text` is given, append to the video chain before `format=yuv420p`: `drawtext=fontfile={font}:textfile={tmp.txt}:fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=20:x=(w-text_w)/2:y={text_y}`. Use `textfile=` (write the text to a temp file) so you never have to escape user text inside the filter string.
   Output args: `-map [v] -map [a] -t {dur} -c:v libx264 -preset {preset} -crf {crf} -r 30 -c:a aac -b:a {audio_bitrate} -movflags +faststart`.
3. Banner size check (before running ffmpeg): scaled banner width = `round(banner_w * top_height_px / banner_h)`; if it is wider than `compose.width`, raise `ValueError("banner too wide at top_height_px: ...")` (lower `top_height_px`, but never below `top_min_height_px`). Never stretch or squash a banner: aspect ratio is always kept.

**Tests:**
```python
def test_compose_base_layout(media, cfg, tmp_path):
    out = tmp_path / "base.mp4"
    frame = tmp_path / "frame.png"
    video.make_frame_png(media["bg"], cfg, frame)
    video.compose_base(media["src25"], frame, cfg, out, crf=20, preset="veryfast")
    p = video.probe(out)
    assert (p["video"]["width"], p["video"]["height"]) == (1080, 1920)
    assert abs(p["duration"] - 25) < 0.1 and p["audio"]

def test_compose_adds_silence_when_source_has_no_audio(media, cfg, tmp_path):
    ...  # src15_noaudio -> output has an audio stream, duration 15 +/- 0.1
```

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] Unit test on `make_frame_png`: pixel at `(540, circle_center_y)` has alpha 0 (hole), pixel at `(540, circle_center_y - circle_diameter // 2 + 1)` is white (ring), pixel at `(10, 10)` is opaque background.
- [ ] HUMAN CHECK (optional but recommended): open `base.mp4` from the test tmp dir (or produce one with `python -m clips build` after Task 1.6) and compare with section 1.1: background fills the frame, circle is round with a thin white ring, banner sticker centered at the top, circle below the banner.

### Task 1.3: Voiced midroll insertion

**Why:** 700 RUB instead of 400 RUB per 100k views. Spec: for sources 12 s or longer insert the voiced banner; freeze the frame, stop the original audio, play the whole animation with its own sound at normal speed.

**Files:** modify `clips/video.py`; extend `tests/test_video.py`.

**Steps:**

1. Pure function, exactly:
   ```python
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
   ```
   Integer seconds keep video trim (frame boundaries at 30 fps) and audio trim aligned.
2. `insert_midroll(base, midroll, t, cfg, out)`: second ffmpeg pass. `M` = midroll video stream duration from `probe`. If `midroll_has_alpha`, put `-c:v libvpx-vp9` before `-i {midroll}` so alpha is decoded. Inputs: `0` base, `1` midroll. Graph (MX/MY/MW from `banners.midroll_box`):
   ```
   [0:v]split=2[v0][v1];[0:a]asplit=2[a0][a1];
   [v0]trim=end=T,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=M[pre];
   [1:v]fps=30,scale=MW:-2,setpts=PTS-STARTPTS+T/TB[mid];
   [pre][mid]overlay=MX:MY:eof_action=pass[va];
   [v1]trim=start=T,setpts=PTS-STARTPTS[vb];
   [va][vb]concat=n=2:v=1:a=0,format=yuv420p[v];
   [a0]atrim=end=T,asetpts=PTS-STARTPTS[ap];
   [1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=end=M,asetpts=PTS-STARTPTS[am];
   [a1]atrim=start=T,asetpts=PTS-STARTPTS[aq];
   [ap][am][aq]concat=n=3:v=0:a=1[a]
   ```
   How it works: `tpad` clones the last frame before T for M seconds (freeze, banner included). The midroll is delayed to start at T and drawn on the frozen frame. Original audio is cut at T, the midroll audio (padded/trimmed to exactly M) plays, then the original resumes at T. Nothing is sped up. Final duration = source duration + M.
   Output args: same encoder args as the base (`-c:v libx264 -preset {preset} -crf {crf} -r 30 -c:a aac -b:a {bitrate} -movflags +faststart`).
3. When a midroll follows, encode the base pass as a high-quality intermediate (`crf=12`, `preset=veryfast`) into `data/tmp`, so the second encode does not visibly degrade quality.

**Tests:**
```python
B = {"midroll": "x.mp4", "midroll_min_source_sec": 12, "midroll_short_until_sec": 20,
     "midroll_short_insert_sec": 10, "midroll_long_window_sec": [10, 20], "midroll_long_insert_sec": 12}

def test_midroll_at():
    assert video.midroll_at(11.9, B) is None
    assert video.midroll_at(12.0, B) == 9.0        # never in the last 3 s
    assert video.midroll_at(15.0, B) == 10.0
    assert video.midroll_at(25.0, B) == 12.0       # inside 10-20
    assert video.midroll_at(600.0, B) == 12.0
    assert video.midroll_at(25.0, {**B, "midroll": ""}) is None

def test_midroll_freeze_audio_and_duration(media, cfg, tmp_path):
    # build base from src25, insert midroll at T=12 (M=3)
    # 1) duration == 28 +/- 0.1
    # 2) freeze: region_diff(frame(12.5), frame(14.5), box=(250,1300,830,1450)) < 1.0
    #    (inside the circle, below the midroll box y=660..1268) while frame(11) vs frame(16) > 3.0
    # 3) midroll visible: region_diff(frame(13.5), frame(11.9), box=(0,700,1080,1200)) > 30
    # 4) original audio stops: mean_volume of 440 Hz band in [12.2, 14.8] is >= 15 dB lower than in [2, 10]
    #    use: ffmpeg -ss S -t D -i out -vn -af "bandpass=f=440:width_type=h:w=50,volumedetect" -f null -
    #    and parse "mean_volume: X dB" from stderr
```
Measured reference values from the recipe test: freeze diff 0.006, moving diff 7.7, banner diff 0.7 (solid banner) and 2.8 (sticker with soft edges), 440 Hz band -46.5 dB inside the midroll vs -24.1 dB outside.

**Acceptance criteria:**
- [ ] All tests pass.
- [ ] HUMAN CHECK: play one output: the picture stops, the animation plays with its own sound, the original sound is silent during it, then the video continues where it stopped.

### Task 1.4: Optional text plate

**Why:** spec module 7 (optional): a short text plate lowers the risk of an Instagram "unoriginal content" flag.

**Steps:** already wired in Task 1.2 (`text` argument). In `build()` (Task 1.6) pass `random.choice(account["overlay_texts"])` only when `compose.text_overlay = true` and the list is not empty.

**Acceptance criteria:**
- [ ] With `text_overlay = true`, `python -m clips build ... --text "Смотри до конца"` produces a reel with readable Cyrillic text at `text_y` (HUMAN CHECK on one frame: `ffmpeg -ss 2 -i out.mp4 -frames:v 1 f.png`).

### Task 1.5: Validation (spec module 8)

**Why:** a broken reel is never published (H4). Also proves the banner is present and not darkened or covered, which is what Playerok checks.

**Files:** modify `clips/video.py`; extend `tests/test_video.py`.

**Steps:**

1. `validate(path, cfg, expected_duration, midroll_window=None) -> list[str]` returns a list of human-readable problems (empty list = OK). Checks:
   - file exists and `size <= compose.max_file_mb * 1024 * 1024`;
   - video stream: codec `h264`, `pix_fmt` `yuv420p`, width/height = `compose.width/height`, frame rate 30 (`r_frame_rate == "30/1"`);
   - audio stream exists, codec `aac`;
   - `abs(duration - expected_duration) <= 0.25`;
   - banner check (static image banners only; for video banners log "banner check skipped: animated banner" at INFO): load banner, scale it to height `top_height_px` keeping aspect (`bw = round(w * top_height_px / h)`), take its alpha channel as mask (fully opaque pixels only: `alpha.point(lambda a: 255 if a == 255 else 0)`; a sticker's transparent parts show the background and must not be compared), and for each sample time in `[0.5, duration/2, duration - 0.5]` plus the middle of `midroll_window` (if any): `region_diff(frame.crop(band), banner_rgb, mask=mask) <= compose.banner_check_max_diff`, where `x0 = (1080 - bw) // 2` and `band = (x0, top_y, x0 + bw, top_y + top_height_px)`. On failure add `"banner mismatch at t=...: diff=..."`.
2. CLI `validate PATH`: prints `OK` or each problem, exit code 0/1. Expected duration for the CLI: the file's own duration (so only format and banner are checked).

**Tests:**
```python
def test_validate_ok(built_reel, cfg): assert video.validate(built_reel.path, cfg, 28.0, (12, 15)) == []
def test_validate_wrong_resolution(media, cfg): assert any("1080" in e for e in video.validate(media["src25"], cfg, 25.0))
def test_validate_banner_mismatch(built_reel, cfg):
    cfg2 = with banners.top pointing to banner2.png
    assert any("banner" in e for e in video.validate(built_reel.path, cfg2, 28.0))
```

**Acceptance criteria:**
- [ ] Tests pass (positive and both negative cases).

### Task 1.6: `build` function and CLI

**Why:** one call used by the planner (Phase 3) and by humans for testing.

**Files:** modify `clips/video.py`, `clips/__main__.py`.

**Steps:**

1. `build(src, account: dict, cfg, out, text=None, use_midroll=True) -> dict`:
   - `info = probe(src)`; raise `ValueError` if no video stream.
   - `t = midroll_at(info["duration"], cfg["banners"]) if use_midroll else None`.
   - Work in `tempfile.TemporaryDirectory(dir=cfg["data"]/"tmp")`: frame PNG, base (final encode if `t is None`, else intermediate), midroll pass into `out`.
   - Write to `out` with a `.part` suffix first, then `os.replace` (no half-written files).
   - Return `{"duration": final_duration, "midroll_at": t, "midroll_dur": M or 0, "banner_type": "voiced" if t is not None else "static", "banner_file": Path(top).name, "background": Path(account["background"]).name}`.
   - Then call `validate(out, cfg, expected_duration, (t, t + M) if t is not None else None)`; if problems, delete `out` and raise `ValueError("; ".join(problems))`.
2. CLI `build --source --account --out [--no-midroll] [--text]`: look up the account in config by nick, call `build`, print the returned dict as JSON.

**Acceptance criteria (Phase 1 exit):**
- [ ] `.venv/bin/pytest -q` passes.
- [ ] `python -m clips build --source <a real meme mp4 of 25 s> --account example_account --out data/tmp/t.mp4` prints JSON with `"banner_type": "voiced"`, and `python -m clips validate data/tmp/t.mp4` prints `OK`.
- [ ] Same with a real 8 s source: `"banner_type": "static"`, duration unchanged.
- [ ] Build time for a 30 s source is under 60 s on the server (6 vCPU). Measured on the recipe test: 25 s source, base pass 13 s + midroll pass 8 s.
- [ ] HUMAN CHECK: the owner watches 2 outputs on a phone and confirms they match Playerok's banner requirements (position, size, not covered, midroll behavior) and look like the reference layout in section 1.1.

---

## Phase 2: Publish one reel to one account (spec modules 1, 10, 11)

Prerequisites (owner): AdsPower Linux client installed on the server with Local API access and an API key; at least one profile with its own mobile/residential proxy; Instagram logged in manually in that profile; profile `user_id` known.

### Task 2.1: Accounts in config and DB (spec module 1)

**Why:** each account looks different (background, caption language) and the pipeline must know where it may post. Caption texts are not per account: they come from the shared pool (Task 3.5).

**Files:** create `run_sync_accounts` in `clips/db.py` (it is 15 lines, no new module); wire `sync-accounts` and `account-status` in `__main__.py`.

**Steps:**

1. Static fields (nick, adspower_id, background, caption_lang, overlay_texts, daily_limit) live ONLY in config. Runtime fields (status, followers, ...) live ONLY in the DB. No duplication.
2. `sync_accounts(con, cfg)`: `INSERT OR IGNORE INTO accounts(nick, status, updated_at) VALUES (?, 'active', now)` for each config account. Log a WARNING for DB accounts missing from config (never delete them; their history matters). Call it at the start of `plan`, `publish`, `stats` (cheap, keeps things in sync).
3. `account-status NICK STATUS [--reason]`: validate STATUS in the 4 allowed values, call `set_account_status`.
4. Helper `account_cfg(cfg, nick) -> dict` (raise `KeyError` with a clear message).

**Acceptance criteria:**
- [ ] `python -m clips sync-accounts` twice: `sqlite3 data/clips.db "select nick,status from accounts"` shows each config account once, status `active`.
- [ ] `python -m clips account-status example_account paused --reason test` then `sync-accounts`: status stays `paused`.

### Task 2.2: AdsPower client and browser session

**Why:** one AdsPower profile = one Instagram account with its own proxy (H2). The Local API starts the profile and returns a CDP websocket for Playwright.

**AdsPower Local API facts used here** (official docs: https://localapi-doc-en.adspower.com/):
- Base URL `http://local.adspower.net:50325` or `http://127.0.0.1:50325`.
- When security verification is on, every call needs header `Authorization: Bearer <api key>`.
- Rate limit: 2 requests/second for up to 200 profiles; some endpoints 1 request/second. We space calls by `adspower.request_interval_sec`.
- `GET /api/v1/browser/active?user_id=X` returns `{"code":0,"data":{"status":"Active"|"Inactive",...}}`.
- `GET /api/v1/browser/start?user_id=X&headless=1&open_tabs=1&ip_tab=0` returns `{"code":0,"data":{"ws":{"puppeteer":"ws://127.0.0.1:.../devtools/browser/..."},...}}`. Failure: `code != 0` with `msg`.
- `GET /api/v1/browser/stop?user_id=X`.
- Headless client start (Linux): `adspower_global --headless=true --api-key=KEY --api-port=50325` (see Task 6.1).

**Files:** create `clips/adspower.py`; start `clips/instagram.py`.

**Steps:**

1. `clips/adspower.py` (about 40 lines):
   ```python
   class ProfileBusy(Exception): pass
   class AdsPowerError(Exception): pass

   def _get(cfg, path, **params) -> dict:
       time.sleep(cfg["adspower"]["request_interval_sec"])
       key = os.environ.get("ADSPOWER_API_KEY", "")
       r = requests.get(cfg["adspower"]["base_url"] + path, params=params, timeout=120,
                        headers={"Authorization": f"Bearer {key}"} if key else {})
       r.raise_for_status()
       j = r.json()
       if j.get("code") != 0:
           raise AdsPowerError(f"{path} {params}: {j.get('msg')}")
       return j.get("data") or {}

   def is_active(cfg, profile_id) -> bool:
       return _get(cfg, "/api/v1/browser/active", user_id=profile_id).get("status") == "Active"

   def start(cfg, profile_id) -> str:
       if is_active(cfg, profile_id):
           raise ProfileBusy(profile_id)          # someone (operator) has it open here: do not touch
       d = _get(cfg, "/api/v1/browser/start", user_id=profile_id,
                headless=1 if cfg["adspower"]["headless_browser"] else 0, open_tabs=1, ip_tab=0)
       return d["ws"]["puppeteer"]

   def stop(cfg, profile_id) -> None:
       _get(cfg, "/api/v1/browser/stop", user_id=profile_id)
   ```
   A profile open on another device of the AdsPower team makes `start` fail with `code != 0`: treat any `AdsPowerError` from `start` as "busy/unavailable" (log, alert once per publication, skip).
2. Browser lock (H7) in `instagram.py`:
   ```python
   @contextmanager
   def browser_lock(cfg):
       f = open(cfg["data"] / "locks" / "browser.lock", "w")
       try:
           fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
       except BlockingIOError:
           raise SystemExit(0)   # another browser job is running; this run does nothing
       try:
           yield
       finally:
           f.close()
   # ponytail: one profile at a time, add a counting semaphore if throughput ever needs 2-3 profiles
   ```
3. Session context manager:
   ```python
   @contextmanager
   def session(cfg, account):
       ws = adspower.start(cfg, account["adspower_id"])
       try:
           with sync_playwright() as pw:
               browser = pw.chromium.connect_over_cdp(ws)
               ctx = browser.contexts[0]
               page = ctx.new_page()
               page.set_default_timeout(30_000)
               try:
                   yield page
               except Exception:
                   save_debug(cfg, page, account["nick"], "error")   # H9
                   raise
               finally:
                   page.close()
                   browser.close()   # disconnects CDP only
       finally:
           adspower.stop(cfg, account["adspower_id"])
   ```
4. `save_debug(cfg, page, nick, step) -> Path`: `data/screenshots/{YYYYmmdd-HHMMSS}_{nick}_{step}.png` (`full_page=True`) and the same name `.html` with `page.content()`. Swallow errors inside (a failing screenshot must not hide the real error), log the paths.
5. `pause(cfg)`: `time.sleep(random.uniform(*cfg["publish"]["pause_sec"]))`.

**Acceptance criteria:**
- [ ] `curl -s "http://127.0.0.1:50325/api/v1/browser/active?user_id=<id>" -H "Authorization: Bearer $ADSPOWER_API_KEY"` returns `"code":0` (proves AdsPower runs headless and the key works).
- [ ] With the profile already open (start it via curl), `python -m clips health --account X` (Task 2.3) exits without touching it and logs `ProfileBusy`.

### Task 2.3: Health check (spec module 11)

**Why:** an account in trouble must stop posting immediately, and the operator must know. The pipeline never touches an account in `check` until the operator says it is solved (H5).

**Files:** create `clips/ig_selectors.py` (health part); modify `clips/instagram.py`; create `tests/test_instagram.py`.

**Steps:**

1. In `clips/ig_selectors.py` (signals are data, not logic; lowercase texts):
   ```python
   URL_LOGIN = ("/accounts/login",)
   URL_CHECKPOINT = ("/challenge", "/accounts/suspended", "/auth_platform")
   URL_DISABLED = ("/accounts/disabled",)
   TEXT_DISABLED = ("your account has been disabled", "we disabled your account",
                    "ваш аккаунт отключен", "мы отключили ваш аккаунт")
   TEXT_CHECKPOINT = ("confirm it's you", "enter the code", "we sent a code", "upload a selfie",
                      "подтвердите, что это вы", "введите код", "мы отправили код", "селфи")
   TEXT_ACTION_BLOCK = ("try again later", "we restrict certain activity",
                        "повторите попытку позже", "мы ограничиваем")
   LOGGED_IN = ['svg[aria-label="Home"]', 'svg[aria-label="Главная"]']
   ```
2. In `instagram.py` define `class HealthProblem(Exception)` (its single argument is the health string). Pure function (unit-testable), in this order:
   ```python
   def classify_health(url: str, text: str, logged_in: bool) -> str:
       """'ok' | 'logged_out' | 'checkpoint' | 'action_block' | 'disabled'"""
       u, t = url.lower(), text.lower()
       if any(s in u for s in S.URL_DISABLED) or any(s in t for s in S.TEXT_DISABLED): return "disabled"
       if any(s in u for s in S.URL_CHECKPOINT) or any(s in t for s in S.TEXT_CHECKPOINT): return "checkpoint"
       if any(s in u for s in S.URL_LOGIN) or not logged_in: return "logged_out"
       if any(s in t for s in S.TEXT_ACTION_BLOCK): return "action_block"
       return "ok"
   ```
3. `check_health(page) -> str`: read `page.url`, `page.inner_text("body")[:20000]`, `logged_in` = any `LOGGED_IN` selector visible within 10 s, return `classify_health(...)`.
4. `apply_health(con, cfg, nick, health, screenshot, dry)`: mapping:
   | health | DB status | reason | alert |
   |--------|-----------|--------|-------|
   | `ok` | unchanged | | none |
   | `action_block` | `paused`, `paused_until = now + publish.action_block_pause_hours` | `action_block` | yes |
   | `checkpoint` | `check` | `checkpoint` | yes, with button "Проверку прошёл" |
   | `logged_out` | `check` | `logged_out` | yes, with button "Проверку прошёл" |
   | `disabled` | `banned` | `disabled` | yes |
   Alert text (Russian, no em dash): `🔴 Аккаунт @{nick}: {reason_ru}. Публикации остановлены.` plus the screenshot as a photo (Telegram module Task 4.4; until Phase 4 just log the alert). Also run the submit logic for this account's reels right away (Task 4.2 reason `account_in_danger`) once Phase 4 exists.
5. `run_health(cfg, con, args, dry)`: refuse if DB status is `check` or `banned` (print why, exit 0). Otherwise: `browser_lock` -> `session` -> `page.goto("https://www.instagram.com/", wait_until="domcontentloaded")` -> pause -> `check_health` -> screenshot `health` -> print result -> `apply_health` unless dry.

**Tests (`tests/test_instagram.py`):**
```python
def test_classify():
    f = instagram.classify_health
    assert f("https://www.instagram.com/", "", True) == "ok"
    assert f("https://www.instagram.com/accounts/login/?next=/", "", False) == "logged_out"
    assert f("https://www.instagram.com/challenge/abc/", "", False) == "checkpoint"
    assert f("https://www.instagram.com/", "Введите код из письма", True) == "checkpoint"
    assert f("https://www.instagram.com/", "Try Again Later. We restrict certain activity", True) == "action_block"
    assert f("https://www.instagram.com/accounts/disabled/", "", False) == "disabled"
```

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] HUMAN CHECK: `python -m clips health --account X` on a healthy logged-in profile prints `ok`, saves `data/screenshots/*_X_health.png` showing the Instagram feed, and afterwards `/api/v1/browser/active` returns `Inactive`.
- [ ] `python -m clips account-status X check` then `health --account X` refuses to open the profile.

### Task 2.4: Publish flow (spec module 10)

**Why:** the core action. Selectors live in one module because the Instagram UI changes (spec).

**Files:** modify `clips/ig_selectors.py` (UI part), `clips/instagram.py`; extend `tests/test_instagram.py`.

**Steps:**

1. Add UI selectors to `clips/ig_selectors.py`. Each is a list of alternatives (English and Russian UI); the first visible one wins. These are starting points: verify every one in dry-run screenshots and fix them HERE only.
   ```python
   CREATE = ['svg[aria-label="New post"]', 'svg[aria-label="Новая публикация"]',
             'svg[aria-label="Create"]', 'svg[aria-label="Создать"]']
   CREATE_POST_ITEM = ['a:has(svg[aria-label="Post"])', 'a:has(svg[aria-label="Публикация"])',
                       'div[role="button"]:has-text("Post")', 'div[role="button"]:has-text("Публикация")']
   FILE_INPUT = ['div[role="dialog"] input[type="file"]', 'input[type="file"]']
   REELS_NOTICE_OK = ['div[role="dialog"] button:has-text("OK")', 'div[role="dialog"] button:has-text("ОК")']
   CROP_BUTTON = ['svg[aria-label="Select crop"]', 'svg[aria-label="Выбрать размер и обрезать"]']
   CROP_ORIGINAL = ['div[role="button"]:has-text("Original")', 'div[role="button"]:has-text("Оригинал")']
   NEXT = ['div[role="dialog"] div[role="button"]:text-is("Next")',
           'div[role="dialog"] div[role="button"]:text-is("Далее")']
   CAPTION = ['div[role="dialog"] div[contenteditable="true"][role="textbox"]']
   SHARE = ['div[role="dialog"] div[role="button"]:text-is("Share")',
            'div[role="dialog"] div[role="button"]:text-is("Поделиться")']
   SHARED_OK = ['text=/reel has been shared/i', 'text=/post has been shared/i',
                'text=/опубликован/i', 'text=/поделились/i']
   REEL_LINK = 'a[href*="/reel/"]'
   ```
2. Helper in `instagram.py`:
   ```python
   def first(page, candidates, timeout_ms=15_000):
       deadline = time.monotonic() + timeout_ms / 1000
       while time.monotonic() < deadline:
           for sel in candidates:
               loc = page.locator(sel).first
               if loc.count() and loc.is_visible():
                   return loc
           page.wait_for_timeout(500)
       raise LookupError(f"none visible: {candidates}")
   ```
   For `FILE_INPUT` (hidden element) use `page.locator(sel).first` with `count()` only, not `is_visible()`.
3. `post_reel(page, cfg, nick, file_path, caption, dry) -> None`, steps (call `pause(cfg)` between every step; in dry-run call `save_debug(..., step)` after every step):
   1. `page.goto("https://www.instagram.com/")`; health check; if not `ok` raise `HealthProblem(health)`.
   2. Warm-up: random `warmup_sec`, scroll with `page.mouse.wheel(0, random 300-900)` every 1-3 s.
   3. Click `CREATE`; if `CREATE_POST_ITEM` appears within 5 s, click it.
   4. `set_input_files(file_path)` on `FILE_INPUT`.
   5. If `REELS_NOTICE_OK` appears within 5 s, click it.
   6. If `CROP_BUTTON` exists: click it, click `CROP_ORIGINAL` (our file is already 9:16).
   7. Click `NEXT` (crop step), pause, click `NEXT` (edit step).
   8. If the caption is empty (`caption_lang = none`) skip this step. Otherwise click `CAPTION` and type it with `page.keyboard.type(caption, delay=random.randint(*typing_delay_ms))`. `keyboard.type` inserts any Unicode text (Japanese, emoji) directly, no IME needed, and `\n` becomes an Enter press. In the dry-run screenshot check that the text and its line breaks look exactly like the pool entry.
   9. **Dry-run stops here**: screenshot `before_share`, close the dialog with `Escape` (confirm "Discard" / "Отменить" if asked), return.
   10. Record `share_clicked_at = now` in the DB BEFORE clicking (if the process dies after the click we still know a post may exist). Click `SHARE`.
   11. Wait for `SHARED_OK` up to `upload_timeout_sec`. While waiting, every 10 s run `classify_health` on the page text: `action_block` -> raise `HealthProblem("action_block")`.
4. `find_reel(page, nick, caption, known: set[str]) -> str | None`: `goto https://www.instagram.com/{nick}/reels/`, wait for `REEL_LINK`, take the first 6 hrefs, extract shortcodes with `re.search(r"/reel/([\w-]+)", href)`, drop ones in `known` (all `ig_shortcode` values in the DB), then for each remaining: `goto` the reel page and check that the page text contains `caption[:30]`, comparing both after collapsing whitespace (`" ".join(s.split())`, because line breaks render differently); return the first match. Retry the whole thing 3 times with 20 s waits (Instagram may need time to process). Returns None if not found. Pinned reels are why we check the caption instead of taking the first link. With an empty caption take the first unknown shortcode (`# ponytail: no caption to match, a manually pinned unknown reel would be picked; operator must not pin reels by hand`).
5. `read_followers(page, nick) -> int | None`: `goto https://www.instagram.com/{nick}/`, read `meta[name="description"]` content, parse with the pure function below. Best effort: None on any failure.
   ```python
   MULT = {"k": 1e3, "к": 1e3, "тыс": 1e3, "m": 1e6, "м": 1e6, "млн": 1e6}

   def parse_followers(text: str) -> int | None:
       m = re.search(r"(\d[\d\s.,]*?)\s*(k|m|к|м|тыс\.?|млн)?\s*(followers|подписчик)", text, re.I)
       if not m:
           return None
       num = re.sub(r"\s", "", m.group(1))
       suffix = (m.group(2) or "").lower().rstrip(".")
       if suffix:
           return int(float(num.replace(",", ".")) * MULT[suffix])
       return int(re.sub(r"[.,]", "", num))
   ```
6. `publish_one(cfg, con, pub, dry)` (used by `publish-file` now and the `publish` job in Phase 3):
   - account = config + DB row; if status not `active`, skip (log INFO).
   - `with session(cfg, account) as page`: `post_reel` -> (not dry) `find_reel` -> `read_followers`.
   - On success with a shortcode: `UPDATE publications SET state='published', published_at=now, ig_shortcode, ig_url='https://www.instagram.com/reel/<code>/'`; update `accounts.followers`; delete the built file (spec module 17: delete video files after publication).
   - Shortcode not found: `state='unconfirmed'`, alert "Рилс опубликован, но ссылку найти не удалось: @{nick} pub#{id}. Проверю завтра по статистике." (the stats job resolves it, Task 4.1).
   - `HealthProblem(h)`: `apply_health(...)` with the screenshot; if `share_clicked_at` is set mark `unconfirmed`, else `failed` with error `health:<h>`.
   - `ProfileBusy` / `AdsPowerError`: leave the publication as is (it will be retried next run until `max_delay_hours` passes), log WARNING.
   - Any other exception: screenshot already saved by `session`; if `share_clicked_at` is set mark `unconfirmed`, else `failed` with the error text; alert with the screenshot.
7. `publish-file --account --file --banner-type static|voiced [--caption]`: refuse if a publication with the same `file_path` and state `published`/`unconfirmed` exists (idempotency). Insert a publication (`source_id NULL`, `state='built'`, `scheduled_at=now`, caption = given, else `captions.take_caption` for the account's `caption_lang` (Task 3.5; until Phase 3 the `--caption` flag is required), `banner_type` from the required flag), then `browser_lock` + `publish_one`.

**Tests:**
```python
def test_parse_followers():
    assert instagram.parse_followers("1,234 Followers, 56 Following, 7 Posts") == 1234
    assert instagram.parse_followers("12.5K Followers, 3 Following") == 12500
    assert instagram.parse_followers("1 234 подписчиков, 56 подписок") == 1234
    assert instagram.parse_followers("nothing here") is None
```

**Acceptance criteria (Phase 2 exit):**
- [ ] Tests pass.
- [ ] `grep -rniE "delete|archive|remove" clips/instagram.py clips/ig_selectors.py` finds nothing that acts on Instagram posts (H1). Deleting local files is fine and lives in other modules.
- [ ] HUMAN CHECK (dry-run): `python -m clips publish-file --account X --file data/tmp/t.mp4 --banner-type voiced --dry-run` produces step screenshots up to `before_share` with the caption typed; nothing is posted; profile closed afterwards.
- [ ] HUMAN CHECK (real): same without `--dry-run`: the reel appears on the profile; `sqlite3 data/clips.db "select state, ig_url from publications"` shows `published` and the right URL; the built file is deleted; the owner opens the reel in the Instagram app and sees the banner and midroll correctly.
- [ ] Running the same real command again refuses (no double post).

---

## Phase 3: Content intake and posting queue (spec modules 2-6, 9)

### Task 3.1: Apify client

**Why:** spec: trends and stats come from Apify without login, so work accounts are never used for scraping.

**Apify facts used here** (actor page: https://apify.com/apify/instagram-reel-scraper):
- Input keys: `username` (list of usernames or profile URLs), `resultsLimit` (per profile), `onlyPostsNewerThan` (e.g. `"3 days"` or `YYYY-MM-DD`), `skipPinnedPosts` (bool). Optional paid add-ons: `includeTranscript`, `includeDownloadedVideo` (not used in v1).
- Output item keys used: `shortCode`, `url`, `ownerUsername`, `caption`, `videoPlayCount`, `videoViewCount`, `likesCount` (may be -1), `timestamp` (ISO UTC), `videoDuration`, `videoUrl` (direct mp4, expires).
- Cost grows with the number of results returned. Keep `resultsLimit` small and use `onlyPostsNewerThan`.

**Files:** create `clips/trends.py` (Apify part).

**Steps:**

1. With `requests` only (no apify-client dependency):
   ```python
   API = "https://api.apify.com/v2"

   def run_actor(cfg, run_input: dict) -> list[dict]:
       s = requests.Session()
       s.headers["Authorization"] = f"Bearer {config.secret('APIFY_TOKEN')}"
       actor = cfg["apify"]["actor"]
       run = s.post(f"{API}/acts/{actor}/runs", json=run_input,
                    params={"waitForFinish": 60}, timeout=90).json()["data"]
       deadline = time.monotonic() + cfg["apify"]["run_timeout_sec"]
       while run["status"] in ("READY", "RUNNING"):
           if time.monotonic() > deadline:
               raise TimeoutError(f"apify run {run['id']} still {run['status']}")
           run = s.get(f"{API}/actor-runs/{run['id']}", params={"waitForFinish": 60}, timeout=90).json()["data"]
       if run["status"] != "SUCCEEDED":
           raise RuntimeError(f"apify run {run['id']} ended {run['status']}")
       return s.get(f"{API}/datasets/{run['defaultDatasetId']}/items",
                    params={"clean": "true", "format": "json"}, timeout=120).json()
   ```
2. `dump_raw(cfg, name, items) -> Path`: write `data/raw/{name}_{YYYYmmdd-HHMMSS}.json` (always, also in dry-run: it is our debug trail and makes `--from-file` reruns possible).
3. `views_of(item, cfg) -> int | None`: `item.get(cfg["apify"]["views_field"])`, falling back to the other of `videoPlayCount`/`videoViewCount`; None if both missing.

**Acceptance criteria:**
- [ ] HUMAN-assisted: with a real token, `python -c` calling `run_actor` for one donor with `resultsLimit=3` returns 3 dicts that contain `shortCode` and `videoUrl`. Note the Apify run cost in the commit message.

### Task 3.2: Fetch trends (spec module 2)

**Why:** collect fresh donor reels plus enough history to compute each donor's median.

**Files:** modify `clips/trends.py`; create `tests/test_trends.py`.

**Steps:**

1. `fetch(cfg, con, dry, from_file=None)`:
   - donors = `trends.donors` minus `trends.blocked_donors`.
   - Split: `seed` donors with fewer than `min_donor_samples` mature reels in DB (posted more than `max_age_hours` ago), and `regular` donors.
   - Run at most 2 actor calls: seed `{"username": seed, "resultsLimit": seed_results, "skipPinnedPosts": true}`; regular `{"username": regular, "resultsLimit": results_per_donor, "onlyPostsNewerThan": f"{fresh_days} days", "skipPinnedPosts": true}`. With `--from-file`, load items from that JSON instead (no Apify call).
   - `dump_raw(...)`.
   - Upsert each item into `sources` (skip items without `shortCode` or `videoUrl`):
     ```sql
     INSERT INTO sources (shortcode, donor, url, video_url, caption, views, likes, duration, posted_at, fetched_at)
     VALUES (?,?,?,?,?,?,?,?,?,?)
     ON CONFLICT(shortcode) DO UPDATE SET views=excluded.views, likes=excluded.likes,
       video_url=excluded.video_url, fetched_at=excluded.fetched_at
     ```
     (state is never touched by the upsert.) `donor` = `ownerUsername` lowercased; `url` = `https://www.instagram.com/reel/{shortCode}/`; `posted_at` normalized to our ISO format.
   - Log counts: fetched, new, updated, per donor.
2. Dry-run: everything except the DB writes; print the counts.

**Tests:** `test_fetch_from_file_is_idempotent`: write a 3-item JSON fixture (realistic keys from Task 3.1), run `fetch(..., from_file=...)` twice, assert 3 rows and the second run updated `views`.

**Acceptance criteria:**
- [ ] Test passes.
- [ ] Real run with 2 donors: rows appear; second run within minutes adds no duplicates.

### Task 3.3: Selection and topic filter (spec modules 3, 4)

**Why:** take only reels that already proved they work, and never take forbidden topics (Playerok forbids crime, gangs, war, politics, drugs, swearing, discrimination, religion).

**Files:** modify `clips/trends.py`; extend `tests/test_trends.py`.

**Steps:**

1. Topic filter, pure:
   ```python
   def blocked_reason(caption: str | None, donor: str, cfg) -> str | None:
       if donor.lower() in {d.lower() for d in cfg["trends"]["blocked_donors"]}:
           return "blocked_donor"
       text = (caption or "").lower().replace("ё", "е")
       for p in cfg["filter"]["blocked_patterns"]:
           if re.search(p, text):
               return f"blocked_keyword:{p}"
       return None
   ```
   Compile patterns once (module-level cache keyed by the tuple of patterns) if profiling shows a need; it will not.
2. Donor median: median of `views` of the donor's last `median_window` mature reels (`posted_at <= now - max_age_hours`), excluding the candidate itself. Return None if fewer than `min_donor_samples`. Use `statistics.median`.
3. Decision, pure:
   ```python
   def decide(views, age_hours, duration, median, t) -> tuple[str, str | None]:
       if duration is None or not t["min_duration_sec"] <= duration <= t["max_duration_sec"]:
           return "rejected", "duration"
       if age_hours > t["max_age_hours"]:
           return "rejected", "too_old"
       if median is None:
           return "new", "wait:no_baseline"
       if views is not None and views >= max(t["min_views"], median * t["views_to_median_ratio"]):
           return "selected", None
       return "new", "wait:below_threshold"
   ```
   A fresh reel that is below the threshold stays `new` and is re-checked on the next fetch (its views grow), until it becomes `too_old`.
4. `select(cfg, con, dry)`: for every source in state `new`: `blocked_reason` first (-> `rejected`), then `decide`. Store `state`, `reason`, and `score = views / median` for selected ones. Log one line per selected source.

**Tests:**
```python
def test_blocked():
    assert trends.blocked_reason("Лучший мем #юмор", "d", cfg) is None
    assert trends.blocked_reason("Это ВОЙНА мемов", "d", cfg)  # caps + word form
    assert trends.blocked_reason("Хачапури по-аджарски", "d", cfg) is None   # no false positive
    assert trends.blocked_reason("жидкость для линз", "d", cfg) is None       # no false positive
    assert trends.blocked_reason("ну ты и сука", "d", cfg)
    assert trends.blocked_reason("ok", "BadDonor", cfg_with_blocked_donor) == "blocked_donor"

def test_decide():
    t = cfg["trends"]
    assert trends.decide(500_000, 10, 20, 100_000, t) == ("selected", None)
    assert trends.decide(150_000, 10, 20, 100_000, t)[0] == "new"        # below 2x median
    assert trends.decide(500_000, 50, 20, 100_000, t) == ("rejected", "too_old")
    assert trends.decide(500_000, 10, 200, 100_000, t) == ("rejected", "duration")
    assert trends.decide(500_000, 10, 20, None, t) == ("new", "wait:no_baseline")
    assert trends.decide(90_000, 10, 20, 10_000, t)[0] == "new"          # below absolute min_views
```

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] `python -m clips select --dry-run` on real data prints decisions; `select` then shows counts by state: `sqlite3 data/clips.db "select state, count(*) from sources group by 1"`.

### Task 3.4: Download and dedup (spec modules 5, 6)

**Why:** get the file (Apify link first, yt-dlp as fallback) and never take a video that is already in the base, even if another donor reposted it.

**Files:** modify `clips/trends.py`, `clips/video.py` (dHash); extend tests.

**Steps:**

1. dHash in `video.py` (no imagehash/numpy dependency):
   ```python
   def dhash(img) -> int:
       g = img.convert("L").resize((9, 8), Image.LANCZOS)
       px = list(g.getdata())
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
   ```
   Store as `" ".join(f"{h:016x}" for h in hashes)`.
2. `download_one(cfg, src) -> Path` into `data/sources/{shortcode}.mp4`:
   - If the file exists and probes fine, return it (idempotent).
   - Try `requests.get(video_url, stream=True, timeout=60, proxies=...)` (proxy from `download.proxy` if set) into `.part`, then `os.replace`.
   - On any error or a probe failure: `yt-dlp -q -f "mp4/best" -o {part} {url}` (add `--proxy` if set). Never pass account cookies to yt-dlp (H2).
   - Probe the result: must have a video stream and duration > 0, else raise.
3. `download(cfg, con, dry)`: for each `selected` source (oldest first): download, compute hashes, compare with every other source that has a non-null `phash` and state other than `duplicate` (that covers ready sources and everything we ever published). If `min distance <= dedup.max_distance` -> `state='duplicate'`, `reason='dup_of:<shortcode>'`, delete the file. Else `state='ready'`, store `file_path`, `phash`. Download errors -> `state='failed'`, `reason=<error[:300]>`.
   `# ponytail: linear scan over all hashes; fine for tens of thousands, add a BK-tree if it ever gets slow`
4. `trends` command = `fetch` + `select` + `download` in one run.

**Tests:**
```python
def test_dhash_same_video_reencoded(media, tmp_path):
    # re-encode src25 at crf 35 and 540x960; distance to the original <= 6
def test_dhash_different_videos(media):
    # src25 (testsrc2) vs midroll (smptebars): distance > 20
```

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] Real run: selected sources become `ready` with files in `data/sources/`; running `download` again downloads nothing.
- [ ] Manually insert a second source row whose file is a re-encode of a ready one, run `download`: it becomes `duplicate`.

### Task 3.5: Caption pool and profile bios (owner addendum 1a)

**Why:** the working competitor accounts use Japanese bios and captions copied from popular Japanese posts, unrelated to the video (section 1.1). Why it works is unknown, so we copy it and test it in the pilot against accounts without captions (Task 6.3). Every caption is used once in the whole network, so our accounts are never linked to each other by identical text. The language is set per account because Playerok wants a Russian-speaking audience and checks country stats for 500k+ reels.

**Files:** create `clips/captions.py`, `tests/test_captions.py`; add bio selectors to `clips/ig_selectors.py`; add `set_bio` to `clips/instagram.py`.

**Steps:**

1. Cleaning, pure (tested on competitor-style captions while writing this plan, including the Toy Story example):
   ```python
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
       t = BRACKET_RE.sub(lambda m: "" if has_brand(m[0], brand_words) else m[0], t)   # e.g. 【ディズニー公式】
       lines = "\n".join(" ".join(l.split()) for l in t.splitlines()).strip().split("\n")
       if has_brand(lines[0], brand_words):
           if len(lines) == 1:
               return None
           lines = lines[1:]
       t = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
       letters = [ch for ch in t if ch.isalpha()]
       if not c["min_chars"] <= len(t) <= 2200 or t.count("#") > 30 or not letters:
           return None                                   # Instagram limits: 2200 chars, 30 hashtags
       if sum(bool(SCRIPT[lang].match(ch)) for ch in letters) / len(letters) < 0.5:
           return None                                   # mostly not the expected language
       return None if trends.blocked_reason(t, "", cfg) else t
   ```
2. `harvest(cfg, con, dry, from_file=None)` (command `harvest-captions`, weekly timer): for each language used by at least one account (`japanese`, `russian`): one Apify run (`trends.run_actor`) with `{"username": captions.donors_<lang>, "resultsLimit": results_per_donor, "onlyPostsNewerThan": f"{fresh_days} days", "skipPinnedPosts": false}`; `dump_raw`. Keep items with `likesCount >= min_likes` or `likesCount == -1` (hidden). Brand words per item = `captions.brand_words + [ownerUsername, ownerFullName]`. `clean_caption(...)`; if not None: `INSERT OR IGNORE INTO captions (lang, text, source, source_name, source_url, fetched_at)` (UNIQUE text makes reruns harmless). Log: fetched, kept, total free per language. We only harvest POST captions, never profile descriptions (H10). The reel scraper returns reels only; if photo posts are needed later, switch to a post scraper actor after checking its input schema.
3. Taking a caption for a publication (called by the planner inside its transaction, and by `publish-file`):
   ```python
   def take_caption(con, lang, used_for) -> tuple[int | None, str] | None:
       """Reserve a random free caption. ('none' -> (None, '')). None = pool empty for this language."""
       if lang == "none":
           return None, ""
       row = con.execute("SELECT id, text FROM captions WHERE lang = ? AND used_at IS NULL "
                         "ORDER BY random() LIMIT 1", (lang,)).fetchone()
       if row is None:
           return None
       cur = con.execute("UPDATE captions SET used_at = ?, used_for = ? WHERE id = ? AND used_at IS NULL",
                         (db.now_iso(), used_for, row["id"]))
       return (row["id"], row["text"]) if cur.rowcount == 1 else take_caption(con, lang, used_for)
   ```
   A caption is never freed again, even if the publication later fails ("used once" is the rule).
4. Bio (command `set-bio --account NICK [--force]`; run once when an account is onboarded, see Runbook):
   - Refuse if `caption_lang = none`, or `accounts.bio_set_at` is set and no `--force`, or DB status is `check`/`banned`.
   - Pick and reserve (H10). The planner takes captions without the browser lock, so the bio caption is reserved atomically BEFORE the browser starts and released if the update fails; otherwise the planner could give the same caption to a post:
     ```python
     def reserve_bio(con, cfg, lang, nick) -> tuple[int, str] | None:
         brands = cfg["captions"]["brand_words"]
         for r in con.execute("SELECT id, text, source, source_name FROM captions "
                              "WHERE lang = ? AND used_at IS NULL ORDER BY random()", (lang,)).fetchall():
             bio = re.sub(r"\n{3,}", "\n\n", HASHTAG_RE.sub("", r["text"])).strip()
             if not (0 < len(bio) <= cfg["captions"]["bio_max_chars"]) or \
                     has_brand(bio, brands + [r["source"], r["source_name"]]):
                 continue
             with con:
                 cur = con.execute("UPDATE captions SET used_at = ?, used_for = ? WHERE id = ? AND used_at IS NULL",
                                   (db.now_iso(), f"bio:{nick}", r["id"]))
             if cur.rowcount == 1:          # 0 = the planner took it a moment ago, try the next one
                 return r["id"], bio
         return None

     def release_bio(con, caption_id):
         with con:
             con.execute("UPDATE captions SET used_at = NULL, used_for = NULL WHERE id = ?", (caption_id,))
     ```
     Dry-run: pick without reserving (the read-only DB rejects the UPDATE), just print the chosen bio.
   - Browser: `browser_lock` + `session` -> `goto https://www.instagram.com/accounts/edit/` -> health check -> click `BIO_TEXTAREA`, select all (`Control+A`), type the bio with human delays -> dry-run: screenshot `before_bio_submit`, stop -> click `BIO_SUBMIT` -> reload -> read the textarea value and compare with the bio after collapsing whitespace -> set `accounts.bio_set_at`, `bio_caption_id` (the caption is already reserved). Any failure in the browser part -> `release_bio`, then re-raise.
   - Selectors (starting points, verify in the dry-run screenshot, fix only in `ig_selectors.py`):
     ```python
     BIO_TEXTAREA = ['textarea#pepBio', 'textarea[name="biography"]',
                     'textarea[aria-label="Bio"]', 'textarea[aria-label="О себе"]']
     BIO_SUBMIT = ['div[role="button"]:text-is("Submit")', 'div[role="button"]:text-is("Отправить")',
                   'button:text-is("Submit")', 'button:text-is("Отправить")']
     ```
5. Audience geography (needed for the pilot comparison): Apify without login cannot see where viewers are. The operator reads it in the Instagram app (reel insights, audience by country; needs a professional account, which Playerok's 500k+ stats screen recording needs anyway) and records it with the bot command `/geo <pub_id> <percent of views from Russia>` (Task 4.4). Reports compare languages (Task 5.2).

**Tests (`tests/test_captions.py`):**
```python
B = ["公式", "official", "ディズニー", "Disney", "disneyjp"]
# cfg: keep_hashtags True, min_chars 15, blocked_patterns from config.example.toml
EX1 = "サンセットに染まるサントロペで過ごす、忘れられない特別な夜。優雅なシルクの輝きと夕陽の黄金色、そして沢な雰囲気が、まるで夢のようなひとときを演出しました。"
EX2 = "🦕興行収入100億円突破 ・ 時が流れても変わらないものとは...? ・ かつて子どもだったすべての人へ #トイストーリー30周年 #トイストーリー5 #ディズニー写真部"

def test_clean_caption(cfg):
    f = lambda t, lang="japanese": captions.clean_caption(t, lang, B, cfg)
    assert f(EX1) == EX1
    r = f(EX2); assert "#ディズニー写真部" not in r and "#トイストーリー5" in r       # brand hashtag removed
    assert f("【ディズニー公式】\nトイストーリー5、ついに公開！楽しみ\nhttps://disney.jp @disneyjp\n#映画 #ディズニー") \
        == "トイストーリー5、ついに公開！楽しみ\n\n#映画"
    assert f("Disney Japan official\n夏の思い出をもう一度、海辺の風景とともに") == "夏の思い出をもう一度、海辺の風景とともに"
    assert f("ディズニーの夏、始まる") is None                       # single line with a brand
    assert f("Beautiful sunset at the beach tonight") is None        # not Japanese
    assert f("戦争の映画を観た夜、静かに考える") is None                # forbidden topic
    assert f("短い") is None                                          # too short
    assert f("Подпишись @memes_ru! Вечер у моря, тёплый ветер и тишина https://t.me/x", "russian") \
        == "Подпишись ! Вечер у моря, тёплый ветер и тишина"
    # keep_hashtags = False -> no "#" left in EX2

def test_take_caption_once(con):
    # 2 free japanese captions: two takes return both, the third returns None; lang 'none' -> (None, '')

def test_reserve_bio(con, cfg):
    # candidates: a 200-char caption, one containing "Disney", one 80-char clean caption with "#旅行"
    # -> returns the 80-char one, without the hashtag
```

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] Real run with 2 Japanese donors: `sqlite3 data/clips.db "select lang, count(*), sum(used_at is null) from captions group by 1"` shows new rows; a second run right after adds none.
- [ ] HUMAN CHECK: the owner reads 20 random cleaned captions (with a translator): no brand header lines, no @mentions or links, neutral topics.
- [ ] Test: while a bio caption is reserved, `take_caption` never returns it; after `release_bio` it is free again.
- [ ] HUMAN CHECK: `set-bio --account X --dry-run` screenshot shows the bio typed; the real run shows it on the profile; a second run refuses.

### Task 3.6: Planner (spec module 9)

**Why:** spread reels over active accounts within daily limits at random times inside a window, never put the same reel on two accounts within N days, and give flops a second chance.

**Files:** create `clips/planner.py`; create `tests/test_planner.py`.

**Steps:**

1. `pick_slot(now_utc, day_local, existing: list[datetime], p, tz, rng) -> datetime | None` (pure): window `[window_start, window_end]` on `day_local` in `tz`; lower bound `max(window_start, now + lead_minutes)`; try 50 uniform random minutes in the window; accept the first one at least `min_gap_minutes` away from every time in `existing`; return UTC or None.
2. Free sources query (fresh first):
   ```sql
   SELECT s.* FROM sources s
   WHERE s.state = 'ready' AND s.posted_at >= :min_posted        -- now - max_source_age_hours
     AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.source_id = s.id AND p.state != 'failed')
   ORDER BY s.score DESC
   ```
3. Repost candidates (used only when no fresh source is left), sources where:
   - every non-failed publication is `published` with `last_views < repost_max_views`;
   - the newest of them was published at least `max(repost_after_days, cross_account_gap_days)` days ago;
   - count of non-failed publications `< 1 + max_reposts`;
   - target account has never posted this source.
   New row: `attempt = max(attempt) + 1`. If the source file was deleted by maintenance, call `download_one` again (yt-dlp fallback handles expired Apify URLs).
4. `plan(cfg, con, dry)`:
   - `sync_accounts`, `resume_paused_accounts` (+ Telegram "Аккаунт @nick снова активен").
   - Expire stale slots: publications `planned`/`built` with `scheduled_at < now - max_delay_hours` -> `failed`, error `slot_expired`, delete their built file.
   - For each config account with DB status `active`: `taken` = its publications scheduled today (local day) with state not `failed`; for each of `daily_limit - taken` free slots: pick a source (fresh, else repost for this account), pick a slot (None -> stop for this account), then in ONE transaction: `captions.take_caption(con, account caption_lang, ...)` (pool empty for that language -> skip the slot, log WARNING; the daily summary already warned), insert the publication (`state='planned'`, `caption`, `caption_id`, `caption_lang`, `created_at`), set `captions.used_for = 'pub:<id>'`. The `UNIQUE(source_id, attempt)` constraint protects against double planning; catch `sqlite3.IntegrityError` and skip (the transaction rollback also frees the caption).
   - Build every `planned` publication (including ones from earlier runs): `out = data/build/pub_{id}.mp4`, `video.build(source.file_path, account, cfg, out, text=...)`, store `file_path`, `background`, `banner_type`, `banner_file`, `state='built'`. Build/validation error -> publication `failed`, source `failed` with reason, Telegram alert.
   - Dry-run: print the plan table (account, local time, source shortcode, score, attempt), build nothing.

**Tests (`tests/test_planner.py`):**
```python
def test_pick_slot_respects_window_and_gap():
    rng = random.Random(1)
    # now = 09:00 local, window 10:00-22:00, existing slot at 12:00, gap 120 min
    # 100 calls: every result is within the window, >= now + lead, and |result - 12:00| >= 120 min

def test_pick_slot_none_when_window_passed():
    # now = 23:00 local -> None

def test_plan_respects_daily_limit_and_is_idempotent(con, cfg):
    # 2 active accounts with daily_limit 1, 5 ready sources -> after plan: 2 publications, distinct sources
    # plan again -> still 2 publications

def test_same_source_never_on_two_accounts(con, cfg):
    # 1 ready source, 2 accounts -> exactly 1 publication

def test_paused_account_not_planned(con, cfg): ...

```
(For planner tests, monkeypatch `video.build` to write a dummy file and return a fixed dict.)

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] `python -m clips plan --dry-run` on real data prints a sensible plan; `plan` builds files into `data/build/` and each built file passes `python -m clips validate`.

### Task 3.7: Publish job

**Why:** turns due, built publications into posts, sequentially, with human-like pacing.

**Files:** modify `clips/instagram.py`, `clips/__main__.py`.

**Steps:**

1. `run_publish(cfg, con, args, dry)`:
   - `with browser_lock(cfg)` (exits quietly if another browser job runs).
   - `sync_accounts`, `resume_paused_accounts`.
   - Due list: `state='built' AND scheduled_at <= now` joined with accounts `status='active'`, ordered by `scheduled_at`, limit `publish.max_per_run`.
   - Skip a publication if its account published anything less than `min_gap_minutes` ago (guards against catch-up bursts after downtime).
   - For each: `publish_one(...)`, then sleep `between_accounts_sec`.
2. Never publish a file that has not passed `validate` (H4): `publish_one` re-runs `video.validate` on the built file (fast) and marks `failed` on problems.

**Acceptance criteria (Phase 3 exit):**
- [ ] `python -m clips trends && python -m clips plan && python -m clips publish --dry-run` run end-to-end on one real account with `daily_limit = 1`.
- [ ] HUMAN CHECK: let it run for 2 days on one account (timers can be started by hand or with `systemd-run --on-calendar`): exactly `daily_limit` posts per day, inside the window, no duplicates, every caption is in the account's language and none repeats across accounts.

---

## Phase 4: Stats, submit logic, Telegram (spec modules 12-14)

### Task 4.1: Stats collection (spec module 12)

**Why:** daily views of every reel younger than the deadline, as history, not just the last value (needed for the 48 h growth rule).

**Files:** create `clips/submit.py` (stats part).

**Steps:**

1. `collect(cfg, con, dry, from_file=None)`:
   - usernames = accounts with status not `banned` that have publications in the last `deadline_days + 1` days.
   - One actor call: `{"username": usernames, "resultsLimit": stats_results_per_account, "onlyPostsNewerThan": f"{deadline_days + 1} days", "skipPinnedPosts": false}` (our own pinned reels need stats too). `dump_raw(cfg, "stats", items)`.
   - Map `shortCode` -> publication (`state='published'`). For each match: `INSERT OR IGNORE INTO view_snapshots(publication_id, taken_at, views, likes)` with `taken_at = now`, and `UPDATE publications SET last_views = ?`.
   - Resolve `unconfirmed` publications: an item with `ownerUsername == account`, `timestamp >= share_clicked_at - 10 min`, caption starting with the publication caption's first 30 chars (compare after collapsing whitespace on both sides; an empty caption matches on owner and time only), and a shortcode not yet in the DB -> set `state='published'`, `published_at = item timestamp`, `ig_shortcode`, `ig_url`. Unresolved after 48 h -> alert once "Не найден рилс pub#{id} @{nick}, проверьте вручную".
   - Published reels younger than `deadline_days` missing from the results: WARNING log; if `submit_status` is `candidate`/`submit_now`, alert once (the reel may have been removed by Instagram).
2. HUMAN CHECK (one time, during this task): for 3 of our reels compare the views number in the Instagram app with `videoPlayCount` and `videoViewCount` from the raw dump. Set `apify.views_field` to the one that matches what the app shows (Playerok records what the app shows). Note the result in `docs/RUNBOOK.md`.

**Acceptance criteria:**
- [ ] `python -m clips stats --from-file <saved raw dump>` twice creates snapshots for each matched reel (one per run, different `taken_at`), no errors.
- [ ] `sqlite3 data/clips.db "select publication_id, taken_at, views from view_snapshots order by 1,2"` shows the history.

### Task 4.2: Submit logic (spec module 13)

**Why:** submit each reel at the moment that maximizes the payout without missing the deadline. Views are frozen at the moment of submission (Playerok rule), so timing matters.

**Files:** modify `clips/submit.py`; create `tests/test_submit.py`.

**Steps:**

1. Pure functions, exactly this logic:
   ```python
   def payout_rub(views: int, voiced: bool, p: dict) -> int:
       if views < p["min_paid_views"]:
           return 0
       paid = views // p["step_views"] * p["step_views"]
       rate = p["rate_voiced_rub_per_100k"] if voiced else p["rate_static_rub_per_100k"]
       return min(paid * rate // 100_000, p["max_payout_rub"])

   def growth_pct(views_now: int, views_then: int | None) -> float | None:
       if views_then is None or views_then <= 0:
           return None
       return (views_now - views_then) / views_then * 100

   def decide(views, age_days, views_48h_ago, account_status, p) -> tuple[str, str | None]:
       """Return (submit_status, reason) for a published reel that is not yet submitted."""
       if age_days > p["deadline_days"]:
           return ("expired" if views >= p["min_paid_views"] else "low"), None
       if views < p["min_paid_views"]:
           return "low", None
       if account_status != "active":
           return "submit_now", "account_in_danger"
       if age_days >= p["submit_by_day"]:
           return "submit_now", "age"
       g = growth_pct(views, views_48h_ago)
       if g is None or g >= p["stall_growth_pct_48h"]:
           return "candidate", None
       # Stalled. Wait only if the same absolute growth over the next 48 h crosses the next paid step
       # and we still have 2 days before the submit-by day.
       if p["wait_for_next_step"] and age_days + 2 <= p["submit_by_day"]:
           next_step = (views // p["step_views"] + 1) * p["step_views"]
           if views + (views - views_48h_ago) >= next_step:
               return "candidate", "waiting_next_step"
       return "submit_now", "stalled"
   ```
2. `views_48h_ago(con, pub_id, now) -> int | None`: the latest snapshot with `taken_at <= now - 48h + 3h` (3 h tolerance because the daily job does not run at exactly the same second). None if there is none.
3. `run_submit_check(cfg, con, dry)`: for every `published` publication with `submit_status` NULL, `low`, or `candidate` (never touch `submit_now`, `submitted`, `approved`, `rejected`, `expired`: `submit_now` is sticky so the operator is never told "submit" and then "wait"):
   - `views = last_views` (skip if None), `age_days = (now - published_at) / 1 day`.
   - Compute `decide(...)`, store `submit_status`, `submit_reason`, `growth_48h_pct`, and `needs_screen_recording = views >= screen_recording_views`.
   - For newly `submit_now` rows with `notified_at IS NULL`: send the Telegram message (Task 4.4) and set `notified_at`. This makes notifications idempotent.
   - Also re-evaluate when an account changes to `paused`/`check`/`banned` (call `run_submit_check` from `apply_health`).
4. `stats` command = `collect` + `run_submit_check` + `notion.sync` (if Notion is configured; Phase 5).

**Tests (`tests/test_submit.py`):**
```python
P = dict(rate_static_rub_per_100k=400, rate_voiced_rub_per_100k=700, min_paid_views=50_000,
         step_views=50_000, max_payout_rub=10_000, deadline_days=21, submit_by_day=19,
         stall_growth_pct_48h=5.0, screen_recording_views=500_000, wait_for_next_step=True)

def test_payout():
    assert submit.payout_rub(49_999, True, P) == 0
    assert submit.payout_rub(50_000, False, P) == 200
    assert submit.payout_rub(120_000, True, P) == 700
    assert submit.payout_rub(2_000_000, True, P) == 10_000

def test_decide():
    d = submit.decide
    assert d(40_000, 5, 30_000, "active", P) == ("low", None)
    assert d(80_000, 5, 50_000, "active", P) == ("candidate", None)               # growing 60%
    assert d(80_000, 5, 79_000, "active", P) == ("submit_now", "stalled")         # +1.3%, next step far
    assert d(98_000, 5, 95_000, "active", P) == ("candidate", "waiting_next_step")# +3.2%, 98k+3k >= 100k
    assert d(98_000, 18, 95_000, "active", P) == ("submit_now", "stalled")        # too late to wait
    assert d(60_000, 19, 30_000, "active", P) == ("submit_now", "age")
    assert d(60_000, 3, None, "check", P) == ("submit_now", "account_in_danger")
    assert d(60_000, 3, None, "active", P) == ("candidate", None)                 # no 48 h history yet
    assert d(60_000, 22, 59_000, "active", P) == ("expired", None)
```

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] `python -m clips submit-check --dry-run` prints the decision for every published reel and the messages it would send; a second real run sends nothing new.

### Task 4.3: Telegram sending

**Why:** the operator gets everything in Telegram: submit now, account problems, pipeline errors, daily summary (spec module 14).

**Files:** create `clips/telegram.py`.

**Steps:**

1. Plain Bot API over `requests` (no bot framework):
   ```python
   def _api(method, **kw):
       token = config.secret("TELEGRAM_BOT_TOKEN")
       r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60, **kw)
       r.raise_for_status()
       return r.json()["result"]

   def operators() -> list[int]:
       return [int(x) for x in os.environ.get("TELEGRAM_OPERATOR_IDS", "").split(",") if x.strip()]

   def send(text: str, buttons=None, photo: Path | None = None, dry=False):
       """buttons: list of rows, each row a list of (label, callback_data)."""
       markup = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in row]
                                     for row in buttons]} if buttons else None
       if dry:
           print(f"[telegram dry-run] {text} {buttons or ''} {photo or ''}")
           return
       for chat in operators():
           if photo:
               with open(photo, "rb") as f:
                   _api("sendPhoto", data={"chat_id": chat, "caption": text[:1024], "parse_mode": "HTML",
                        **({"reply_markup": json.dumps(markup)} if markup else {})}, files={"photo": f})
           else:
               _api("sendMessage", json={"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                    "disable_web_page_preview": True, **({"reply_markup": markup} if markup else {})})
   ```
   Escape every dynamic value with `html.escape`.
2. Message builders (pure functions returning `(text, buttons)`, Russian, no em dash):
   - `msg_submit(pub, account_status, p)`:
     ```
     🟢 Пора подавать: pub#{id}
     @{account} · {ig_url}
     Просмотры: {views:,} (рост за 48 ч: {growth:.1f}%)
     Причина: {застой | 19-й день | аккаунт в опасности}
     Баннер: {с озвучкой | обычный}
     Ожидаемая сумма: {payout} ₽
     Подать до: {dd.mm} (дедлайн {dd.mm})
     ⚠️ Нужна запись экрана статистики с телефона     (only if needs_screen_recording)
     Запишите долю просмотров из России: /geo {id} <процент>     (only if needs_screen_recording)
     ```
     buttons: `[("Подано", f"sub:{id}")], [("Одобрено", f"ok:{id}"), ("Отклонено", f"rej:{id}")]`.
   - `msg_account(nick, status, reason)` with button `[("Проверку прошёл", f"acc:{nick}")]` for status `check`.
   - `msg_error(command, exc)`.
   Format numbers with a space as thousands separator for Russian readers: `f"{n:,}".replace(",", " ")`.

**Tests (`tests/test_telegram.py`):** `msg_submit` contains the URL, the payout, the deadline date; contains the screen-recording line only for 500k+ views; `chr(0x2014) not in text` (no em dash).

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] HUMAN CHECK: with a real bot token, `python -c "from clips import telegram; telegram.send('test')"` arrives to every operator id.

### Task 4.4: Telegram bot (buttons)

**Why:** the operator marks what happened: submitted, approved + amount, rejected + reason, check solved. Submission itself stays manual (H3).

**Files:** modify `clips/telegram.py`; extend `tests/test_telegram.py`.

**Steps:**

1. `handle_update(con, cfg, update, pending: dict) -> list[tuple[int, str]]` (pure-ish: DB writes + returns replies `(chat_id, text)`; the loop sends them). Rules:
   - Ignore (log WARNING) updates from user ids not in `operators()`.
   - `callback_query` data:
     - `sub:{id}` -> `submit_status='submitted'`, `submitted_at=now`, `submitted_views=last_views`. Reply "Отмечено: pub#{id} подан ({views} просмотров)".
     - `ok:{id}` -> `pending[chat] = ("ok", id)`; reply "Введите сумму в рублях для pub#{id}".
     - `rej:{id}` -> `pending[chat] = ("rej", id)`; reply "Введите причину отказа для pub#{id}".
     - `acc:{nick}` -> `set_account_status(nick, 'active')`; reply "Аккаунт @{nick} снова активен".
     - Always call `answerCallbackQuery` (in the loop) so the button stops spinning.
   - `message.text`:
     - if `pending[chat]` is `("ok", id)`: parse an integer (strip spaces and "₽"); must be `0 <= amount <= max_payout_rub`, else reply "Нужна сумма числом от 0 до 10000" and keep pending; on success: `submit_status='approved'`, `amount_rub`, `decided_at=now`; clear pending; reply.
     - if `("rej", id)`: `submit_status='rejected'`, `reject_reason=text[:500]`, `decided_at=now`; clear; reply.
     - `/status`: reply with the summary text from Task 4.5.
     - `/geo <pub_id> <percent>`: integer 0..100, store `publications.ru_audience_pct`; reply "pub#{id}: Россия {p}%". Any reel can be recorded, not only 500k+ (the pilot needs a sample from every language group).
     - anything else: reply a 3-line help.
   - All writes are idempotent (pressing a button twice sets the same values).
2. `run_bot(cfg, con, args, dry)`: long polling loop:
   ```python
   offset, pending = 0, {}
   while True:
       try:
           updates = _api("getUpdates", json={"offset": offset, "timeout": 50,
                                              "allowed_updates": ["message", "callback_query"]})
       except requests.RequestException as e:
           log.warning("getUpdates failed: %s", e); time.sleep(5); continue
       for u in updates:
           offset = u["update_id"] + 1
           try:
               for chat, text in handle_update(con, cfg, u, pending):
                   _api("sendMessage", json={"chat_id": chat, "text": text})
               if "callback_query" in u:
                   _api("answerCallbackQuery", json={"callback_query_id": u["callback_query"]["id"]})
           except Exception:
               log.exception("update %s failed", u.get("update_id"))
   ```
   `# ponytail: pending answers live in memory; after a bot restart the operator presses the button again`

**Tests:** feed fake update dicts into `handle_update` with an in-memory DB: `sub` sets submitted; `ok` then text "1 400 ₽" sets approved 1400; `ok` then "abc" keeps pending; `rej` then text sets rejected; `acc` sets active; a non-operator id changes nothing.

**Acceptance criteria:**
- [ ] Tests pass.
- [ ] HUMAN CHECK: run `python -m clips bot`, trigger a fake submit message for a test publication (`submit-check` on a DB row edited to 60k views), press each button, check the DB.

### Task 4.5: Daily summary

**Why:** one message a day tells the operator the state of the whole network.

**Files:** create `clips/reports.py` (summary part).

**Steps:** `summary_text(con, cfg) -> str` (also used by `/status`):
```
📊 Сводка за {date}
Опубликовано вчера: {n} ({per account})
Ошибки вчера: {failed count}
Аккаунты: активны {a}, пауза {p} ({nicks}), проверка {c} ({nicks}), бан {b}
Пора подавать: {n}
  pub#{id} @{acc} {views} просм., подать до {dd.mm}
Кандидаты: {n} · Поданы, ждут решения: {n}
Одобрено в этом месяце: {sum} ₽
Свободных подписей: japanese {n}, russian {m}     (⚠️ before a language with fewer than captions.low_pool_alert, only languages in use)
Свободно на диске: {gb} ГБ
```
`summary` command sends it (prints in dry-run).

**Acceptance criteria (Phase 4 exit):**
- [ ] `python -m clips summary --dry-run` prints a correct summary for the current DB (check one number by hand with sqlite3).
- [ ] The whole chain works on real data for at least 3 days: snapshots daily, statuses change, the operator receives and answers at least one "Пора подавать".

---

## Phase 5: Notion, reports, maintenance (spec modules 15-17)

### Task 5.1: Notion setup and sync (spec module 15)

**Why:** Notion is a read-only mirror for reports. SQLite stays the source of truth; sync is one-way (DB -> Notion), never read back.

**Files:** create `clips/notion.py`; extend tests.

**Steps:**

1. HTTP: `requests` to `https://api.notion.com/v1`, headers `Authorization: Bearer {NOTION_TOKEN}`, `Notion-Version: {notion.version}`, `Content-Type: application/json`. Sleep 0.35 s between calls (Notion allows about 3 requests/second). On HTTP 429 sleep `Retry-After` seconds and retry once.
2. `notion-setup --parent-page ID`: create two databases under the page (`POST /v1/databases`), print their IDs to put into `.env`:
   - "Публикации": `Ролик` (title), `Донор` (select), `Оригинал` (url), `Ссылка` (url), `Аккаунт` (select), `Дата публикации` (date), `Дедлайн` (date), `Подать до` (date), `Просмотры` (number), `Рост 48ч %` (number), `Статус` (select: Мало, Кандидат, Пора подавать, Подан, Одобрен, Отклонён, Просрочен), `Сумма ₽` (number), `Причина отказа` (rich_text), `Баннер` (select: обычный, с озвучкой), `ID` (number, our publication id).
   - "Аккаунты": `Ник` (title), `Профиль AdsPower` (rich_text), `Статус` (select: активен, пауза, проверка, бан), `Подписчики` (number), `Просмотры 7д` (number), `Просмотры 30д` (number), `Доход ₽` (number).
3. Pure property builders (unit-tested): `pub_properties(row, source_row, cfg) -> dict`, `account_properties(nick, db_row, cfg, views7, views30, income) -> dict`. Title of a publication: `"@{account} {dd.mm.yyyy} {ig_shortcode or 'pub#'+id}"`. Deadline = `published_at + deadline_days`, submit-by = `published_at + submit_by_day` (dates in local timezone). Skip properties whose value is None (send `{"number": None}` only where clearing is intended).
4. `sync(cfg, con, dry)`:
   - Publications with `published_at IS NOT NULL AND notion_dirty = 1`: if `notion_page_id` is NULL -> `POST /v1/pages` with `parent: {"database_id": NOTION_PUBLICATIONS_DB}`, store the returned id; else `PATCH /v1/pages/{id}`. Then `UPDATE publications SET notion_dirty = 0, notion_page_id = ?` (this update does not re-trigger the dirty flag; see the trigger column list).
   - Accounts: all of them every run (at most 20 rows). Views 7d/30d = sum of `last_views` of the account's reels published in the last 7/30 days. Income = sum of `amount_rub` of approved reels (all time).
   - Dry-run: print payloads.
5. `notion-sync` command; also called at the end of `stats`.

**Acceptance criteria:**
- [ ] Unit test: `pub_properties` for a sample row has the right status name ("Пора подавать" for `submit_now`) and dates.
- [ ] HUMAN CHECK: `notion-setup` creates both databases; `notion-sync` fills them; changing a row (e.g. pressing "Подано" in the bot) shows up after the next sync; a sync with nothing dirty makes zero publication API calls (check the log).

### Task 5.2: Weekly report (spec module 16)

**Why:** learn what works: which accounts, donors, backgrounds, banner types and caption languages bring views and money, and what share of reels passes 50k.

**Files:** modify `clips/reports.py`; create `tests/test_reports.py`.

**Steps:**

1. `weekly(con, cfg, now) -> str`: two parts.
   - Reels published in the last 7 days, grouped separately by `account`, `donor` (via source), `background`, `banner_type`, `caption_lang`: count, total views (`last_views`), median views, `% >= min_paid_views`. For `caption_lang` also: average `ru_audience_pct` over reels that have it, and how many have it.
   - Money decided in the last 7 days (`decided_at`): approved sum by account; rejected count with reasons (top 5).
   - Totals line: reels, views, % >= 50k, approved ₽, pending submissions.
2. Format as `<pre>` monospace tables, top 10 rows per group by views, keep under 4096 characters (truncate groups, never the totals).
3. `report` command sends it.

**Tests:** small in-memory DB with 4 publications across 2 accounts: check counts, sums, and the 50k percentage.

**Acceptance criteria:**
- [ ] Test passes; `python -m clips report --dry-run` prints a readable report.

### Task 5.3: Maintenance (spec module 17)

**Why:** the disk must not fill up, the DB must be backed up, logs must not grow forever.

**Files:** create `clips/maintenance.py`.

**Steps:** `run_maintenance(cfg, con, dry)`; each step logs what it deletes (in dry-run only logs):
1. Built files: delete `file_path` of publications in `published`/`unconfirmed`/`failed` (published ones are normally already deleted by `publish_one`), set `file_path = NULL`.
2. Source files: delete files of sources posted more than `keep_source_days` ago that have no `planned`/`built` publication; set `file_path = NULL`. Duplicate/failed sources: delete right away.
3. `data/screenshots/*` and `data/raw/*` older than `keep_debug_days`.
4. Orphans: files in `data/build/` and `data/tmp/` not referenced by the DB and older than 1 day.
5. Backup: `src.backup(dst)` via `sqlite3` into `data/backups/clips-{YYYYmmdd}.db` (online, safe with WAL); keep the newest `keep_backups`. Off-server copies are out of scope (see Owner inputs).
6. Disk: `shutil.disk_usage(data_dir).free`; below `min_free_disk_gb` -> Telegram alert.
7. Logs: journald does rotation (Task 6.1). Nothing to do in code.

**Acceptance criteria (Phase 5 exit):**
- [ ] `python -m clips maintenance --dry-run` lists files it would delete and none of them is referenced by a `planned`/`built` publication.
- [ ] After a real run, a backup file exists and opens: `sqlite3 data/backups/clips-*.db "select count(*) from publications"`.

---

## Phase 6: Deployment and pilot

### Task 6.1: systemd units

**Why:** everything runs unattended on a schedule (spec). Schedules live in timer files (they are config files, not code); posting windows live in `config.toml`.

**Files:** create everything under `deploy/`.

**Steps:**

1. Server layout: repo at `/opt/clips`, system user `clips` (owns `/opt/clips/data`), venv at `/opt/clips/.venv`, `.env` mode 600.
2. `deploy/systemd/adspower.service`:
   ```ini
   [Unit]
   Description=AdsPower headless client
   After=network-online.target
   [Service]
   User=clips
   EnvironmentFile=/opt/clips/.env
   ExecStart=/usr/bin/xvfb-run -a "/opt/AdsPower Global/adspower_global" --headless=true --api-key=${ADSPOWER_API_KEY} --api-port=50325
   Restart=always
   RestartSec=10
   [Install]
   WantedBy=multi-user.target
   ```
   Check the real binary path after installing the AdsPower .deb (`dpkg -L` on the package). Try first without `xvfb-run -a`; keep it only if the client refuses to start without a display. Headless mode needs AdsPower client v3.3.2+ / kernel 2.4.2.8+ and a plan with Local API access (AdsPower docs). The API key cannot be changed while headless mode runs.
3. `deploy/systemd/clips@.service` (template for all oneshot commands):
   ```ini
   [Unit]
   Description=clips %i
   After=network-online.target adspower.service
   [Service]
   Type=oneshot
   User=clips
   WorkingDirectory=/opt/clips
   EnvironmentFile=/opt/clips/.env
   ExecStart=/opt/clips/.venv/bin/python -m clips %i
   TimeoutStartSec=3h
   ```
   A timer never starts a second copy of a unit that is still running, so the same command never overlaps itself. Different browser commands are serialized by `browser.lock` (H7).
4. `deploy/systemd/clips-bot.service`: `Type=simple`, `ExecStart=... -m clips bot`, `Restart=always`, `RestartSec=5`.
5. Timers (`deploy/systemd/clips-<name>.timer`, each with `Unit=clips@<command>.service`, `Persistent=true` except publish):
   | Timer | OnCalendar | Command |
   |-------|------------|---------|
   | clips-trends.timer | `*-*-* 00/3:15:00` | trends |
   | clips-plan.timer | `*-*-* *:05:00` | plan |
   | clips-publish.timer | `*:0/10` | publish |
   | clips-stats.timer | `*-*-* 06:20:00 Europe/Moscow` | stats |
   | clips-summary.timer | `*-*-* 09:40:00 Europe/Moscow` | summary |
   | clips-report.timer | `Mon *-*-* 10:10:00 Europe/Moscow` | report |
   | clips-notion.timer | `*-*-* *:45:00` | notion-sync |
   | clips-maintenance.timer | `*-*-* 04:30:00 Europe/Moscow` | maintenance |
   | clips-captions.timer | `Sun *-*-* 05:10:00 Europe/Moscow` | harvest-captions |
   Example:
   ```ini
   [Unit]
   Description=clips trends every 3 hours
   [Timer]
   OnCalendar=*-*-* 00/3:15:00
   Persistent=true
   Unit=clips@trends.service
   [Install]
   WantedBy=timers.target
   ```
6. `deploy/journald.conf.d/clips.conf` (install to `/etc/systemd/journald.conf.d/`):
   ```ini
   [Journal]
   SystemMaxUse=2G
   MaxRetentionSec=30day
   ```
7. README install section: copy units to `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now adspower clips-bot clips-*.timer`.

**Acceptance criteria:**
- [ ] `systemd-analyze verify deploy/systemd/*` reports no errors.
- [ ] `systemctl list-timers 'clips-*'` shows all 9 timers with next run times.
- [ ] `journalctl -u 'clips@*' --since today` shows runs of each command.
- [ ] Reboot the server: AdsPower, the bot and all timers come back without manual steps.

### Task 6.2: Runbook

**Why:** the operator and the admin need short, exact instructions that are not code.

**Files:** create `docs/RUNBOOK.md`.

**Content (keep it short, one section each):**
1. Add a new account: create the AdsPower profile with its proxy, log in to Instagram by hand, switch the account to a professional (creator) account, add `[[accounts]]` to `config.toml` (with `caption_lang`), put the background image, `python -m clips sync-accounts`, `python -m clips health --account X`, `python -m clips set-bio --account X`.
2. Account went to "проверка": what to do in AdsPower, then press "Проверку прошёл".
3. Change banners (e.g. new Playerok campaign): replace files, update `[banners]`, run `build` + `validate` on a sample, check on a phone.
4. Instagram UI changed (publish fails with `LookupError`): open the latest screenshot + HTML in `data/screenshots/`, fix `clips/ig_selectors.py`, run `publish-file --dry-run`.
5. Logs: `journalctl -u clips@publish -n 200`, `journalctl -u clips-bot -f`.
6. Restore a backup: stop timers and bot, copy the backup over `data/clips.db`, start again.
6a. Caption pool runs low (daily summary shows ⚠️): add more popular accounts to `captions.donors_<lang>` and run `python -m clips harvest-captions` now instead of waiting for Sunday.
7. The `apify.views_field` decision from Task 4.1.
8. Monthly: `.venv/bin/pip install -U yt-dlp` (Instagram breaks old versions).

**Acceptance criteria:**
- [ ] The owner reads it and confirms every step is clear.

### Task 6.3: Pilot (3-5 accounts, 3-4 weeks)

**Why:** spec: scale to 20 accounts only after a pilot.

**Steps:**
1. Week 1: 3 accounts, `daily_limit = 1`. Watch every alert. Fix selectors and thresholds. Split caption languages from day one: at least 2 accounts `japanese` and 2 `none` (no caption, the control group; owner decided not to use Russian caption donors) once there are 4+ accounts. Use the same video donors for both groups so only the caption differs.
2. Weeks 2-4: up to 5 accounts, `daily_limit = 2`. Weekly: read the report, tune `trends.views_to_median_ratio`, `min_views`, donors list, `filter.blocked_patterns`.
3. Track per week (from the weekly report): reels posted, % reaching 50k, approved RUB, rejection reasons, account incidents (checks, blocks, bans), Apify spend. Per caption language: median views per reel, % reaching 50k, average Russian audience share from `/geo` (record it for every reel with 50k+ views), Playerok rejections that mention geography or audience.
3a. Proposed decision after the pilot (owner decides): keep `japanese` only if its median views are not lower than `none`, its Russian audience share is at most 10 points lower, and Playerok rejected no Japanese-caption reel over geography. Otherwise switch every account to `none` (config only). With 4-5 accounts the result is a hint, not proof.
4. Proposed go/no-go for scaling (owner decides): no bans caused by the pipeline in the last 2 weeks; at least one approved payout per active account; Playerok rejection rate under 20%; zero double posts; zero reels published without a valid banner.
5. Scale in steps of +3 to 5 accounts per week, not all at once.

**Acceptance criteria:**
- [ ] Owner signs off the pilot results and the scaling decision.

---

## 7. Optional (v2, only if the owner asks)

- **Topic filter on audio (recommended first v2 step):** swearing and politics are often in the audio, not the caption. Cheapest option: set Apify `includeTranscript: true` (paid per minute of audio) and run the same `blocked_reason` over `caption + transcript`. Alternative: local `faster-whisper` small model on CPU.
- **AI check of frames** (spec module 4 v2): sample 3 frames and ask a vision model for the forbidden categories. Only after the transcript filter proves insufficient.
- **2-3 profiles in parallel** (spec allows it): replace the single `browser.lock` with N lock files (`browser-0.lock` ... `browser-{N-1}.lock`), take the first free one. Only if one-at-a-time cannot keep up with the schedule.
- **Off-server backups:** `rclone copy data/backups remote:clips-backups` in maintenance.

---

## 8. Owner inputs and open questions (ask before or during the named task)

| # | Question / input | Needed by | Default if no answer |
|---|------------------|-----------|----------------------|
| 1 | Playerok banner files: the top sticker (transparent PNG, as in the competitor reels) and the voiced banner (format, transparency, where on screen it plays: over the circle, full screen?) | Task 1.2, 1.3 | PNG sticker on top; voiced MP4 drawn below the top banner at full width |
| 2 | Do Playerok's midroll timing rules ("shorter than 20 s", "at second 10-20") refer to the source length or the final reel length? | Task 1.3 | Source length; default insert at second 10 satisfies both readings for sources 13 s and longer |
| 3 | Banner position below Instagram's header | Task 1.2 | Resolved from competitor reels (section 1.1): centered, `top_y = 280`, `top_height_px = 360` |
| 4 | Background image, overlay texts and `caption_lang` for every account | Task 2.1 | none, required |
| 5 | Donor list (Instagram usernames) | Task 3.2 | none, required |
| 6 | AdsPower: plan with Local API, API key, profiles with proxies, Instagram logged in, profile ids; UI language of Instagram in the profiles (selectors cover EN and RU) | Task 2.2 | none, required |
| 7 | Tokens: Apify, Telegram bot + operator user ids, Notion integration + parent page id | Tasks 3.1, 4.3, 5.1 | none, required |
| 8 | Which Apify field equals the views number Playerok sees (`videoPlayCount` or `videoViewCount`) | Task 4.1 | `videoPlayCount`, verified by hand on 3 reels |
| 9 | Is the 21-day deadline counted from the publication time (our assumption)? | Task 4.2 | publication time |
| 10 | Downloads of donor videos (Apify CDN links, yt-dlp fallback) are anonymous and go from the server IP, not tied to any account. Acceptable, or route them through `download.proxy`? | Task 3.4 | server IP, no cookies |
| 11 | Off-server backup destination, if wanted | Task 5.3 | local backups only |
| 12 | A competitor reel posted on 15.11.2026 uses the plain Playerok logo, not the GTA VI banner. Is the plain logo still paid in November, or only GTA VI? | Task 1.2 | GTA VI banner only |
| 13 | Caption style | Task 3.5 | Resolved (addendum 1a): language per account; Japanese vs no caption compared in the pilot (no Russian donors) |
| 14 | Caption donors | Task 3.5 | Resolved 16.11.2026: 9 Japanese accounts in `captions.donors_japanese` (incl. anime: `onepiece_staff`); no Russian donors |
| 15 | Are all accounts professional (creator) accounts? Needed to see audience countries (`/geo`) and for Playerok's 500k+ stats recording | Task 3.5 | operator switches them during onboarding |

---

## 9. Requirements traceability (spec module -> tasks)

| Spec module | Tasks |
|-------------|-------|
| 1 Accounts | 0.2 (config), 0.3 (table), 2.1 |
| 2 Trends | 3.1, 3.2, 6.1 (every 3 h) |
| 3 Selection | 3.3 |
| 4 Topic filter | 3.3 (v1), section 7 (v2) |
| 5 Dedup | 3.4 |
| 6 Download | 3.4 |
| 7 Video build | 1.1, 1.2, 1.3, 1.4, 1.6 |
| 8 Build validation | 1.5, 3.7 |
| 9 Queue | 3.6 |
| 10 Publishing | 2.2, 2.4, 3.7 |
| 11 Account health | 2.3, 2.4 |
| 12 Stats | 4.1 |
| 13 Submit logic | 4.2 |
| 14 Telegram bot | 4.3, 4.4, 4.5 |
| 15 Notion | 5.1 |
| 16 Weekly report | 5.2 |
| 17 Maintenance | 5.3, 6.1 (journald) |
| Principles: schedule, config, .env, dry-run, logs, screenshots, idempotency, never delete posts | 0.1-0.4, 2.2, 2.4, 6.1, hard rules H1-H9 |
| Development order and pilot | Phases 1-6, Task 6.3 |
| Addendum 1a: Japanese captions and bios, per-account language, pilot comparison | 3.5, 3.6, 4.4, 5.2, 6.3, H10 |
