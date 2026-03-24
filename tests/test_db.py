"""Tests for the database layer."""

import time

from nexus.store.db import NexusDB


def test_db_creates_tables(db):
    stats = db.get_stats()
    assert stats["files"] == 0
    assert stats["symbols"] == 0
    assert stats["edges"] == 0


def test_upsert_and_get_file(db):
    file_id = db.upsert_file(
        path="src/main.py",
        sha256="abc123",
        language="python",
        line_count=50,
        byte_size=1200,
        timestamp=time.time(),
    )
    assert file_id > 0

    row = db.get_file_by_path("src/main.py")
    assert row is not None
    assert row["sha256"] == "abc123"
    assert row["language"] == "python"
    assert row["line_count"] == 50


def test_upsert_updates_existing(db):
    db.upsert_file("a.py", "hash1", "python", 10, 100, time.time())
    db.upsert_file("a.py", "hash2", "python", 20, 200, time.time())

    row = db.get_file_by_path("a.py")
    assert row["sha256"] == "hash2"
    assert row["line_count"] == 20


def test_insert_symbol_and_find(db):
    fid = db.upsert_file("x.py", "h", "python", 5, 50, time.time())
    sid = db.insert_symbol(
        file_id=fid, name="foo", qualified="x.foo",
        kind="function", line_start=1, line_end=3,
        signature="def foo()", docstring="Does foo.",
    )
    assert sid > 0

    results = db.find_symbol_by_name("foo")
    assert len(results) == 1
    assert results[0]["name"] == "foo"
    assert results[0]["file_path"] == "x.py"


def test_insert_edge_and_neighbors(db):
    fid = db.upsert_file("e.py", "h", "python", 10, 100, time.time())
    s1 = db.insert_symbol(fid, "a", "e.a", "function", 1, 3)
    s2 = db.insert_symbol(fid, "b", "e.b", "function", 4, 6)

    db.insert_edge(s1, s2, "calls")
    neighbors = db.get_neighbors(s1)
    assert len(neighbors) == 1
    assert neighbors[0]["name"] == "b"


def test_duplicate_edge_ignored(db):
    fid = db.upsert_file("d.py", "h", "python", 10, 100, time.time())
    s1 = db.insert_symbol(fid, "x", "d.x", "function", 1, 2)
    s2 = db.insert_symbol(fid, "y", "d.y", "function", 3, 4)

    db.insert_edge(s1, s2, "calls")
    db.insert_edge(s1, s2, "calls")  # should not raise

    stats = db.get_stats()
    assert stats["edges"] == 1


def test_clear_file_cascades(db):
    fid = db.upsert_file("c.py", "h", "python", 5, 50, time.time())
    s1 = db.insert_symbol(fid, "m", "c.m", "function", 1, 2)

    db.clear_file(fid)
    assert db.get_symbols_for_file(fid) == []


def test_file_tags(db):
    fid = db.upsert_file("gen.py", "h", "python", 5, 50, time.time())
    db.tag_file(fid, "generated")
    db.tag_file(fid, "generated")  # duplicate should be fine

    tags = db.get_file_tags(fid)
    assert tags == ["generated"]


def test_integrity_check(tmp_path):
    """DB should pass integrity check on creation."""
    db_path = tmp_path / ".nexus" / "test.db"
    db = NexusDB(db_path)  # should not raise
    db.close()


def test_schema_version(db):
    """Schema version should be recorded."""
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(version) as v FROM schema_version").fetchone()
        assert row["v"] is not None
        assert row["v"] >= 2
