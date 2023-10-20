import os

import pytest

import chatmaild.dictproxy
from chatmaild.dictproxy import get_user_data, lookup_passdb
from chatmaild.database import DBError


def test_basic(db, tmpdir, monkeypatch):
    monkeypatch.setattr(chatmaild.dictproxy, "NOCREATE_FILE", tmpdir.join("nocreate").strpath)
    lookup_passdb(db, "link2xt@c1.testrun.org", "asdf")
    data = get_user_data(db, "link2xt@c1.testrun.org")
    assert data


def test_dont_overwrite_password_on_wrong_login(db):
    """Test that logging in with a different password doesn't create a new user"""
    res = lookup_passdb(db, "newuser1@something.org", "kajdlkajsldk12l3kj1983")
    assert res["password"]
    res2 = lookup_passdb(db, "newuser1@something.org", "kajdlqweqwe")
    # this function always returns a password hash, which is actually compared by dovecot.
    assert res["password"] == res2["password"]


def test_nocreate_file(db, tmpdir, monkeypatch):
    nocreate = tmpdir.join("nocreate")
    monkeypatch.setattr(chatmaild.dictproxy, "NOCREATE_FILE", str(nocreate))
    nocreate.write("")
    lookup_passdb(db, "newuser1@something.org", "kajdlqweqwe")
    assert not get_user_data(db, "newuser1@something.org")


def test_db_version(db):
    assert db.get_schema_version() == 1


def test_too_high_db_version(db):
    with db.write_transaction() as conn:
        conn.execute("PRAGMA user_version=%s;" % (999,))
    with pytest.raises(DBError):
        db.ensure_tables()
