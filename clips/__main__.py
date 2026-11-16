"""CLI: python -m clips <command> [--dry-run]. One command per pipeline step."""
import argparse
import json
import logging
import sys

from clips import config, db

log = logging.getLogger("clips")

# command -> phase in which it gets implemented (docs/IMPLEMENTATION_PLAN.md, section 2.6)
LATER = {
    "sync-accounts": 2, "account-status": 2, "health": 2, "publish-file": 2,
    "fetch-trends": 3, "select": 3, "download": 3, "trends": 3, "harvest-captions": 3, "set-bio": 3,
    "plan": 3, "publish": 3,
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
    for name, phase in LATER.items():
        sub.add_parser(name, parents=[common], help=f"(phase {phase})")
    return p


def account_cfg(cfg, nick) -> dict:
    for a in cfg["accounts"]:
        if a["nick"] == nick:
            return a
    raise SystemExit(f"account {nick!r} not in config")


def run_command(args, cfg, con) -> int:
    from clips import video
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
