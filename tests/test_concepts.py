"""Tests for the concept graph (long-lived knowledge base)."""

import pytest

from nexus.session.concepts import (
    VALID_KINDS,
    VALID_RELATIONS,
    attach_concept_to_file,
    attach_concept_to_symbol,
    delete_concept,
    format_concept_graph,
    get_concept,
    get_concept_neighbors,
    link_concepts,
    list_concepts,
    upsert_concept,
)
from nexus.store.db import NexusDB


@pytest.fixture
def db(tmp_path):
    return NexusDB(tmp_path / "nexus.db")


# ── upsert ──────────────────────────────────────────────────────────────────

class TestUpsertConcept:
    def test_create_new(self, db):
        cid = upsert_concept(db, "Hexagonal Architecture",
                             "Isolates core logic from I/O concerns.",
                             kind="architecture")
        assert cid > 0
        got = get_concept(db, "Hexagonal Architecture")
        assert got["name"] == "Hexagonal Architecture"
        assert got["kind"] == "architecture"
        assert got["confidence"] == 0.5  # default

    def test_case_insensitive_lookup(self, db):
        upsert_concept(db, "BM25", "Text-relevance scoring.")
        got = get_concept(db, "bm25")
        assert got is not None
        assert got["name"] == "BM25"

    def test_update_existing_preserves_id(self, db):
        cid1 = upsert_concept(db, "RRF", "Rank fusion.", confidence=0.5)
        cid2 = upsert_concept(db, "rrf",   # case-insensitive match
                              "Reciprocal rank fusion with k=60.", confidence=0.9)
        assert cid1 == cid2
        got = get_concept(db, "RRF")
        assert "k=60" in got["summary"]
        assert got["confidence"] == 0.9

    def test_invalid_kind_raises(self, db):
        with pytest.raises(ValueError, match="Invalid kind"):
            upsert_concept(db, "X", "y", kind="not_a_kind")

    def test_invalid_confidence_raises(self, db):
        with pytest.raises(ValueError, match="confidence"):
            upsert_concept(db, "X", "y", confidence=1.5)

    def test_empty_name_raises(self, db):
        with pytest.raises(ValueError, match="empty"):
            upsert_concept(db, "   ", "y")


# ── link ────────────────────────────────────────────────────────────────────

class TestLinkConcepts:
    def test_creates_edge(self, db):
        upsert_concept(db, "BM25", "text ranking")
        upsert_concept(db, "RRF", "rank fusion")
        eid = link_concepts(db, "BM25", "RRF", relation="used_by")
        assert eid > 0

    def test_auto_creates_missing_concepts(self, db):
        eid = link_concepts(db, "Foo", "Bar", relation="related_to")
        assert eid > 0
        assert get_concept(db, "Foo") is not None
        assert get_concept(db, "Bar") is not None

    def test_invalid_relation_raises(self, db):
        with pytest.raises(ValueError, match="Invalid relation"):
            link_concepts(db, "A", "B", relation="bogus")

    def test_duplicate_edge_updates_weight(self, db):
        eid1 = link_concepts(db, "A", "B", relation="related_to", weight=0.5)
        eid2 = link_concepts(db, "A", "B", relation="related_to", weight=0.9)
        assert eid1 == eid2


# ── neighbors / traversal ───────────────────────────────────────────────────

class TestGetConceptNeighbors:
    def test_depth_0_returns_only_center(self, db):
        upsert_concept(db, "A", "a")
        upsert_concept(db, "B", "b")
        link_concepts(db, "A", "B")
        g = get_concept_neighbors(db, "A", depth=0)
        assert g["center"]["name"] == "A"
        assert len(g["nodes"]) == 1  # just A
        assert g["edges"] == []

    def test_depth_1_includes_direct_neighbors(self, db):
        upsert_concept(db, "A", "a")
        upsert_concept(db, "B", "b")
        upsert_concept(db, "C", "c")
        link_concepts(db, "A", "B", relation="depends_on")
        link_concepts(db, "A", "C", relation="refines")
        g = get_concept_neighbors(db, "A", depth=1)
        names = {n["name"] for n in g["nodes"]}
        assert names == {"A", "B", "C"}
        assert len(g["edges"]) == 2

    def test_depth_2_traverses_two_hops(self, db):
        # A -> B -> C
        link_concepts(db, "A", "B")
        link_concepts(db, "B", "C")
        g = get_concept_neighbors(db, "A", depth=2)
        names = {n["name"] for n in g["nodes"]}
        assert names == {"A", "B", "C"}

    def test_respects_max_nodes_cap(self, db):
        for i in range(20):
            link_concepts(db, "Hub", f"N{i}")
        g = get_concept_neighbors(db, "Hub", depth=1, max_nodes=5)
        assert len(g["nodes"]) <= 5

    def test_missing_concept_returns_empty(self, db):
        g = get_concept_neighbors(db, "does-not-exist")
        assert g["center"] is None
        assert g["nodes"] == []


