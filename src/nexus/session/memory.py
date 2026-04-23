"""Cross-session decisions — persistent memory with 7-day TTL."""

from __future__ import annotations

import time
from typing import Any

from nexus.store.db import NexusDB

# Decision types
DECISION = "decision"
TASK = "task"
NEXT = "next"
FACT = "fact"
BLOCKER = "blocker"
LOCKED = "locked"  # Pinned, no-TTL invariants surfaced first at session start.

_VALID_TYPES = {DECISION, TASK, NEXT, FACT, BLOCKER, LOCKED}

# Default TTL: 7 days in seconds
DEFAULT_TTL = 7 * 24 * 3600

# "No expiry" for locked decisions: 100 years in the future. We prefer a
# sentinel timestamp over NULL so existing `expires_at > now` filters keep
# working without schema changes.
LOCKED_TTL = 100 * 365 * 24 * 3600

# Max words per decision (standard types). Locked entries allow more since
# invariants often need to be spelled out unambiguously for 4.7+ literal
# instruction-following.
MAX_WORDS = 20
MAX_WORDS_LOCKED = 60

# Max decisions injected at session start
MAX_INJECT = 15


def remember(
    db: NexusDB,
    content: str,
    decision_type: str = DECISION,
    tags: str | None = None,
    files: str | None = None,
    session_id: str = "",
    ttl: int = DEFAULT_TTL,
) -> int:
    """Store a cross-session decision.

    Args:
        db: Database instance.
        content: Decision text (max 20 words, or 60 for locked — enforced).
        decision_type: One of: decision, task, next, fact, blocker, locked.
        tags: Comma-separated tags for filtering.
        files: Comma-separated file paths this decision relates to.
        session_id: Current session ID.
        ttl: Time-to-live in seconds (default: 7 days).
            Ignored for ``locked`` type, which always uses ``LOCKED_TTL``.

    Returns:
        The decision ID.
    """
    if decision_type not in _VALID_TYPES:
        raise ValueError(f"Invalid type '{decision_type}'. Must be one of: {_VALID_TYPES}")

    # Enforce word limit. Locked invariants get a larger cap since they
    # encode multi-clause rules ("do X, never Y, only when Z").
    max_words = MAX_WORDS_LOCKED if decision_type == LOCKED else MAX_WORDS
    words = content.split()
    if len(words) > max_words:
        content = " ".join(words[:max_words])

    # Locked decisions never expire. Override ttl so callers that forget
    # to pass LOCKED_TTL still get persistence.
    effective_ttl = LOCKED_TTL if decision_type == LOCKED else ttl

    now = time.time()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (content, type, tags, files, created_at, expires_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content, decision_type, tags, files, now, now + effective_ttl, session_id),
        )
        return cur.lastrowid


def get_active_decisions(
    db: NexusDB,
    tags: str | None = None,
    decision_type: str | None = None,
    limit: int = MAX_INJECT,
) -> list[dict[str, Any]]:
    """Get active (non-expired) decisions.

    Locked decisions are always returned in addition to the regular limit —
    invariants must never be truncated out of the session-start injection.

    Args:
        db: Database instance.
        tags: Filter by tag substring.
        decision_type: Filter by type. When set, no special locked handling.
        limit: Max results for non-locked decisions.
    """
    now = time.time()

    # When the caller filters by a specific type, respect it verbatim.
    if decision_type:
        query = "SELECT * FROM decisions WHERE expires_at > ? AND type = ?"
        params: list[Any] = [now, decision_type]
        if tags:
            query += " AND tags LIKE ?"
            params.append(f"%{tags}%")
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # Default path: always include every active locked decision, then fill
    # remaining slots with the most recent non-locked decisions.
    locked_sql = "SELECT * FROM decisions WHERE expires_at > ? AND type = ?"
    locked_params: list[Any] = [now, LOCKED]
    if tags:
        locked_sql += " AND tags LIKE ?"
        locked_params.append(f"%{tags}%")
    locked_sql += " ORDER BY created_at DESC"

    other_sql = "SELECT * FROM decisions WHERE expires_at > ? AND type != ?"
    other_params: list[Any] = [now, LOCKED]
    if tags:
        other_sql += " AND tags LIKE ?"
        other_params.append(f"%{tags}%")
    other_sql += " ORDER BY created_at DESC LIMIT ?"
    other_params.append(limit)

    with db.connect() as conn:
        locked_rows = conn.execute(locked_sql, locked_params).fetchall()
        other_rows = conn.execute(other_sql, other_params).fetchall()

    return [dict(r) for r in locked_rows] + [dict(r) for r in other_rows]


def cleanup_expired(db: NexusDB) -> int:
    """Remove expired decisions. Returns count deleted."""
    now = time.time()
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM decisions WHERE expires_at <= ?", (now,))
        return cur.rowcount


def format_decisions(decisions: list[dict[str, Any]]) -> str:
    """Format decisions for injection into session start.

    Locked invariants are surfaced first under a dedicated "Locked invariants"
    header so they cannot be visually lost in a long decision list. This is
    the 4.7 mitigation anchor for the re-litigation failure mode.
    """
    if not decisions:
        return ""

    locked = [d for d in decisions if d.get("type") == LOCKED]
    others = [d for d in decisions if d.get("type") != LOCKED]

    lines: list[str] = []

    if locked:
        lines.append("## Locked invariants (do not violate):")
        for d in locked:
            tags = f" ({d['tags']})" if d.get("tags") else ""
            lines.append(f"  [LOCKED] {d['content']}{tags}")
        if others:
            lines.append("")

    if others:
        lines.append("## Cross-session decisions:")
        for d in others:
            prefix = f"[{d['type']}]"
            tags = f" ({d['tags']})" if d.get("tags") else ""
            lines.append(f"  {prefix} {d['content']}{tags}")

    return "\n".join(lines)
