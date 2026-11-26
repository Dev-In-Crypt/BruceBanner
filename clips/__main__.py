"""CLI: python -m clips <command> [--dry-run]. One command per pipeline step."""
import argparse
import json
import logging
import sys

from clips import config, db

log = logging.getLogger("clips")

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
    b.add_argument("--midroll-index", type=int, default=0, help="which midroll variant (they take turns)")
    ib = sub.add_parser("import-banners", parents=[common],
                        help="re-encode a Playerok .mov banner set to light WebM, delete the sources")
    ib.add_argument("--name", required=True)
    ib.add_argument("--top", required=True)
    ib.add_argument("--midroll", nargs="*", default=[])
    ib.add_argument("--start", help="first day, YYYY-MM-DD")
    ib.add_argument("--end", help="last day, YYYY-MM-DD")
    ib.add_argument("--keep-source", action="store_true")
    b.add_argument("--text", help="text plate (needs compose.text_overlay = true)")
    v = sub.add_parser("validate", parents=[common], help="validate a built reel")
    v.add_argument("path")
    sub.add_parser("sync-accounts", parents=[common], help="add config accounts to the DB")
    s = sub.add_parser("account-status", parents=[common], help="set an account status by hand")
    s.add_argument("nick")
    s.add_argument("status", choices=["active", "paused", "check", "banned"])
    s.add_argument("--reason", default="manual")
    bd = sub.add_parser("account-bound", parents=[common], help="mark an account as bound in the Playerok bot")
    bd.add_argument("nick")
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
    # clipping.net: TikTok + YouTube Shorts with Kick streamer clips (docs/CLIPPING_PLAN.md)
    sub.add_parser("kick", parents=[common], help="clipping: Kick clips + cut campaign videos (Claude) -> download + checks")
    ca = sub.add_parser("clip-approve", parents=[common], help="clipping: approve the AI hooks of cut clips")
    ca.add_argument("clip_ids", nargs="+")
    ac = sub.add_parser("add-clip", parents=[common], help="clipping: add a clip by hand (file or link)")
    ac.add_argument("source")
    ac.add_argument("--campaign", required=True)
    ac.add_argument("--streamer", required=True, help="Kick channel slug for the KICK.COM/<STREAMER> bar")
    ac.add_argument("--hook", default="")
    ch = sub.add_parser("clip-hook", parents=[common], help="clipping: set the hook text of a clip")
    ch.add_argument("clip_id")
    ch.add_argument("hook")
    cp = sub.add_parser("clip-preview", parents=[common], help="clipping: build one clip to a file to look at")
    cp.add_argument("clip_id")
    cp.add_argument("--out", required=True)
    sub.add_parser("clip-plan", parents=[common], help="clipping: plan today's posts and build them")
    sub.add_parser("clip-publish", parents=[common], help="clipping: post due videos, send links to submit")
    sub.add_parser("clip-stats", parents=[common], help="clipping: views of our posts, alert on deleted ones")
    sub.add_parser("publish", parents=[common], help="publish due built publications")
    st = sub.add_parser("stats", parents=[common], help="collect views, decide what to submit, notify")
    st.add_argument("--from-file", help="use a saved Apify JSON dump instead of calling Apify")
    sub.add_parser("submit-check", parents=[common], help="only the submit logic + notifications")
    sub.add_parser("bot", parents=[common], help="long-running Telegram bot for the operator")
    sub.add_parser("summary", parents=[common], help="daily summary to Telegram")
    sub.add_parser("moderate-comments", parents=[common], help="delete spam/bait/links/bots under our reels")
    ns = sub.add_parser("notion-setup", parents=[common], help="create the two Notion databases")
    ns.add_argument("--parent-page", required=True, help="Notion page id to create the databases in")
    sub.add_parser("notion-sync", parents=[common], help="push changed rows to Notion")
    sub.add_parser("report", parents=[common], help="weekly report to Telegram")
    sub.add_parser("maintenance", parents=[common], help="old files, DB backup, disk alert")
    return p


def account_cfg(cfg, nick) -> dict:
    for a in cfg["accounts"]:
        if a["nick"] == nick:
            return a
    raise SystemExit(f"account {nick!r} not in config")


