"""Cross-project edge detection for clustered projects.

Resolves unresolved imports against sibling projects in the same cluster.
For example, if project_a imports from project_b, this module detects that
and creates cross-project edges stored in a cluster-level database.
"""

from __future__ import annotations

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


def resolve_cross_project_edges(
    projects: dict[str, ProjectConfig],
    cluster: str,
) -> CrossProjectResult:
    """Resolve unresolved imports across projects in the same cluster.

    For each project's unresolved imports, searches sibling projects'
    symbol tables for matches. Creates edges in a shared cluster database.
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
    # symbol_name -> [(project_name, symbol_qualified, file_path)]
    global_symbol_index: dict[str, list[tuple[str, str, str]]] = {}

    for name, cfg in cluster_projects.items():
        if not cfg.db_path.exists():
            continue
        db = NexusDB(cfg.db_path)
        project_dbs[name] = db

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
