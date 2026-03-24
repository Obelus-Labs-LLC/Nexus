"""Session action tracking — logs reads, edits, queries for recency scoring."""

from __future__ import annotations

import time
import uuid
from typing import Any

from nexus.store.db import NexusDB

# Action types
READ = "read"
EDIT = "edit"
QUERY = "query"
SCAN = "scan"


class SessionTracker:
    """Tracks actions within a session for recency-based ranking."""

    def __init__(self, db: NexusDB, session_id: str | None = None):
        self.db = db
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._action_count = 0

    def log(
        self,
        action: str,
        target: str,
        symbol: str | None = None,
        metadata: str | None = None,
    ) -> None:
        """Log a session action."""
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO session_actions (session_id, action, target, symbol, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.session_id, action, target, symbol, time.time(), metadata),
            )
        self._action_count += 1

    def log_read(self, file_path: str, symbol: str | None = None) -> None:
        self.log(READ, file_path, symbol)

    def log_edit(self, file_path: str, summary: str | None = None) -> None:
        self.log(EDIT, file_path, metadata=summary)

    def log_query(self, query: str) -> None:
        self.log(QUERY, query)

    def get_recent_files(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recently accessed files, ordered by most recent action.

        Returns list of dicts with: file_path, action_count, last_action, last_timestamp.
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT target as file_path,
                          COUNT(*) as action_count,
                          MAX(timestamp) as last_timestamp,
                          GROUP_CONCAT(DISTINCT action) as actions
                   FROM session_actions
                   WHERE action IN (?, ?)
                   GROUP BY target
                   ORDER BY MAX(timestamp) DESC
                   LIMIT ?""",
                (READ, EDIT, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recency_rankings(self, db: NexusDB) -> list[dict[str, Any]]:
        """Get file rankings based on recency of access.

        Files with more recent and more frequent access rank higher.
        Edit actions weight 2x vs reads.

        Returns list of dicts with: file_id, file_path, score, rank.
        """
        recent = self.get_recent_files()
        if not recent:
            return []

        now = time.time()
        scored: list[dict[str, Any]] = []

        for item in recent:
            file_info = db.get_file_by_path(item["file_path"])
            if not file_info:
                continue

            # Recency decay: score = action_weight / (1 + hours_since_action)
            hours_ago = (now - item["last_timestamp"]) / 3600
            action_weight = item["action_count"]

            # Boost edits
            actions = item.get("actions", "")
            if EDIT in actions:
                action_weight *= 2

            score = action_weight / (1.0 + hours_ago)

            scored.append({
                "file_id": file_info["id"],
                "file_path": item["file_path"],
                "score": score,
            })

        # Sort and assign ranks
        scored.sort(key=lambda x: x["score"], reverse=True)
        for i, item in enumerate(scored):
            item["rank"] = i

        return scored

    def get_session_summary(self) -> dict[str, Any]:
        """Summarize this session's activity."""
        with self.db.connect() as conn:
            actions = conn.execute(
                "SELECT action, COUNT(*) as c FROM session_actions WHERE session_id = ? GROUP BY action",
                (self.session_id,),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) as c FROM session_actions WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["c"]

        return {
            "session_id": self.session_id,
            "total_actions": total,
            "breakdown": {a["action"]: a["c"] for a in actions},
        }
