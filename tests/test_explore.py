"""Tests for multi-hop exploration (RLM-inspired)."""

import pytest

from nexus.rank.bm25 import NexusBM25
from nexus.rank.explore import explore, format_exploration
from nexus.store.db import NexusDB


@pytest.fixture
def db(tmp_path):
    return NexusDB(tmp_path / "nexus.db")


def _add_file(db, path, language="python"):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO files (path, sha256, language, line_count, "
            "byte_size, last_parsed) VALUES (?, ?, ?, ?, ?, ?)",
            (path, f"h_{path}", language, 1, 1, 0.0),
        )
        return cur.lastrowid


def _add_symbol(db, file_id, name, qualified=None, kind="function",
                body="", signature=""):
    qualified = qualified or name
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO symbols (file_id, name, qualified, kind, line_start, "
            "line_end, signature, docstring, body_text, visibility, decorators) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, name, qualified, kind, 1, 10, signature, "", body,
             "public", ""),
        )
        return cur.lastrowid


def _add_edge(db, src_id, tgt_id, kind="calls"):
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, kind, weight) "
            "VALUES (?, ?, ?, 1.0)",
            (src_id, tgt_id, kind),
        )


@pytest.fixture
def graph_db(db):
    """Build a small connected graph:
         auth.py:login  --calls-->  auth.py:validate
         auth.py:login  --calls-->  users.py:lookup
         users.py:lookup --calls--> db.py:query
         README.md (isolated)
    """
    auth_fid = _add_file(db, "auth.py")
    users_fid = _add_file(db, "users.py")
    db_fid = _add_file(db, "db.py")
    readme_fid = _add_file(db, "README.md", language="markdown")

    login_sid = _add_symbol(db, auth_fid, "login", "auth.login",
                            body="authenticate user login session password")
    validate_sid = _add_symbol(db, auth_fid, "validate", "auth.validate",
                               body="validate credentials")
    lookup_sid = _add_symbol(db, users_fid, "lookup", "users.lookup",
                             body="lookup user by id")
    query_sid = _add_symbol(db, db_fid, "query", "db.query",
                            body="execute sql query")
    _add_symbol(db, readme_fid, "readme", "readme", body="project readme docs")

    _add_edge(db, login_sid, validate_sid, kind="calls")
    _add_edge(db, login_sid, lookup_sid, kind="calls")
    _add_edge(db, lookup_sid, query_sid, kind="calls")

    bm25 = NexusBM25()
    bm25.build(db)
    return db, bm25


# ── Core behavior ───────────────────────────────────────────────────────────

class TestExplore:
    def test_returns_empty_for_no_match(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "xyzzy_nonsense_token", seeds=5, hops=2)
        assert result["seeds"] == []
        assert result["expansion"] == []
        assert result["total"] == 0

    def test_hop_0_returns_only_seeds(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login authenticate", seeds=2, hops=0)
        assert len(result["seeds"]) >= 1
        # At hop 0, expansion equals seeds
        assert all(e["hop"] == 0 for e in result["expansion"])
        assert result["edges_followed"] == 0

    def test_hop_1_finds_direct_neighbors(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login", seeds=1, hops=1)
        paths = {e["file_path"] for e in result["expansion"]}
        assert "auth.py" in paths
        # auth.py's internal edges reach users.py via login->lookup
        assert "users.py" in paths

    def test_hop_2_finds_two_hop_neighbors(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login", seeds=1, hops=2)
        paths = {e["file_path"] for e in result["expansion"]}
        # 2 hops: login -> lookup (hop 1) -> query (hop 2)
        assert "db.py" in paths

    def test_isolated_file_not_pulled_in(self, graph_db):
        db, bm25 = graph_db
        # README has no edges, so even at hop 3 we shouldn't reach it
        # unless BM25 matches it directly.
        result = explore(db, bm25, "login", seeds=1, hops=3)
        paths = {e["file_path"] for e in result["expansion"]}
        assert "README.md" not in paths

    def test_max_expanded_caps_results(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login", seeds=3, hops=3, max_expanded=2)
        assert result["total"] <= 2

    def test_expansion_has_provenance(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login", seeds=1, hops=1)
        # Non-seed entries must have "via" and edge-based reason
        non_seeds = [e for e in result["expansion"] if e["hop"] > 0]
        for e in non_seeds:
            assert e["via"] is not None
            assert e["reason"].startswith("edge:")

    def test_seeds_have_reason_bm25(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login", seeds=1, hops=2)
        seeds = [e for e in result["expansion"] if e["hop"] == 0]
        assert len(seeds) >= 1
        assert all(e["reason"] == "bm25_seed" for e in seeds)

    def test_negative_hops_clamped(self, graph_db):
        db, bm25 = graph_db
        result = explore(db, bm25, "login", seeds=1, hops=-5)
        # Behaves like hops=0
        assert result["edges_followed"] == 0


# ── Formatter ───────────────────────────────────────────────────────────────

def test_format_exploration_produces_readable_output(graph_db):
    db, bm25 = graph_db
    result = explore(db, bm25, "login", seeds=1, hops=2)
    text = format_exploration(result)
    assert "Multi-hop exploration" in text
    assert "login" in text
    assert "hop 0" in text or "-- hop 0 --" in text


def test_format_empty_result():
    text = format_exploration({
        "query": "nothing",
        "seeds": [],
        "expansion": [],
        "total": 0,
        "edges_followed": 0,
    })
    assert "No BM25 seeds" in text
    assert "nothing" in text
