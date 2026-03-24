"""Indexing pipeline: scan → parse → build graph."""

from __future__ import annotations

import signal
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path

from nexus.index.graph import build_intra_file_edges, resolve_imports
from nexus.index.parser import parse_file, ParseResult
from nexus.index.scanner import ScanResult, scan_project
from nexus.store.db import NexusDB
from nexus.util.config import ProjectConfig

_PARSE_TIMEOUT_S = 5  # Max seconds per file parse


@dataclass
class IndexResult:
    """Full indexing result."""
    scan: ScanResult | None = None
    symbols_added: int = 0
    edges_added: int = 0
    imports_resolved: int = 0
    imports_unresolved: int = 0
    parse_errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


def index_project(
    config: ProjectConfig,
    db: NexusDB,
    force: bool = False,
    lazy: bool = False,
) -> IndexResult:
    """Run the full indexing pipeline for a project.

    1. Scan for new/changed files
    2. Parse changed files with tree-sitter (skipped if lazy=True)
    3. Build intra-file edges (class→method contains)
    4. Resolve cross-file import edges

    If lazy=True, only registers file paths/hashes — parsing is deferred.
    Call parse_unparsed_files() later, or index_project(lazy=False) to parse.
    """
    start = time.monotonic()
    result = IndexResult()

    # Step 1: Scan
    result.scan = scan_project(config, db, force=force)

    if lazy:
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    # Step 2 & 3: Parse changed/new files and build intra-file edges
    with db.connect() as conn:
        # Get all files that need parsing (changed or new)
        # We identify them by checking which files were just upserted
        # For simplicity, re-parse files with no symbols yet, or if forced
        if force:
            rows = conn.execute("SELECT id, path, language FROM files").fetchall()
        else:
            rows = conn.execute(
                "SELECT f.id, f.path, f.language FROM files f "
                "WHERE NOT EXISTS (SELECT 1 FROM symbols s WHERE s.file_id = f.id)"
            ).fetchall()

    files_to_parse = [dict(r) for r in rows]

    for file_info in files_to_parse:
        file_id = file_info["id"]
        rel_path = file_info["path"]
        language = file_info["language"]

        if not language:
            continue

        abs_path = config.root / rel_path
        if not abs_path.exists():
            continue

        parsed = _parse_with_timeout(abs_path, language, _PARSE_TIMEOUT_S)

        if parsed.errors:
            result.parse_errors.extend(
                f"{rel_path}: {e}" for e in parsed.errors
            )
            # Don't skip — still index whatever symbols were extracted
            # (tree-sitter produces partial trees on syntax errors)

        # Insert symbols
        for sym in parsed.symbols:
            db.insert_symbol(
                file_id=file_id,
                name=sym.name,
                qualified=sym.qualified,
                kind=sym.kind,
                line_start=sym.line_start,
                line_end=sym.line_end,
                signature=sym.signature,
                docstring=sym.docstring,
                body_text=sym.body_text,
                visibility=sym.visibility,
                decorators=sym.decorators,
            )
            result.symbols_added += 1

        # Build intra-file edges
        result.edges_added += build_intra_file_edges(db, file_id, parsed.symbols)

    # Step 4: Resolve imports (second pass — all symbols are now in DB)
    # Skip if nothing changed (no new symbols to resolve against)
    if result.symbols_added == 0 and not force:
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    # Clear old unresolved imports before re-resolving
    with db.connect() as conn:
        conn.execute("DELETE FROM unresolved_imports")
        all_files = conn.execute(
            "SELECT id, path, language FROM files WHERE language IS NOT NULL"
        ).fetchall()

    for file_info in all_files:
        file_id = file_info["id"]
        rel_path = file_info["path"]
        language = file_info["language"]

        abs_path = config.root / rel_path
        if not abs_path.exists():
            continue

        parsed = parse_file(abs_path, language)
        if parsed.imports:
            file_symbols = {
                s["qualified"]: s["id"]
                for s in db.get_symbols_for_file(file_id)
            }
            resolved = resolve_imports(db, file_id, parsed.imports, file_symbols)
            result.imports_resolved += resolved
            result.imports_unresolved += sum(
                1 for imp in parsed.imports
                if imp.is_from and imp.names
            ) - resolved

    # Record scan metadata
    stats = db.get_stats()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO scan_meta (started_at, completed_at, files_total, files_changed, "
            "symbols_total, edges_total, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                start,
                time.monotonic(),
                result.scan.files_total if result.scan else 0,
                (result.scan.files_changed + result.scan.files_new) if result.scan else 0,
                stats["symbols"],
                stats["edges"],
                int((time.monotonic() - start) * 1000),
            ),
        )

    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def parse_unparsed_files(
    config: ProjectConfig,
    db: NexusDB,
    file_ids: list[int] | None = None,
) -> int:
    """Parse files that have been scanned but not yet parsed.

    If file_ids is provided, only parse those specific files.
    Returns the number of symbols added.
    """
    with db.connect() as conn:
        if file_ids:
            placeholders = ",".join("?" for _ in file_ids)
            rows = conn.execute(
                f"SELECT f.id, f.path, f.language FROM files f "
                f"WHERE f.id IN ({placeholders}) "
                f"AND NOT EXISTS (SELECT 1 FROM symbols s WHERE s.file_id = f.id)",
                file_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT f.id, f.path, f.language FROM files f "
                "WHERE NOT EXISTS (SELECT 1 FROM symbols s WHERE s.file_id = f.id)"
            ).fetchall()

    symbols_added = 0
    for row in rows:
        file_id, rel_path, language = row["id"], row["path"], row["language"]
        if not language:
            continue
        abs_path = config.root / rel_path
        if not abs_path.exists():
            continue

        parsed = _parse_with_timeout(abs_path, language, _PARSE_TIMEOUT_S)
        for sym in parsed.symbols:
            db.insert_symbol(
                file_id=file_id, name=sym.name, qualified=sym.qualified,
                kind=sym.kind, line_start=sym.line_start, line_end=sym.line_end,
                signature=sym.signature, docstring=sym.docstring,
                body_text=sym.body_text, visibility=sym.visibility,
                decorators=sym.decorators,
            )
            symbols_added += 1
        build_intra_file_edges(db, file_id, parsed.symbols)

    return symbols_added


def _parse_with_timeout(path: Path, language: str, timeout_s: int) -> ParseResult:
    """Parse a file with a timeout to prevent hanging on huge files."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(parse_file, path, language)
        try:
            return future.result(timeout=timeout_s)
        except (FuturesTimeout, TimeoutError):
            return ParseResult(errors=[f"Parse timeout ({timeout_s}s exceeded)"])
        except Exception as e:
            return ParseResult(errors=[f"Parse error: {e}"])
