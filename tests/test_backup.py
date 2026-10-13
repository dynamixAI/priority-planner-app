"""Backups: the nightly copy must be able to rebuild the database exactly."""
import io
import sqlite3

from backup.dump import sql_value, write_dump
from conftest import add_task


def test_awkward_values_are_written_safely():
    assert sql_value(None) == "NULL"
    assert sql_value(7) == "7"
    assert sql_value(2.5) == "2.5"
    assert sql_value("Sam's task") == "'Sam''s task'"
    assert sql_value(b"\x00\xff") == "X'00ff'"


def test_a_backup_rebuilds_the_database_exactly(user, db):
    add_task(user, title="Sam's 'quoted' task", time="07:00")
    add_task(user, title="Emoji \u2705 and a\nnew line", time="08:00")
    out = io.StringIO()
    counts = write_dump(db, out)
    assert counts["users"] == 1 and counts["tasks"] == 2

    copy = sqlite3.connect(":memory:")
    copy.executescript(out.getvalue())
    for table in list(counts) + ["sqlite_sequence"]:
        query = 'SELECT * FROM "%s" ORDER BY rowid' % table
        assert copy.execute(query).fetchall() == db.execute(query).fetchall()
