"""Tests for the `locked` memory kind (Claude 4.7 mitigation pack).

Locked decisions are invariants:
- never expire
- always surface at the top of the session-start injection
- allowed a larger word cap (60) since they may encode multi-clause rules
"""

from __future__ import annotations

import time

import pytest

from nexus.session.memory import (
    DEFAULT_TTL,
    LOCKED,
    LOCKED_TTL,
    MAX_WORDS,
    MAX_WORDS_LOCKED,
    format_decisions,
    get_active_decisions,
    remember,
)


class TestLockedType:
    def test_locked_is_valid_type(self, db):
        """`locked` must be accepted as a decision type."""
        did = remember(db, "Never call nexus_scan in hot paths", decision_type=LOCKED)
        assert did is not None

    def test_locked_ignores_ttl_argument(self, db):
        """A caller passing a short TTL should still get LOCKED_TTL."""
        did = remember(db, "Invariant rule", decision_type=LOCKED, ttl=60)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT created_at, expires_at FROM decisions WHERE id = ?", (did,)
            ).fetchone()
        ttl_actual = row["expires_at"] - row["created_at"]
        # Should be ~LOCKED_TTL (100 years), not 60 seconds.
        assert ttl_actual > DEFAULT_TTL * 100, f"locked ttl was only {ttl_actual}s"
        assert ttl_actual == pytest.approx(LOCKED_TTL, rel=0.01)

    def test_non_locked_still_gets_short_ttl(self, db):
        """Regression guard: default TTL unchanged for standard types."""
        did = remember(db, "Standard decision", decision_type="decision")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT created_at, expires_at FROM decisions WHERE id = ?", (did,)
            ).fetchone()
        ttl_actual = row["expires_at"] - row["created_at"]
        assert ttl_actual == pytest.approx(DEFAULT_TTL, rel=0.01)


class TestLockedWordCap:
    def test_locked_accepts_longer_content(self, db):
        """Locked entries allow up to 60 words (vs 20 for standard)."""
        words = ["invariant"] * (MAX_WORDS + 5)  # 25 words, over standard cap
        content = " ".join(words)
        did = remember(db, content, decision_type=LOCKED)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM decisions WHERE id = ?", (did,)
            ).fetchone()
        # Should not have been truncated at MAX_WORDS (20) — full 25 words kept.
        assert len(row["content"].split()) == MAX_WORDS + 5

    def test_locked_truncates_at_its_own_cap(self, db):
        """Beyond MAX_WORDS_LOCKED, truncation kicks in."""
        over = MAX_WORDS_LOCKED + 10
        content = " ".join(["x"] * over)
        did = remember(db, content, decision_type=LOCKED)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM decisions WHERE id = ?", (did,)
            ).fetchone()
        assert len(row["content"].split()) == MAX_WORDS_LOCKED

    def test_standard_type_still_truncates_at_20(self, db):
        """Regression guard: standard cap unchanged."""
        content = " ".join(["x"] * (MAX_WORDS + 5))
        did = remember(db, content, decision_type="decision")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM decisions WHERE id = ?", (did,)
            ).fetchone()
        assert len(row["content"].split()) == MAX_WORDS


class TestLockedSurfacing:
    def test_format_puts_locked_first(self, db):
        """`format_decisions` must render locked entries under their own header first."""
        remember(db, "Standard decision here", decision_type="decision")
        remember(db, "Never edit tests to make them pass", decision_type=LOCKED)
        remember(db, "Task one", decision_type="task")

        active = get_active_decisions(db)
        out = format_decisions(active)

        # Locked header appears before the standard decisions header.
        idx_locked = out.index("## Locked invariants")
        idx_other = out.index("## Cross-session decisions:")
        assert idx_locked < idx_other, "locked header must come first"

        # Locked entry uses the [LOCKED] tag.
        assert "[LOCKED]" in out
        assert "Never edit tests to make them pass" in out

    def test_locked_always_returned_even_above_limit(self, db):
        """Fill past MAX_INJECT with regular decisions, then add locked — it must still come back."""
        for i in range(20):
            # Stagger timestamps so ordering is deterministic; older first.
            remember(db, f"decision number {i}", decision_type="decision")
        remember(db, "critical invariant rule", decision_type=LOCKED)

        active = get_active_decisions(db, limit=3)  # tight non-locked limit
        types = [d["type"] for d in active]
        # Locked must be present.
        assert LOCKED in types, f"locked missing from {types}"
        # Non-locked respects the limit.
        non_locked = [t for t in types if t != LOCKED]
        assert len(non_locked) <= 3

    def test_format_handles_only_locked(self, db):
        remember(db, "Only rule", decision_type=LOCKED)
        active = get_active_decisions(db)
        out = format_decisions(active)
        assert "## Locked invariants" in out
        assert "Cross-session decisions:" not in out

    def test_format_handles_no_locked(self, db):
        remember(db, "Only regular", decision_type="decision")
        active = get_active_decisions(db)
        out = format_decisions(active)
        assert "## Locked invariants" not in out
        assert "## Cross-session decisions:" in out


class TestLockedToolValidation:
    """Validate that the `nexus_remember` MCP tool accepts `locked`."""

    def test_tool_accepts_locked_type(self, sample_project):
        from mcp.server.fastmcp import FastMCP

        from nexus.server.state import _call_timestamps, activate_project
        from nexus.server.tools_refactor import register

        _call_timestamps.clear()
        try:
            activate_project(str(sample_project))

            test_mcp = FastMCP("test")
            register(test_mcp)

            remember_fn = test_mcp._tool_manager._tools["nexus_remember"]
            result = remember_fn.fn(
                content="Never rename public MCP tool identifiers without a migration",
                type="locked",
            )
            assert "LOCKED" in result or "locked" in result.lower()
        finally:
            _call_timestamps.clear()

    def test_tool_rejects_invalid_type(self, sample_project):
        from mcp.server.fastmcp import FastMCP

        from nexus.server.state import _call_timestamps, activate_project
        from nexus.server.tools_refactor import register

        _call_timestamps.clear()
        try:
            activate_project(str(sample_project))

            test_mcp = FastMCP("test")
            register(test_mcp)

            remember_fn = test_mcp._tool_manager._tools["nexus_remember"]
            result = remember_fn.fn(content="bad", type="pinned")
            assert "Invalid type" in result
        finally:
            _call_timestamps.clear()

    def test_tool_enforces_locked_cap(self, sample_project):
        from mcp.server.fastmcp import FastMCP

        from nexus.server.state import _call_timestamps, activate_project
        from nexus.server.tools_refactor import register

        _call_timestamps.clear()
        try:
            activate_project(str(sample_project))

            test_mcp = FastMCP("test")
            register(test_mcp)

            remember_fn = test_mcp._tool_manager._tools["nexus_remember"]
            huge = " ".join(["word"] * 100)  # 100 > MAX_WORDS_LOCKED (60)
            result = remember_fn.fn(content=huge, type="locked")
            assert "too long" in result
        finally:
            _call_timestamps.clear()
