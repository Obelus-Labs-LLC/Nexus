"""SQLite database connection manager with WAL mode."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_CURRENT_VERSION = 4  # Bump when schema changes

# Migration functions: version -> callable
# Each migration takes a connection and upgrades FROM (version-1) TO (version).
_MIGRATIONS: dict[int, str] = {
    2: """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER NOT NULL,
            applied_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_tags (
            file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            tag         TEXT NOT NULL,
            PRIMARY KEY (file_id, tag)
        );
    """,
    3: """
        CREATE TABLE IF NOT EXISTS query_history (
            id          INTEGER PRIMARY KEY,
            query       TEXT NOT NULL,
            result_files TEXT,
            confidence  TEXT,
            session_id  TEXT NOT NULL,
            timestamp   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_query_history_ts ON query_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_query_history_query ON query_history(query);
        CREATE TABLE IF NOT EXISTS cross_project_edges (
            id              INTEGER PRIMARY KEY,
            source_project  TEXT NOT NULL,
            source_import   TEXT NOT NULL,
            target_project  TEXT NOT NULL,
            target_qualified TEXT NOT NULL,
            target_file     TEXT NOT NULL,
            UNIQUE(source_project, source_import, target_project, target_qualified)
        );
        CREATE INDEX IF NOT EXISTS idx_cross_edges_source ON cross_project_edges(source_project);
        CREATE INDEX IF NOT EXISTS idx_cross_edges_target ON cross_project_edges(target_project);
    """,
}


class NexusDB:
    """Per-project SQLite database for Nexus graph storage."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()
        self._check_integrity()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA_PATH.read_text())
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply any pending schema migrations."""
        # Check current version
        try:
            row = conn.execute(
                "SELECT MAX(version) as v FROM schema_version"
            ).fetchone()
            current = row["v"] if row and row["v"] else 1
        except sqlite3.OperationalError:
            current = 1

        if current >= _CURRENT_VERSION:
            return

        for ver in range(current + 1, _CURRENT_VERSION + 1):
            sql = _MIGRATIONS.get(ver)
            if sql:
                conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (ver, time.time()),
            )

    def _check_integrity(self) -> None:
        """Run a quick integrity check on startup."""
        with self.connect() as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] != "ok":
                raise RuntimeError(
                    f"Nexus database integrity check failed: {result[0]}"
                )

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create the persistent connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
        return self._conn

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        """Close the persistent connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def clear_file(self, file_id: int) -> None:
        """Remove all symbols and edges for a file before re-parsing."""
        with self.connect() as conn:
            conn.execute("DELETE FROM unresolved_imports WHERE file_id = ?", (file_id,))
            conn.execute(
                "DELETE FROM edges WHERE source_id IN (SELECT id FROM symbols WHERE file_id = ?)",
                (file_id,),
            )
            conn.execute(
                "DELETE FROM edges WHERE target_id IN (SELECT id FROM symbols WHERE file_id = ?)",
                (file_id,),
            )
            conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))

    def upsert_file(
        self,
        path: str,
        sha256: str,
        language: str | None,
        line_count: int,
        byte_size: int,
        timestamp: float,
        is_entry: bool = False,
    ) -> int:
        """Insert or update a file record. Returns the file id."""
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE files SET sha256=?, language=?, line_count=?, byte_size=?, last_parsed=?, is_entry=? WHERE id=?",
                    (sha256, language, line_count, byte_size, timestamp, is_entry, row["id"]),
                )
                return row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO files (path, sha256, language, line_count, byte_size, last_parsed, is_entry) VALUES (?,?,?,?,?,?,?)",
                    (path, sha256, language, line_count, byte_size, timestamp, is_entry),
                )
                return cur.lastrowid

    def insert_symbol(
        self,
        file_id: int,
        name: str,
        qualified: str,
        kind: str,
        line_start: int,
        line_end: int,
        signature: str | None = None,
        docstring: str | None = None,
        body_text: str | None = None,
        visibility: str = "public",
        decorators: str | None = None,
    ) -> int:
        """Insert a symbol and return its id."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO symbols
                   (file_id, name, qualified, kind, line_start, line_end,
                    signature, docstring, body_text, visibility, decorators)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (file_id, name, qualified, kind, line_start, line_end,
                 signature, docstring, body_text, visibility, decorators),
            )
            return cur.lastrowid

    def insert_edge(
        self,
        source_id: int,
        target_id: int,
        kind: str,
        weight: float = 1.0,
        metadata: str | None = None,
    ) -> None:
        """Insert a graph edge, ignoring duplicates."""
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, kind, weight, metadata) VALUES (?,?,?,?,?)",
                (source_id, target_id, kind, weight, metadata),
            )

    def tag_file(self, file_id: int, tag: str) -> None:
        """Add a tag to a file (e.g., 'generated', 'vendored')."""
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO file_tags (file_id, tag) VALUES (?, ?)",
                (file_id, tag),
            )

    def get_file_tags(self, file_id: int) -> list[str]:
        """Get all tags for a file."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT tag FROM file_tags WHERE file_id = ?", (file_id,)
            ).fetchall()
            return [r["tag"] for r in rows]

    def get_file_by_path(self, path: str) -> dict | None:
        """Look up a file by relative path."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
            return dict(row) if row else None

    def get_symbols_for_file(self, file_id: int) -> list[dict]:
        """Get all symbols in a file."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE file_id = ? ORDER BY line_start",
                (file_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_symbol_by_name(self, name: str) -> list[dict]:
        """Find symbols by name (case-insensitive substring match)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, f.path as file_path FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.name LIKE ? ORDER BY s.name",
                (f"%{name}%",),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_neighbors(self, symbol_id: int) -> list[dict]:
        """Get symbols connected to this symbol via edges."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.*, f.path as file_path, e.kind as edge_kind
                   FROM edges e
                   JOIN symbols s ON (s.id = e.target_id OR s.id = e.source_id)
                   JOIN files f ON s.file_id = f.id
                   WHERE (e.source_id = ? OR e.target_id = ?) AND s.id != ?""",
                (symbol_id, symbol_id, symbol_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get database statistics."""
        with self.connect() as conn:
            files = conn.execute("SELECT COUNT(*) as c FROM files").fetchone()["c"]
            symbols = conn.execute("SELECT COUNT(*) as c FROM symbols").fetchone()["c"]
            edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
            return {"files": files, "symbols": symbols, "edges": edges}
