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
