"""File discovery with .gitignore filtering and SHA-256 change detection."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from nexus.store.db import NexusDB
from nexus.util.config import ProjectConfig
from nexus.util.hashing import sha256_file

# Language detection by extension
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".rs": "rust",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".c": "c", ".h": "c",
    ".go": "go",
    ".java": "java",
}

# Patterns for generated/vendored files — deprioritized in ranking
_GENERATED_PATTERNS: list[str] = [
    r"_pb2\.py$",            # protobuf generated
    r"_pb2_grpc\.py$",       # grpc generated
    r"\.generated\.",        # explicit generated marker
    r"migrations/",          # database migrations
    r"alembic/versions/",    # alembic migrations
    r"__generated__/",       # codegen output
    r"\.min\.js$",           # minified JS
    r"\.min\.css$",          # minified CSS
    r"vendor/",              # vendored deps
    r"third_party/",         # third party
    r"\.d\.ts$",             # TS declaration files
    r"package-lock\.json$",  # lock files
]
_GENERATED_RE = re.compile("|".join(_GENERATED_PATTERNS))


@dataclass
class ScanResult:
    """Result of scanning a project directory."""
    files_total: int = 0
    files_changed: int = 0
    files_new: int = 0
    files_deleted: int = 0
    files_unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


def scan_project(
    config: ProjectConfig,
    db: NexusDB,
    force: bool = False,
) -> ScanResult:
    """Scan a project, detect changed files, upsert into the database.

    Returns a ScanResult summarizing what changed.
    If force=True, re-indexes all files regardless of SHA-256 match.
    """
    start = time.monotonic()
    result = ScanResult()

    # Collect gitignore patterns
    ignore_patterns = _load_gitignore(config.root)
    ignore_patterns.extend(config.all_ignore)

    # Track which paths we see (to detect deletions)
    seen_paths: set[str] = set()
    extensions = config.extensions
    entry_points = set(config.entry_points)

    # Collect all files first, then hash in parallel
    file_list = list(_walk_files(config.root, ignore_patterns, extensions, config.max_files))
    result.files_total = len(file_list)

    # Parallel SHA-256 hashing (IO-bound, threads help)
    hashes: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        hash_futures = {pool.submit(sha256_file, fp): fp for fp in file_list}
        for future in hash_futures:
            fp = hash_futures[future]
            try:
                hashes[fp] = future.result(timeout=10)
            except Exception as e:
                rel = fp.relative_to(config.root).as_posix()
                result.errors.append(f"{rel}: hash error: {e}")

    for file_path in file_list:
        rel_path = file_path.relative_to(config.root).as_posix()
        seen_paths.add(rel_path)

        sha = hashes.get(file_path)
        if sha is None:
            continue  # hash failed, already logged

        try:
            stat = file_path.stat()
            lang = _EXT_TO_LANG.get(file_path.suffix)
            is_entry = rel_path in entry_points

            existing = db.get_file_by_path(rel_path)

            if existing and existing["sha256"] == sha and not force:
                result.files_unchanged += 1
                continue

            file_id = db.upsert_file(
                path=rel_path,
                sha256=sha,
                language=lang,
                line_count=_count_lines(file_path),
                byte_size=stat.st_size,
                timestamp=time.time(),
                is_entry=is_entry,
            )

            # Tag generated/vendored files
            if is_generated(rel_path):
                db.tag_file(file_id, "generated")

            if existing:
                # File changed — clear old symbols/edges before re-parse
                db.clear_file(file_id)
                result.files_changed += 1
            else:
                result.files_new += 1

        except Exception as e:
            result.errors.append(f"{rel_path}: {e}")

    # Detect deleted files
    result.files_deleted = _remove_deleted_files(db, seen_paths)

    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def is_generated(rel_path: str) -> bool:
    """Check if a file matches generated/vendored patterns."""
    return bool(_GENERATED_RE.search(rel_path))


def _walk_files(
    root: Path,
    ignore_patterns: list[str],
    extensions: set[str],
    max_files: int,
) -> Iterator[Path]:
    """Walk directory tree yielding files that pass filters."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter directories in-place to prune the walk
        dirnames[:] = [
            d for d in dirnames
            if not _should_ignore(d, ignore_patterns, is_dir=True)
            and not d.startswith(".")
        ]

        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir != "." and _should_ignore(rel_dir, ignore_patterns, is_dir=True):
            dirnames.clear()
            continue

        for fname in filenames:
            if count >= max_files:
                return

            fpath = Path(dirpath) / fname
            if not extensions or fpath.suffix in extensions:
                rel = fpath.relative_to(root).as_posix()
                if not _should_ignore(rel, ignore_patterns, is_dir=False):
                    yield fpath
                    count += 1


def _should_ignore(path: str, patterns: list[str], is_dir: bool = False) -> bool:
    """Check if a path matches any ignore pattern."""
    name = Path(path).name
    for pattern in patterns:
        # Direct name match (e.g., "node_modules", "*.pyc")
        if fnmatch.fnmatch(name, pattern):
            return True
        # Full path match (e.g., "build/output")
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _load_gitignore(root: Path) -> list[str]:
    """Load .gitignore patterns from the project root."""
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []

    patterns: list[str] = []
    for line in gitignore.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip trailing slashes (directory indicators) — fnmatch handles both
        patterns.append(line.rstrip("/"))
    return patterns


def _count_lines(path: Path) -> int:
    """Count lines in a file, handling encoding errors."""
    try:
        return sum(1 for _ in open(path, "r", errors="replace"))
    except Exception:
        return 0


def _remove_deleted_files(db: NexusDB, seen_paths: set[str]) -> int:
    """Remove files from the database that no longer exist on disk."""
    deleted = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        for row in rows:
            if row["path"] not in seen_paths:
                conn.execute("DELETE FROM files WHERE id = ?", (row["id"],))
                deleted += 1
    return deleted
