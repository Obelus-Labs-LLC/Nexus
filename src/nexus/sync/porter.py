"""Export/import portable Nexus state for multi-machine sync.

The symbol index (files, symbols, edges) is rebuilt from source code,
so it doesn't need syncing. What does need syncing:
  - Cross-session decisions (active context about what you're working on)
  - Session action history (recency signal for ranking)
  - Query history (analytics data for tuning)

Export format: JSON lines (.jsonl) for easy merging and diffing.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nexus.store.db import NexusDB
from nexus.util.config import ProjectConfig, load_config


@dataclass
class SyncManifest:
    """Metadata about a sync export."""
    machine_id: str
    export_time: float
    projects: list[str]
    decisions_count: int
    actions_count: int
    queries_count: int


def export_state(
    config_path: Path,
    output_path: Path,
    machine_id: str = "",
) -> SyncManifest:
    """Export portable state from all indexed projects to a JSONL file.

    Args:
        config_path: Path to nexus.toml.
        output_path: Where to write the export file (.jsonl).
        machine_id: Identifier for this machine (default: hostname).
    """
    import socket
    if not machine_id:
        machine_id = socket.gethostname()

    projects = load_config(config_path)
    manifest = SyncManifest(
        machine_id=machine_id,
        export_time=time.time(),
        projects=[],
        decisions_count=0,
        actions_count=0,
        queries_count=0,
    )

    with open(output_path, "w") as f:
        for name, cfg in projects.items():
            if not cfg.db_path.exists():
                continue

            manifest.projects.append(name)
            db = NexusDB(cfg.db_path)

            with db.connect() as conn:
                # Export decisions
                rows = conn.execute(
                    "SELECT content, type, tags, files, created_at, expires_at, session_id "
                    "FROM decisions WHERE expires_at > ?", (time.time(),)
                ).fetchall()
                for row in rows:
                    record = {
                        "kind": "decision",
                        "project": name,
                        "machine": machine_id,
                        **dict(row),
                    }
                    f.write(json.dumps(record) + "\n")
                    manifest.decisions_count += 1

                # Export recent session actions (last 7 days)
                cutoff = time.time() - 7 * 86400
                rows = conn.execute(
                    "SELECT session_id, action, target, symbol, timestamp, metadata "
                    "FROM session_actions WHERE timestamp > ?", (cutoff,)
                ).fetchall()
                for row in rows:
                    record = {
                        "kind": "action",
                        "project": name,
                        "machine": machine_id,
                        **dict(row),
                    }
                    f.write(json.dumps(record) + "\n")
                    manifest.actions_count += 1

                # Export query history (last 30 days)
                cutoff30 = time.time() - 30 * 86400
                try:
                    rows = conn.execute(
                        "SELECT query, result_files, confidence, session_id, timestamp "
                        "FROM query_history WHERE timestamp > ?", (cutoff30,)
                    ).fetchall()
                    for row in rows:
                        record = {
                            "kind": "query",
                            "project": name,
                            "machine": machine_id,
                            **dict(row),
                        }
                        f.write(json.dumps(record) + "\n")
                        manifest.queries_count += 1
                except Exception:
                    pass  # query_history table may not exist yet

            db.close()

        # Write manifest as last line
        f.write(json.dumps({"kind": "manifest", **asdict(manifest)}) + "\n")

    return manifest


def import_state(
    config_path: Path,
    input_path: Path,
    merge_strategy: str = "newer_wins",
) -> dict[str, int]:
    """Import portable state from a JSONL export file.

    Args:
        config_path: Path to nexus.toml.
        input_path: Path to the .jsonl export file.
        merge_strategy: How to handle conflicts.
            "newer_wins" — keep the more recent record.
            "source_wins" — always use the imported record.
            "skip_existing" — only import records that don't exist.

    Returns a dict with counts of imported records by kind.
    """
    projects = load_config(config_path)
    counts: dict[str, int] = {"decisions": 0, "actions": 0, "queries": 0}

    # Open all project DBs
    project_dbs: dict[str, NexusDB] = {}
    for name, cfg in projects.items():
        if cfg.db_path.exists():
            project_dbs[name] = NexusDB(cfg.db_path)

    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            kind = record.get("kind")
            project = record.get("project")

            if kind == "manifest":
                continue

            db = project_dbs.get(project)
            if not db:
                continue

            if kind == "decision":
                _import_decision(db, record, merge_strategy)
                counts["decisions"] += 1
            elif kind == "action":
                _import_action(db, record, merge_strategy)
                counts["actions"] += 1
            elif kind == "query":
                _import_query(db, record, merge_strategy)
                counts["queries"] += 1

    for db in project_dbs.values():
        db.close()

    return counts


def _import_decision(db: NexusDB, record: dict, strategy: str) -> None:
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id, created_at FROM decisions WHERE content = ? AND type = ?",
            (record["content"], record["type"]),
        ).fetchone()

        if existing:
            if strategy == "newer_wins" and record["created_at"] > existing["created_at"]:
                conn.execute(
                    "UPDATE decisions SET expires_at = ?, session_id = ? WHERE id = ?",
                    (record["expires_at"], record["session_id"], existing["id"]),
                )
            elif strategy == "source_wins":
                conn.execute(
                    "UPDATE decisions SET expires_at = ?, session_id = ? WHERE id = ?",
                    (record["expires_at"], record["session_id"], existing["id"]),
                )
            # skip_existing: do nothing
        else:
            conn.execute(
                "INSERT INTO decisions (content, type, tags, files, created_at, expires_at, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["content"], record["type"], record.get("tags"),
                 record.get("files"), record["created_at"], record["expires_at"],
                 record["session_id"]),
            )


def _import_action(db: NexusDB, record: dict, strategy: str) -> None:
    with db.connect() as conn:
        # Check for duplicate by timestamp + session_id + target
        existing = conn.execute(
            "SELECT id FROM session_actions WHERE session_id = ? AND timestamp = ? AND target = ?",
            (record["session_id"], record["timestamp"], record["target"]),
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO session_actions (session_id, action, target, symbol, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (record["session_id"], record["action"], record["target"],
                 record.get("symbol"), record["timestamp"], record.get("metadata")),
            )


def _import_query(db: NexusDB, record: dict, strategy: str) -> None:
    with db.connect() as conn:
        # Check for duplicate
        existing = conn.execute(
            "SELECT id FROM query_history WHERE session_id = ? AND timestamp = ? AND query = ?",
            (record["session_id"], record["timestamp"], record["query"]),
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO query_history (query, result_files, confidence, session_id, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (record["query"], record.get("result_files"), record.get("confidence"),
                 record["session_id"], record["timestamp"]),
            )
