"""Concept graph — long-lived project knowledge (MegaMemory-inspired).

Unlike `decisions` (ephemeral 7-day TTL, 20-word cap), concepts accumulate
across sessions and form a traversable graph. A concept is a named node
in the project's knowledge base:

  - architecture ideas  ("hexagonal architecture")
  - design patterns     ("strategy pattern used for extractors")
  - conventions         ("all extractors return ParseResult")
  - risks               ("SQLite WAL requires POSIX file locking")
  - glossary terms      ("RRF = Reciprocal Rank Fusion, k=60")

Concepts can be linked to:
  - other concepts (via `concept_edges`, e.g. 'depends_on', 'contradicts')
  - files (via `concept_files`, e.g. "this file embodies X")
  - symbols (via `concept_symbols`, pin to a specific class/function)

The graph is traversable with `get_concept_neighbors()` for multi-hop
reasoning by `nexus_explore`.
"""

from __future__ import annotations

import time
from typing import Any

from nexus.store.db import NexusDB

# Valid concept kinds — open-ended but enforced for consistency.
VALID_KINDS = frozenset({
    "concept", "pattern", "convention", "risk", "glossary",
    "architecture", "decision", "invariant",
})

# Valid relation types between concepts. Symmetric or directed — caller decides.
VALID_RELATIONS = frozenset({
    "related_to", "depends_on", "contradicts", "refines",
    "example_of", "implements", "replaces", "used_by",
})


def upsert_concept(
    db: NexusDB,
    name: str,
    summary: str,
    kind: str = "concept",
    body: str | None = None,
    confidence: float = 0.5,
    session_id: str = "",
) -> int:
    """Create or update a concept by name (case-insensitive).

    Returns the concept id. If a concept with this name already exists,
    the summary/body/confidence are updated and updated_at is refreshed.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind '{kind}'. Must be one of: {sorted(VALID_KINDS)}")
    if not name.strip():
        raise ValueError("Concept name cannot be empty")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must be in [0,1], got {confidence}")

    now = time.time()
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM concepts WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE concepts SET summary = ?, kind = ?, body = ?, "
                "confidence = ?, updated_at = ?, session_id = ? WHERE id = ?",
                (summary, kind, body, confidence, now, session_id, existing["id"]),
            )
            return int(existing["id"])

        cur = conn.execute(
            "INSERT INTO concepts (name, kind, summary, body, confidence, "
            "created_at, updated_at, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, summary, body, confidence, now, now, session_id),
        )
        return int(cur.lastrowid)


def get_concept(db: NexusDB, name: str) -> dict[str, Any] | None:
    """Fetch a single concept by name (case-insensitive). None if not found."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM concepts WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
    return dict(row) if row else None


