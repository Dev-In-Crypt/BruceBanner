"""CLI: python -m clips <command> [--dry-run]. One command per pipeline step."""
import argparse
import json
import logging
import sys

from clips import config, db

log = logging.getLogger("clips")

# command -> phase in which it gets implemented (docs/IMPLEMENTATION_PLAN.md, section 2.6)
LATER = {
    "stats": 4, "submit-check": 4, "bot": 4, "summary": 4,
    "notion-setup": 5, "notion-sync": 5, "report": 5, "maintenance": 5,
}


def common_flags(suppress: bool) -> argparse.ArgumentParser:
    # Subcommands use SUPPRESS defaults so a flag given before the command is not reset by them.
    d = {"default": argparse.SUPPRESS} if suppress else {}
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--dry-run", action="store_true", help="no DB writes, no posting, no messages", **d)
    c.add_argument("--verbose", action="store_true", **d)
    c.add_argument("--config", help="path to config.toml", **d)
    return c


def parser() -> argparse.ArgumentParser:
    common = common_flags(suppress=True)
    p = argparse.ArgumentParser(prog="python -m clips", parents=[common_flags(suppress=False)])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", parents=[common], help="create the database")
    b = sub.add_parser("build", parents=[common], help="build one reel from a local file")
    b.add_argument("--source", required=True)
    b.add_argument("--account", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--no-midroll", action="store_true")
    b.add_argument("--text", help="text plate (needs compose.text_overlay = true)")
    v = sub.add_parser("validate", parents=[common], help="validate a built reel")
    v.add_argument("path")
    sub.add_parser("sync-accounts", parents=[common], help="add config accounts to the DB")
    s = sub.add_parser("account-status", parents=[common], help="set an account status by hand")
    s.add_argument("nick")
    s.add_argument("status", choices=["active", "paused", "check", "banned"])
    s.add_argument("--reason", default="manual")
    h = sub.add_parser("health", parents=[common], help="open the profile, check the account, close")
    h.add_argument("--account", required=True)
    f = sub.add_parser("publish-file", parents=[common], help="publish one local file (manual test)")
    f.add_argument("--account", required=True)
    f.add_argument("--file", required=True)
    f.add_argument("--banner-type", required=True, choices=["static", "voiced"])
    f.add_argument("--caption")
    for name, hlp in (("fetch-trends", "Apify reels of donors -> sources"), ("trends", "fetch + select + download"),
                      ("harvest-captions", "collect captions from popular accounts")):
        t = sub.add_parser(name, parents=[common], help=hlp)
        t.add_argument("--from-file", help="use a saved Apify JSON dump instead of calling Apify")
    sub.add_parser("select", parents=[common], help="pick sources worth posting (+ topic filter)")
    sub.add_parser("download", parents=[common], help="download selected sources + dedup")
    sb = sub.add_parser("set-bio", parents=[common], help="set the profile bio from the caption pool")
    sb.add_argument("--account", required=True)
    sb.add_argument("--force", action="store_true")
    sub.add_parser("plan", parents=[common], help="plan today's slots, build and validate them")
    sub.add_parser("publish", parents=[common], help="publish due built publications")
    for name, phase in LATER.items():
        sub.add_parser(name, parents=[common], help=f"(phase {phase})")
    return p


def account_cfg(cfg, nick) -> dict:
    for a in cfg["accounts"]:
        if a["nick"] == nick:
            return a
    raise SystemExit(f"account {nick!r} not in config")


def run_command(args, cfg, con) -> int:
    from clips import captions, instagram, planner, trends, video
    dry = args.dry_run
    if args.command in ("sync-accounts", "account-status", "health", "publish-file") and not dry:
        db.sync_accounts(con, cfg)
    if args.command == "init-db":
        print(cfg["data"] / "clips.db")
    elif args.command == "build":
        if args.text and not cfg["compose"]["text_overlay"]:
            raise SystemExit("--text needs compose.text_overlay = true")
        meta = video.build(args.source, account_cfg(cfg, args.account), cfg, args.out,
                           text=args.text, use_midroll=not args.no_midroll)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    elif args.command == "validate":
        problems = video.validate(args.path, cfg, video.probe(args.path)["duration"])
        print("OK" if not problems else "\n".join(problems))
        return 1 if problems else 0
    elif args.command == "sync-accounts":
        for r in con.execute("SELECT nick, status FROM accounts ORDER BY nick"):
            print(r["nick"], r["status"])
    elif args.command == "account-status":
        account_cfg(cfg, args.nick)
        if not dry:
            db.set_account_status(con, args.nick, args.status, args.reason)
        print(f"@{args.nick}: {args.status}")
    elif args.command == "health":
        instagram.run_health(cfg, con, account_cfg(cfg, args.account), dry)
    elif args.command == "publish-file":
        state = instagram.run_publish_file(cfg, con, account_cfg(cfg, args.account), args.file,
                                           args.banner_type, args.caption, dry)
        return 0 if state in ("published", "built") else 1
    elif args.command == "fetch-trends":
        trends.fetch(cfg, con, dry, args.from_file)
    elif args.command == "select":
        trends.select(cfg, con, dry)
    elif args.command == "download":
        trends.download(cfg, con, dry)
    elif args.command == "trends":
        trends.fetch(cfg, con, dry, args.from_file)
        trends.select(cfg, con, dry)
        trends.download(cfg, con, dry)
    elif args.command == "harvest-captions":
        captions.harvest(cfg, con, dry, args.from_file)
    elif args.command == "set-bio":
        instagram.run_set_bio(cfg, con, account_cfg(cfg, args.account), args.force, dry)
    elif args.command == "plan":
        planner.plan(cfg, con, dry)
    elif args.command == "publish":
        instagram.run_publish(cfg, con, dry)
    else:
        print(f"not implemented yet (phase {LATER[args.command]})")
        return 2
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load(args.config)
    con = db.connect(cfg["data"] / "clips.db", readonly=args.dry_run)
    try:
        return run_command(args, cfg, con)
    except Exception:
        # ponytail: Telegram alert on failure arrives with telegram.send in Task 4.3; log only until then
        log.exception("command %s failed", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