# ── list ────────────────────────────────────────────────────────────────────

class TestListConcepts:
    def test_returns_by_recency(self, db):
        upsert_concept(db, "Old", "old")
        import time; time.sleep(0.01)
        upsert_concept(db, "New", "new")
        concepts = list_concepts(db)
        assert concepts[0]["name"] == "New"

    def test_filter_by_kind(self, db):
        upsert_concept(db, "Pat", "a pattern", kind="pattern")
        upsert_concept(db, "Arc", "an arch", kind="architecture")
        patterns = list_concepts(db, kind="pattern")
        assert len(patterns) == 1
        assert patterns[0]["name"] == "Pat"


# ── delete ──────────────────────────────────────────────────────────────────

class TestDeleteConcept:
    def test_delete_removes_concept(self, db):
        upsert_concept(db, "Temp", "t")
        assert delete_concept(db, "Temp") is True
        assert get_concept(db, "Temp") is None

    def test_delete_cascades_edges(self, db):
        upsert_concept(db, "A", "a")
        upsert_concept(db, "B", "b")
        link_concepts(db, "A", "B")
        delete_concept(db, "A")
        # B remains but has no neighbors via A
        g = get_concept_neighbors(db, "B", depth=1)
        assert g["edges"] == []

    def test_delete_missing_returns_false(self, db):
        assert delete_concept(db, "nonexistent") is False


# ── file/symbol attachment ──────────────────────────────────────────────────

class TestAttach:
    def _add_file(self, db, path="src/foo.py"):
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO files (path, sha256, language, line_count, "
                "byte_size, last_parsed) VALUES (?, ?, ?, ?, ?, ?)",
                (path, "hash", "python", 0, 0, 0.0),
            )
            return cur.lastrowid

    def test_attach_file_found(self, db):
        self._add_file(db, "src/foo.py")
        upsert_concept(db, "X", "x")
        assert attach_concept_to_file(db, "X", "src/foo.py") is True
        g = get_concept_neighbors(db, "X")
        assert any(f["path"] == "src/foo.py" for f in g["files"])

    def test_attach_file_missing_returns_false(self, db):
        upsert_concept(db, "X", "x")
        assert attach_concept_to_file(db, "X", "nope.py") is False

    def test_attach_file_missing_concept_returns_false(self, db):
        self._add_file(db, "src/foo.py")
        assert attach_concept_to_file(db, "nope", "src/foo.py") is False


# ── formatting ──────────────────────────────────────────────────────────────

def test_format_concept_graph_basic(db):
    upsert_concept(db, "A", "Summary of A", kind="pattern")
    upsert_concept(db, "B", "Summary of B")
    link_concepts(db, "A", "B", relation="depends_on")
    g = get_concept_neighbors(db, "A")
    out = format_concept_graph(g)
    assert "A" in out
    assert "pattern" in out
    assert "depends_on" in out
    assert "B" in out


def test_format_concept_not_found(db):
    g = get_concept_neighbors(db, "missing")
    out = format_concept_graph(g)
    assert "not found" in out.lower()


# ── constants sanity ────────────────────────────────────────────────────────

def test_valid_kinds_nonempty():
    assert "pattern" in VALID_KINDS
    assert "architecture" in VALID_KINDS
    assert len(VALID_KINDS) >= 5


def test_valid_relations_nonempty():
    assert "depends_on" in VALID_RELATIONS
    assert "contradicts" in VALID_RELATIONS
    assert len(VALID_RELATIONS) >= 5