def list_concepts(
    db: NexusDB,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List concepts, optionally filtered by kind, ordered by most recently updated."""
    query = "SELECT * FROM concepts"
    params: list[Any] = []
    if kind:
        query += " WHERE kind = ?"
        params.append(kind)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def link_concepts(
    db: NexusDB,
    source_name: str,
    target_name: str,
    relation: str = "related_to",
    weight: float = 1.0,
) -> int:
    """Create a typed edge between two concepts (creates them if absent).

    Returns the edge id.
    """
    if relation not in VALID_RELATIONS:
        raise ValueError(f"Invalid relation '{relation}'. Must be one of: {sorted(VALID_RELATIONS)}")

    # Ensure both concepts exist, but DO NOT clobber existing ones — only
    # create stubs when missing. This lets callers link already-rich concepts
    # (with their own kind/summary) without losing that data.
    def _ensure(n: str) -> int:
        existing = get_concept(db, n)
        if existing:
            return int(existing["id"])
        return upsert_concept(db, n, summary=f"(auto-created via link: {n})")

    source_id = _ensure(source_name)
    target_id = _ensure(target_name)

    now = time.time()
    with db.connect() as conn:
        # If the edge already exists, update weight only; don't duplicate.
        existing = conn.execute(
            "SELECT id FROM concept_edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE concept_edges SET weight = ? WHERE id = ?",
                (weight, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO concept_edges (source_id, target_id, relation, weight, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, relation, weight, now),
        )
        return int(cur.lastrowid)


def attach_concept_to_file(
    db: NexusDB,
    concept_name: str,
    file_path: str,
    weight: float = 1.0,
) -> bool:
    """Link a concept to a file. Returns True if the file was found and linked."""
    with db.connect() as conn:
        concept = conn.execute(
            "SELECT id FROM concepts WHERE name = ? COLLATE NOCASE", (concept_name,)
        ).fetchone()
        if not concept:
            return False
        file_row = conn.execute(
            "SELECT id FROM files WHERE path = ?", (file_path,)
        ).fetchone()
        if not file_row:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO concept_files (concept_id, file_id, weight) VALUES (?, ?, ?)",
            (concept["id"], file_row["id"], weight),
        )
    return True


def attach_concept_to_symbol(
    db: NexusDB,
    concept_name: str,
    symbol_qualified: str,
    weight: float = 1.0,
) -> bool:
    """Link a concept to a symbol by its qualified name. Returns success."""
    with db.connect() as conn:
        concept = conn.execute(
            "SELECT id FROM concepts WHERE name = ? COLLATE NOCASE", (concept_name,)
        ).fetchone()
        if not concept:
            return False
        sym = conn.execute(
            "SELECT id FROM symbols WHERE qualified = ?", (symbol_qualified,)
        ).fetchone()
        if not sym:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO concept_symbols (concept_id, symbol_id, weight) VALUES (?, ?, ?)",
            (concept["id"], sym["id"], weight),
        )
    return True


def get_concept_neighbors(
    db: NexusDB,
    name: str,
    depth: int = 1,
    max_nodes: int = 20,
) -> dict[str, Any]:
    """BFS expand a concept's neighborhood up to `depth` hops.

    Returns a dict with:
      - center: the starting concept
      - nodes:  list of concept dicts reached (includes center)
      - edges:  list of {source, target, relation, weight} dicts
      - files:  list of linked files (deduped across visited concepts)
      - symbols: list of linked symbol qualified names
    """
    start = get_concept(db, name)
    if not start:
        return {"center": None, "nodes": [], "edges": [], "files": [], "symbols": []}

    visited_ids: set[int] = {int(start["id"])}
    frontier: list[int] = [int(start["id"])]
    nodes: list[dict[str, Any]] = [start]
    edges: list[dict[str, Any]] = []

    with db.connect() as conn:
        for _ in range(max(0, depth)):
            if not frontier or len(visited_ids) >= max_nodes:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = conn.execute(
                f"SELECT e.source_id, e.target_id, e.relation, e.weight, "
                f"       cs.name AS s_name, ct.name AS t_name "
                f"FROM concept_edges e "
                f"JOIN concepts cs ON cs.id = e.source_id "
                f"JOIN concepts ct ON ct.id = e.target_id "
                f"WHERE e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders})",
                frontier + frontier,
            ).fetchall()

            next_frontier: list[int] = []
            for r in rows:
                edges.append({
                    "source": r["s_name"],
                    "target": r["t_name"],
                    "relation": r["relation"],
                    "weight": r["weight"],
                })
                for nid in (int(r["source_id"]), int(r["target_id"])):
                    if nid not in visited_ids and len(visited_ids) < max_nodes:
                        visited_ids.add(nid)
                        next_frontier.append(nid)
            frontier = next_frontier

        # Fetch full concept rows for every visited id (except start which we have)
        other_ids = [i for i in visited_ids if i != int(start["id"])]
        if other_ids:
            placeholders = ",".join("?" * len(other_ids))
            other_rows = conn.execute(
                f"SELECT * FROM concepts WHERE id IN ({placeholders})", other_ids
            ).fetchall()
            nodes.extend(dict(r) for r in other_rows)

        # Linked files across all visited concepts
        placeholders = ",".join("?" * len(visited_ids))
        file_rows = conn.execute(
            f"SELECT DISTINCT f.path, cf.weight "
            f"FROM concept_files cf JOIN files f ON f.id = cf.file_id "
            f"WHERE cf.concept_id IN ({placeholders}) "
            f"ORDER BY cf.weight DESC LIMIT 50",
            list(visited_ids),
        ).fetchall()
        files = [dict(r) for r in file_rows]

        # Linked symbols
        sym_rows = conn.execute(
            f"SELECT DISTINCT s.qualified, s.kind, cs.weight "
            f"FROM concept_symbols cs JOIN symbols s ON s.id = cs.symbol_id "
            f"WHERE cs.concept_id IN ({placeholders}) "
            f"ORDER BY cs.weight DESC LIMIT 50",
            list(visited_ids),
        ).fetchall()
        symbols = [dict(r) for r in sym_rows]

    return {
        "center": start,
        "nodes": nodes,
        "edges": edges,
        "files": files,
        "symbols": symbols,
    }


def delete_concept(db: NexusDB, name: str) -> bool:
    """Delete a concept and all its edges/links. Returns True if deleted."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM concepts WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if not row:
            return False
        # ON DELETE CASCADE handles edges and concept_files/symbols.
        conn.execute("DELETE FROM concepts WHERE id = ?", (row["id"],))
    return True


def format_concept_graph(graph: dict[str, Any]) -> str:
    """Render a get_concept_neighbors() result as a readable string."""
    if not graph.get("center"):
        return "Concept not found."

    lines = [f"# {graph['center']['name']} ({graph['center']['kind']})"]
    lines.append(graph["center"]["summary"])
    if graph["center"].get("body"):
        lines.append("")
        lines.append(graph["center"]["body"])

    if graph["edges"]:
        lines.append("")
        lines.append("## Related concepts")
        for e in graph["edges"]:
            lines.append(f"  - {e['source']} --[{e['relation']}]--> {e['target']} (w={e['weight']:.2f})")

    if graph["files"]:
        lines.append("")
        lines.append("## Linked files")
        for f in graph["files"][:10]:
            lines.append(f"  - {f['path']} (w={f['weight']:.2f})")

    if graph["symbols"]:
        lines.append("")
        lines.append("## Linked symbols")
        for s in graph["symbols"][:10]:
            lines.append(f"  - {s['kind']:8s} {s['qualified']}")

    return "\n".join(lines)
