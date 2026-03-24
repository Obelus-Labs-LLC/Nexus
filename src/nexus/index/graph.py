"""Build graph edges from parsed symbols and imports."""

from __future__ import annotations

from nexus.index.parser import Import, Symbol
from nexus.store.db import NexusDB


def build_intra_file_edges(db: NexusDB, file_id: int, symbols: list[Symbol]) -> int:
    """Create 'contains' edges between classes and their methods.

    Returns the number of edges created.
    """
    edges_created = 0

    # Map qualified names to symbol DB IDs
    db_symbols = db.get_symbols_for_file(file_id)
    qualified_to_id: dict[str, int] = {s["qualified"]: s["id"] for s in db_symbols}

    for sym in symbols:
        if sym.kind == "method":
            # parent qualified = everything before the last separator
            # Python uses '.', Rust uses '::'
            if "::" in sym.qualified:
                parts = sym.qualified.rsplit("::", 1)
            else:
                parts = sym.qualified.rsplit(".", 1)
            if len(parts) == 2:
                parent_q = parts[0]
                if parent_q in qualified_to_id and sym.qualified in qualified_to_id:
                    db.insert_edge(
                        source_id=qualified_to_id[parent_q],
                        target_id=qualified_to_id[sym.qualified],
                        kind="contains",
                    )
                    edges_created += 1

    return edges_created


def resolve_imports(
    db: NexusDB,
    file_id: int,
    imports: list[Import],
    file_symbols: dict[str, int],
) -> int:
    """Attempt to resolve imports to symbols in the database.

    Creates 'imports' edges for resolved imports and records unresolved ones.
    Returns the number of resolved edges.

    Args:
        db: Database instance.
        file_id: ID of the importing file.
        imports: Parsed imports from the file.
        file_symbols: Map of qualified name -> symbol ID for symbols in this file.
    """
    resolved = 0
    # Get a source symbol to attach edges to (first symbol in file, or None)
    source_symbols = db.get_symbols_for_file(file_id)
    if not source_symbols:
        return 0

    # Use the module-level symbol or first symbol as the import source
    source_id = source_symbols[0]["id"]

    for imp in imports:
        if imp.is_from and imp.names:
            # from foo.bar import baz -> look for foo.bar.baz
            for name in imp.names:
                if name == "*":
                    continue
                qualified = f"{imp.module}.{name}"
                targets = _find_target(db, qualified, name)
                if targets:
                    for tid in targets:
                        db.insert_edge(source_id, tid, "imports")
                        resolved += 1
                else:
                    _record_unresolved(db, file_id, f"{imp.module}.{name}", imp.line)
        else:
            # import foo / import foo.bar
            targets = _find_target(db, imp.module, imp.module.split(".")[-1])
            if targets:
                for tid in targets:
                    db.insert_edge(source_id, tid, "imports")
                    resolved += 1
            else:
                _record_unresolved(db, file_id, imp.module, imp.line)

    return resolved


def _find_target(db: NexusDB, qualified: str, name: str) -> list[int]:
    """Find symbol IDs matching an import target."""
    # First try exact qualified match
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM symbols WHERE qualified = ?", (qualified,)
        ).fetchall()
        if rows:
            return [r["id"] for r in rows]

        # Try matching by name (less precise but catches module-level symbols)
        rows = conn.execute(
            "SELECT id FROM symbols WHERE name = ? AND kind IN ('class', 'function', 'module')",
            (name,),
        ).fetchall()
        return [r["id"] for r in rows]


def _record_unresolved(db: NexusDB, file_id: int, import_path: str, line: int) -> None:
    """Record an unresolved import."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO unresolved_imports (file_id, import_path, line) VALUES (?, ?, ?)",
            (file_id, import_path, line),
        )
