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
