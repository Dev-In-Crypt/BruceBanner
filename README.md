# Playerok Clips pipeline

Unattended pipeline for the Playerok "Clips" program: takes trending Russian meme reels, rebuilds them
in the Playerok format (account background, video in a circle, Playerok banner, voiced midroll),
publishes them to a network of Instagram accounts through AdsPower, tracks views and tells the
operator in Telegram when to submit each reel to the Playerok bot.

- Plan for the AI coder: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
- Owner's spec (Russian): [docs/spec-ru.md](docs/spec-ru.md)
- Operator and admin runbook (Russian): [docs/RUNBOOK.md](docs/RUNBOOK.md)
- Status: all code phases (0-5) and deployment files (6) are written; live checks and the pilot are next.

## Install (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y ffmpeg python3-venv python3-pip sqlite3 fonts-dejavu-core xvfb
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp config.example.toml config.toml   # then edit: accounts, banners, donors
cp .env.example .env                 # secrets, never commit
```

Do not run `playwright install`: the pipeline connects to AdsPower's browser, not Playwright's.

## Run

```bash
.venv/bin/python -m clips --help
.venv/bin/python -m clips build --source meme.mp4 --account example_account --out data/tmp/reel.mp4
.venv/bin/python -m clips validate data/tmp/reel.mp4
.venv/bin/pytest -q
```

Every command accepts `--dry-run` (no DB writes, no posting, no messages).

## Server (systemd)

Units and timers are in `deploy/systemd/`, log limits in `deploy/journald.conf.d/`.
Install steps and the schedule: [docs/RUNBOOK.md](docs/RUNBOOK.md), section 0.
