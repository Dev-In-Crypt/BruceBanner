import sqlite3

import pytest

from clips import db


def test_dry_run_never_creates_or_writes_db(tmp_path):
    path = tmp_path / "clips.db"
    con = db.connect(path, readonly=True)
    assert not path.exists()                              # fresh install: nothing created
    assert con.execute("select count(*) from accounts").fetchone()[0] == 0
    db.connect(path).execute("insert into accounts(nick, updated_at) values ('a', 'x')").connection.commit()
    ro = db.connect(path, readonly=True)
    assert ro.execute("select nick from accounts").fetchone()[0] == "a"
    with pytest.raises(sqlite3.OperationalError):          # any write in dry-run fails loudly
        ro.execute("insert into accounts(nick, updated_at) values ('b', 'x')")


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)                       # a database created before addendum 2
    old.close()
    con = db.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
    assert {"donor_group", "shares"} <= cols
    db.connect(path)                                   # second connect: nothing to add, no error


def test_dry_run_on_old_database_sees_new_columns_without_writing(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    old.close()
    before = path.read_bytes()
    ro = db.connect(path, readonly=True)
    ro.execute("SELECT donor_group, shares FROM sources").fetchall()      # no 'no such column'
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO accounts(nick, updated_at) VALUES ('x', 'y')")
    assert path.read_bytes() == before                                   # the file was not migrated


def test_clip_posts_gets_platform_x_on_an_old_db(tmp_path):
    import sqlite3
    from clips import db
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA.replace("('tiktok','youtube','x')", "('tiktok','youtube')"))
    old.execute("INSERT INTO kick_clips (id, campaign, streamer, video_url, fetched_at) VALUES ('k','c','s','u','t')")
    old.execute("INSERT INTO clip_posts (clip_id, account, platform, campaign, scheduled_at, created_at) "
                "VALUES ('k', 'a', 'tiktok', 'c', 't', 't')")
    old.commit()
    old.close()
    con = db.connect(path)                                   # migrates: rebuilds clip_posts with the wider CHECK
    con.execute("INSERT INTO clip_posts (clip_id, account, platform, campaign, scheduled_at, created_at) "
                "VALUES ('k', 'b', 'x', 'c', 't', 't')")
    assert [r[0] for r in con.execute("SELECT platform FROM clip_posts ORDER BY id")] == ["tiktok", "x"]
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'clip_posts_account'").fetchone()
