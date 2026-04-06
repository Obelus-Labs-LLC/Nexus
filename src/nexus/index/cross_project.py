"""Cross-project edge detection for clustered projects.

Resolves unresolved imports against sibling projects in the same cluster.
For example, if project_a imports from project_b, this module detects that
and creates cross-project edges stored in a cluster-level database.

Uses import-boundary checksums for incremental resolution: if a project's
unresolved imports haven't changed since last resolution, it's skipped.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from nexus.store.db import NexusDB
from nexus.util.config import ProjectConfig


@dataclass
class CrossProjectResult:
    """Result of cross-project edge resolution."""
    edges_added: int = 0
    projects_linked: set[tuple[str, str]] = field(default_factory=set)
    duration_ms: int = 0
    projects_skipped: int = 0  # Projects skipped due to unchanged imports


def _get_import_checksum(db: NexusDB) -> str:
    """Compute a stable checksum of a project's unresolved imports."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT import_path FROM unresolved_imports ORDER BY import_path"
        ).fetchall()
    joined = ",".join(r["import_path"] for r in rows)
    return hashlib.md5(joined.encode()).hexdigest()  # noqa: S324 — not security-sensitive


def _get_stored_checksum(db: NexusDB, project_name: str) -> str | None:
    """Get the previously stored import checksum for this project."""
    try:
        with db.connect() as conn:
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='import_checksums'"
            ).fetchone()
            if not table_check:
                return None
            row = conn.execute(
                "SELECT checksum FROM import_checksums WHERE project_name = ?",
                (project_name,),
            ).fetchone()
            return row["checksum"] if row else None
    except Exception:
        return None


def _store_checksum(db: NexusDB, project_name: str, checksum: str) -> None:
    """Persist the import checksum for this project."""
    try:
        with db.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS import_checksums (
                    project_name TEXT PRIMARY KEY,
                    checksum     TEXT NOT NULL,
                    computed_at  REAL NOT NULL
                )"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO import_checksums (project_name, checksum, computed_at) "
                "VALUES (?, ?, ?)",
                (project_name, checksum, time.time()),
            )
    except Exception:
        pass


def resolve_cross_project_edges(
    projects: dict[str, ProjectConfig],
    cluster: str,
    force: bool = False,
) -> CrossProjectResult:
    """Resolve unresolved imports across projects in the same cluster.

    For each project's unresolved imports, searches sibling projects'
    symbol tables for matches. Creates edges in a shared cluster database.

    Incremental: projects whose import boundary hasn't changed are skipped
    unless force=True.
    """
    start = time.monotonic()
    result = CrossProjectResult()

    # Filter to projects in this cluster
    cluster_projects = {
        name: cfg for name, cfg in projects.items()
        if cfg.cluster == cluster and cfg.cross_project
    }

    if len(cluster_projects) < 2:
        return result

    # Load all project DBs and their symbol indices
    project_dbs: dict[str, NexusDB] = {}
    project_checksums: dict[str, str] = {}
    # symbol_name -> [(project_name, symbol_qualified, file_path)]
    global_symbol_index: dict[str, list[tuple[str, str, str]]] = {}

    for name, cfg in cluster_projects.items():
        if not cfg.db_path.exists():
            continue
        db = NexusDB(cfg.db_path)
        project_dbs[name] = db

        # Compute checksum for incremental detection
        checksum = _get_import_checksum(db)
        project_checksums[name] = checksum

        # Index all symbols from this project
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT s.name, s.qualified, f.path FROM symbols s "
                "JOIN files f ON s.file_id = f.id"
            ).fetchall()

        for row in rows:
            sym_name = row["name"]
            if sym_name not in global_symbol_index:
                global_symbol_index[sym_name] = []
            global_symbol_index[sym_name].append(
                (name, row["qualified"], row["path"])
            )

    # For each project, check unresolved imports against the global index
    for proj_name, db in project_dbs.items():
        checksum = project_checksums[proj_name]

        # Skip if imports haven't changed since last resolution
        if not force:
            stored = _get_stored_checksum(db, proj_name)
            if stored == checksum:
                result.projects_skipped += 1
                continue

        with db.connect() as conn:
            unresolved = conn.execute(
                "SELECT import_path, file_id FROM unresolved_imports"
            ).fetchall()

        for row in unresolved:
            import_path = row["import_path"]
            # Extract the symbol name (last component of import path)
            parts = import_path.replace("::", ".").split(".")
            sym_name = parts[-1] if parts else import_path

            matches = global_symbol_index.get(sym_name, [])
            for target_proj, target_qualified, target_file in matches:
                if target_proj == proj_name:
                    continue  # Skip same-project matches

                # Record cross-project edge
                _record_cross_edge(
                    db, proj_name, import_path,
                    target_proj, target_qualified, target_file,
                )
                result.edges_added += 1
                result.projects_linked.add(
                    (min(proj_name, target_proj), max(proj_name, target_proj))
                )

        # Store the new checksum
        _store_checksum(db, proj_name, checksum)

    # Close all DBs
    for db in project_dbs.values():
        db.close()

    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def _record_cross_edge(
    db: NexusDB,
    source_project: str,
    source_import: str,
    target_project: str,
    target_qualified: str,
    target_file: str,
) -> None:
    """Record a cross-project dependency edge."""
    with db.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO cross_project_edges
               (source_project, source_import, target_project, target_qualified, target_file)
               VALUES (?, ?, ?, ?, ?)""",
            (source_project, source_import, target_project, target_qualified, target_file),
        )
