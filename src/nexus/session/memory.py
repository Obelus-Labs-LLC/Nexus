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

_VALID_TYPES = {DECISION, TASK, NEXT, FACT, BLOCKER}

# Default TTL: 7 days in seconds
DEFAULT_TTL = 7 * 24 * 3600

# Max words per decision
MAX_WORDS = 20

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
        content: Decision text (max 20 words — enforced).
        decision_type: One of: decision, task, next, fact, blocker.
        tags: Comma-separated tags for filtering.
        files: Comma-separated file paths this decision relates to.
        session_id: Current session ID.
        ttl: Time-to-live in seconds (default: 7 days).

    Returns:
        The decision ID.
    """
    if decision_type not in _VALID_TYPES:
        raise ValueError(f"Invalid type '{decision_type}'. Must be one of: {_VALID_TYPES}")

    # Enforce word limit
    words = content.split()
    if len(words) > MAX_WORDS:
        content = " ".join(words[:MAX_WORDS])

    now = time.time()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (content, type, tags, files, created_at, expires_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content, decision_type, tags, files, now, now + ttl, session_id),
        )
        return cur.lastrowid


def get_active_decisions(
    db: NexusDB,
    tags: str | None = None,
    decision_type: str | None = None,
    limit: int = MAX_INJECT,
) -> list[dict[str, Any]]:
    """Get active (non-expired) decisions.

    Args:
        db: Database instance.
        tags: Filter by tag substring.
        decision_type: Filter by type.
        limit: Max results.
    """
    now = time.time()
    query = "SELECT * FROM decisions WHERE expires_at > ?"
    params: list[Any] = [now]

    if decision_type:
        query += " AND type = ?"
        params.append(decision_type)

    if tags:
        query += " AND tags LIKE ?"
        params.append(f"%{tags}%")

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]


def cleanup_expired(db: NexusDB) -> int:
    """Remove expired decisions. Returns count deleted."""
    now = time.time()
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM decisions WHERE expires_at <= ?", (now,))
        return cur.rowcount


def format_decisions(decisions: list[dict[str, Any]]) -> str:
    """Format decisions for injection into session start."""
    if not decisions:
        return ""

    lines = ["## Cross-session decisions:"]
    for d in decisions:
        prefix = f"[{d['type']}]"
        tags = f" ({d['tags']})" if d.get("tags") else ""
        lines.append(f"  {prefix} {d['content']}{tags}")

    return "\n".join(lines)