def run_clipping(args, cfg, con) -> None:
    from clips import clipping, kick, shorts, video, vod
    dry = args.dry_run
    if args.command == "kick":
        kick.fetch(cfg, con, dry)
        vod.process(cfg, con, dry)
        kick.download(cfg, con, dry)
    elif args.command == "clip-approve":
        print(f"approved: {vod.approve(con, args.clip_ids)}")
    elif args.command == "add-clip":
        print(kick.add(cfg, con, args.source, args.campaign, args.streamer, args.hook))
    elif args.command == "clip-hook":
        clipping.set_hook(con, args.clip_id, args.hook)
    elif args.command == "clip-preview":
        c = con.execute("SELECT * FROM kick_clips WHERE id = ? AND state = 'ready'", (args.clip_id,)).fetchone()
        if not c:
            raise SystemExit(f"no ready clip {args.clip_id!r} (run kick first)")
        bar = kick.campaign(cfg, c["campaign"]).get("bar", "kick")
        print(json.dumps(video.build_clip(c["file_path"], c["hook"] or c["title"] or "", c["streamer"], cfg, args.out,
                                          seed=c["id"], bar=bar)))
    elif args.command == "clip-plan":
        clipping.plan(cfg, con, dry)
    elif args.command == "clip-publish":
        shorts.run_publish(cfg, con, dry)
    elif args.command == "clip-stats":
        clipping.stats(cfg, con, dry)


def run_command(args, cfg, con) -> int:
    from clips import banners, captions, comments, instagram, maintenance, notion, planner, reports, submit, telegram, trends, video
    dry = args.dry_run
    if args.command in ("sync-accounts", "account-status", "account-bound", "health", "publish-file") and not dry:
        db.sync_accounts(con, cfg)
    if args.command == "init-db":
        print(cfg["data"] / "clips.db")
    elif args.command == "build":
        if args.text and not cfg["compose"]["text_overlay"]:
            raise SystemExit("--text needs compose.text_overlay = true")
        meta = video.build(args.source, account_cfg(cfg, args.account), cfg, args.out,
                           text=args.text, use_midroll=not args.no_midroll, midroll_index=args.midroll_index)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    elif args.command == "import-banners":
        if dry:
            raise SystemExit("import-banners has no dry-run: it only writes new files under assets/banners")
        print(banners.import_set(cfg, args.name, args.top, args.midroll, args.start, args.end, args.keep_source))
        print("# paste this block into config.toml under [banners]")
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
    elif args.command == "account-bound":
        account_cfg(cfg, args.nick)
        if not dry:
            with con:
                con.execute("UPDATE accounts SET bound_at = ?, updated_at = ? WHERE nick = ?",
                            (db.now_iso(), db.now_iso(), args.nick))
        print(f"@{args.nick}: bound")
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
    elif args.command in ("kick", "clip-approve", "add-clip", "clip-hook", "clip-preview", "clip-plan", "clip-publish", "clip-stats"):
        run_clipping(args, cfg, con)
    elif args.command == "publish":
        instagram.run_publish(cfg, con, dry)
    elif args.command == "stats":
        submit.run_stats(cfg, con, dry, args.from_file)
    elif args.command == "submit-check":
        submit.run_submit_check(cfg, con, dry)
    elif args.command == "bot":
        if dry:
            raise SystemExit("bot has no --dry-run mode: it must record the operator's answers")
        telegram.run_bot(cfg, con)
    elif args.command == "moderate-comments":
        comments.run_moderation(cfg, con, dry)
    elif args.command == "summary":
        reports.run_summary(cfg, con, dry)
    elif args.command == "notion-setup":
        notion.setup(cfg, args.parent_page, dry)
    elif args.command == "notion-sync":
        notion.sync(cfg, con, dry)
    elif args.command == "report":
        reports.run_report(cfg, con, dry)
    elif args.command == "maintenance":
        maintenance.run_maintenance(cfg, con, dry)
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load(args.config, check_files=args.command != "import-banners")  # it creates the banner files
    con = db.connect(cfg["data"] / "clips.db", readonly=args.dry_run)
    try:
        return run_command(args, cfg, con)
    except Exception as e:
        log.exception("command %s failed", args.command)
        from clips import telegram
        telegram.send(telegram.msg_error(args.command, e), dry=args.dry_run)  # never raises
        return 1


if __name__ == "__main__":
    sys.exit(main())
