import os
import sqlite3
import time
from datetime import datetime, timezone

from clips import db, maintenance


def test_maintenance(cfg, con):
    d = cfg["data"]
    db.sync_accounts(con, {"accounts": [{"nick": "a"}]})
    done, waiting, orphan, shot = d / "build/pub_1.mp4", d / "build/pub_2.mp4", d / "tmp/x.mp4", d / "screenshots/a.png"
    for f in (done, waiting, orphan, shot):
        f.write_bytes(b"x")
    old = time.time() - 40 * 86400
    for f in (orphan, shot):
        os.utime(f, (old, old))
    con.execute("INSERT INTO publications (account, state, scheduled_at, created_at, file_path) "
                "VALUES ('a', 'published', 'x', 'x', ?), ('a', 'built', 'x', 'x', ?)", (str(done), str(waiting)))
    con.commit()
    assert maintenance.run_maintenance(cfg, con, True)["build"] == 1 and done.exists()   # dry-run touches nothing
    n = maintenance.run_maintenance(cfg, con, False, now=datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert n == {"build": 1, "sources": 0, "debug": 1, "orphans": 1}
    assert not done.exists() and waiting.exists()                        # a reel waiting to be posted is kept
    backup = d / "backups/clips-20261117.db"
    assert sqlite3.connect(backup).execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 2
