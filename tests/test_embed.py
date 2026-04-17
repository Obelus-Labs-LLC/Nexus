"""Tests for the optional embeddings fallback module.

These tests focus on graceful-degradation behavior when fastembed is not
installed. They do NOT require fastembed to run. If fastembed is installed,
we also exercise the happy path with a tiny corpus.
"""

import pytest

from nexus.rank.embed import (
    DEFAULT_DIM,
    DEFAULT_MODEL,
    EmbeddingIndex,
    ensure_embed_schema,
    is_available,
)
from nexus.rank.fusion import fuse_rankings
from nexus.store.db import NexusDB


# ── Graceful degradation (runs with or without fastembed) ──────────────────

def test_is_available_returns_bool():
    assert isinstance(is_available(), bool)


def test_embedding_index_reports_availability():
    idx = EmbeddingIndex()
    assert isinstance(idx.is_available, bool)
    assert idx.is_available == is_available()


def test_model_defaults():
    idx = EmbeddingIndex()
    assert idx.model_name == DEFAULT_MODEL
    assert idx.dim == DEFAULT_DIM


def test_query_returns_empty_before_build(tmp_path):
    idx = EmbeddingIndex()
    assert idx.query("anything", top_k=10) == []


def test_build_noop_when_unavailable(tmp_path, monkeypatch):
    """If fastembed is missing, build() returns 0 and logs a warning."""
    monkeypatch.setattr("nexus.rank.embed._FASTEMBED_AVAILABLE", False)
    db = NexusDB(tmp_path / "n.db")
    idx = EmbeddingIndex()
    assert idx.build(db) == 0


def test_load_noop_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.rank.embed._FASTEMBED_AVAILABLE", False)
    db = NexusDB(tmp_path / "n.db")
    idx = EmbeddingIndex()
    assert idx.load(db) is False


def test_ensure_embed_schema_creates_table(tmp_path):
    db = NexusDB(tmp_path / "n.db")
    with db.connect() as conn:
        ensure_embed_schema(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='symbol_embeddings'"
        ).fetchone()
        assert row is not None


def test_schema_ensure_is_idempotent(tmp_path):
    db = NexusDB(tmp_path / "n.db")
    with db.connect() as conn:
        ensure_embed_schema(conn)
        ensure_embed_schema(conn)  # second call must not raise


# ── Fusion integration: embed signal is optional ───────────────────────────

def test_fuse_without_embed_signal():
    bm25 = [{"file_id": 1, "file_path": "a.py", "rank": 0, "score": 1.0}]
    pr = [{"file_id": 1, "rank": 0, "score": 0.5}]
    out = fuse_rankings(bm25, pr)
    assert len(out) == 1
    assert out[0]["file_id"] == 1


def test_fuse_with_embed_signal():
    bm25 = [{"file_id": 1, "file_path": "a.py", "rank": 0, "score": 1.0}]
    pr = [{"file_id": 2, "rank": 0, "score": 0.5}]
    embed = [{"file_id": 3, "rank": 0, "score": 0.8}]
    out = fuse_rankings(bm25, pr, embed_results=embed)
    # All 3 files should appear; embed-only file gets lower score
    fids = {r["file_id"] for r in out}
    assert fids == {1, 2, 3}

    file_3 = next(r for r in out if r["file_id"] == 3)
    assert file_3.get("embed_rank") == 0
    assert file_3.get("embed_score") == 0.8


def test_embed_weighted_below_bm25_pr():
    """An embed-only hit should score lower than a bm25-only hit at same rank."""
    bm25_only = fuse_rankings(
        [{"file_id": 1, "file_path": "a", "rank": 0, "score": 1.0}],
        [],
    )
    embed_only = fuse_rankings(
        [], [],
        embed_results=[{"file_id": 1, "rank": 0, "score": 1.0}],
    )
    assert bm25_only[0]["rrf_score"] > embed_only[0]["rrf_score"]


# ── Happy path (only if fastembed installed) ────────────────────────────────

@pytest.mark.skipif(not is_available(), reason="fastembed not installed")
def test_build_and_query_real_embeddings(tmp_path):
    db = NexusDB(tmp_path / "n.db")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO files (path, sha256, language, line_count, "
            "byte_size, last_parsed) VALUES (?, ?, ?, ?, ?, ?)",
            ("auth.py", "h1", "python", 10, 100, 0.0),
        )
        conn.execute(
            "INSERT INTO symbols (file_id, name, qualified, kind, line_start, "
            "line_end, signature, docstring, body_text, visibility, decorators) "
            "VALUES (1, 'login', 'auth.login', 'function', 1, 10, 'def login', "
            "'authenticate a user and start a session', '', 'public', '')"
        )

    idx = EmbeddingIndex()
    n = idx.build(db)
    assert n == 1

    # Semantic query should match even with paraphrasing
    results = idx.query("user sign in", top_k=5)
    assert len(results) == 1
    assert results[0]["file_id"] == 1
