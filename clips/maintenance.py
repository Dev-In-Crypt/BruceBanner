"""Housekeeping (spec module 17): old files, DB backup, disk alert. Logs rotate in journald (Task 6.1)."""
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clips import db, telegram

log = logging.getLogger(__name__)


def _rm(path, dry, why) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    log.info("%s %s (%s)", "would delete" if dry else "delete", p, why)
    if not dry:
        p.unlink()
    return True


def run_maintenance(cfg, con, dry, now=None) -> dict:
    now, m = now or datetime.now(timezone.utc), cfg["maintenance"]
    n = {"build": 0, "sources": 0, "debug": 0, "orphans": 0}
    # 1. built reels that will never be published again
    for r in con.execute("SELECT id, file_path FROM publications WHERE file_path IS NOT NULL "
                         "AND state IN ('published','unconfirmed','failed')").fetchall():
        if Path(r["file_path"]).resolve().is_relative_to((cfg["data"] / "build").resolve()):
            n["build"] += _rm(r["file_path"], dry, f"pub {r['id']} done")
            if not dry:
                with con:
                    con.execute("UPDATE publications SET file_path = NULL WHERE id = ?", (r["id"],))
    # 2. source videos: duplicates/failed now, others after keep_source_days unless still needed
    old = db.iso(now - timedelta(days=m["keep_source_days"]))
    for r in con.execute(
            "SELECT s.id, s.file_path, s.state FROM sources s WHERE s.file_path IS NOT NULL AND "
            "(s.state IN ('duplicate','failed') OR s.posted_at < ?) AND NOT EXISTS (SELECT 1 FROM publications p "
            "WHERE p.source_id = s.id AND p.state IN ('planned','built'))", (old,)).fetchall():
        n["sources"] += _rm(r["file_path"], dry, f"source {r['id']} {r['state']}")
        if not dry:
            with con:
                con.execute("UPDATE sources SET file_path = NULL WHERE id = ?", (r["id"],))
    # 3. debug files
    cutoff = time.time() - m["keep_debug_days"] * 86400
    for d in ("screenshots", "raw"):
        for f in (cfg["data"] / d).iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                n["debug"] += _rm(f, dry, "old debug file")
    # 4. orphans in build/ and tmp/ older than a day
    referenced = {str(Path(r[0]).resolve().with_suffix(s)) for r in con.execute(  # .jpg: the reel's cover
        "SELECT file_path FROM publications WHERE file_path IS NOT NULL") for s in (".mp4", ".jpg")}
    day_ago = time.time() - 86400
    for d in ("build", "tmp"):
        for f in (cfg["data"] / d).iterdir():
            if f.is_file() and str(f.resolve()) not in referenced and f.stat().st_mtime < day_ago:
                n["orphans"] += _rm(f, dry, "orphan")
    # 5. backup (online, safe with WAL), keep the newest N
    if not dry:
        dst = cfg["data"] / "backups" / f"clips-{now:%Y%m%d}.db"
        with sqlite3.connect(dst) as b:
            con.backup(b)
        b.close()
        for f in sorted((cfg["data"] / "backups").glob("clips-*.db"))[:-m["keep_backups"]]:
            f.unlink()
        log.info("backup: %s", dst)
    # 6. disk
    free_gb = shutil.disk_usage(cfg["data"]).free / 2**30
    if free_gb < m["min_free_disk_gb"]:
        telegram.send(f"⚠️ Мало места на диске: {free_gb:.1f} ГБ свободно (порог {m['min_free_disk_gb']} ГБ)",
                      dry=dry)
    log.info("maintenance: %s, free %.1f GB%s", n, free_gb, " (dry-run)" if dry else "")
    return n
