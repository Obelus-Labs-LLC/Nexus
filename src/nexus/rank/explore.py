"""Multi-hop exploration — RLM-inspired recursive context retrieval.

The Recursive Language Model (CodeRLM, arxiv:2512.24601) works by letting
the model explore code in waves:

  1. Start from a seed set of high-ranking files/symbols.
  2. For each seed, follow graph edges (callers, callees, importers) to a
     neighborhood.
  3. Re-rank the expanded set with BM25 against the query, pulling in
     indirect matches that the first-pass BM25 missed.
  4. Return the unified, ranked expansion — richer than single-hop search.

The function returns a list of ranked files with provenance: which seed
brought them in, at what hop distance, and why (edge kind).

This is strictly a retrieval primitive. The MCP tool `nexus_explore`
wraps it and formats the output for agent consumption.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from nexus.rank.bm25 import NexusBM25
from nexus.store.db import NexusDB


def explore(
    db: NexusDB,
    bm25: NexusBM25,
    query: str,
    seeds: int = 5,
    hops: int = 2,
    max_expanded: int = 30,
) -> dict[str, Any]:
    """Multi-hop exploration starting from top BM25 seeds.

    Args:
        db: Project database.
        bm25: Built BM25 index.
        query: User query.
        seeds: Number of BM25 top files to use as starting points.
        hops: Graph traversal depth.
        max_expanded: Cap on total files returned (safety).

    Returns:
        Dict with keys:
          - query: echoed query
          - seeds: list of {file_path, rank, score} used as starting points
          - expansion: list of {file_path, hop, via, reason, score} for
            every file discovered (including seeds at hop=0)
          - total: len(expansion)
          - edges_followed: count of graph edges traversed
    """
    if hops < 0:
        hops = 0

    # Step 1 — seed BM25 retrieval.
    seed_results = bm25.query(query, top_k=seeds)
    if not seed_results:
        return {
            "query": query,
            "seeds": [],
            "expansion": [],
            "total": 0,
            "edges_followed": 0,
        }

    # Track discovered files with best (lowest-hop, highest-score) entry.
    # Map: file_id -> {"file_path", "hop", "via", "reason", "score"}
    discovered: dict[int, dict[str, Any]] = {}
    edges_followed = 0

    for s in seed_results:
        fid = int(s["file_id"])
        discovered[fid] = {
            "file_id": fid,
            "file_path": s["file_path"],
            "hop": 0,
            "via": None,
            "reason": "bm25_seed",
            "score": float(s["score"]),
        }

    # Step 2 — BFS traversal through symbol-level edges.
    frontier_fids: set[int] = set(discovered)

    for current_hop in range(1, hops + 1):
        if not frontier_fids or len(discovered) >= max_expanded:
            break
        next_frontier: set[int] = set()

        with db.connect() as conn:
            # For every file in the frontier, gather neighboring files via
            # edges connecting their symbols.
            placeholders = ",".join("?" * len(frontier_fids))
            rows = conn.execute(
                f"""
                SELECT DISTINCT
                    src_f.id  AS src_file_id,
                    src_f.path AS src_path,
                    tgt_f.id  AS tgt_file_id,
                    tgt_f.path AS tgt_path,
                    e.kind    AS edge_kind
                FROM edges e
                JOIN symbols src_s ON src_s.id = e.source_id
                JOIN symbols tgt_s ON tgt_s.id = e.target_id
                JOIN files   src_f ON src_f.id = src_s.file_id
                JOIN files   tgt_f ON tgt_f.id = tgt_s.file_id
                WHERE src_f.id IN ({placeholders}) OR tgt_f.id IN ({placeholders})
                """,
                list(frontier_fids) + list(frontier_fids),
            ).fetchall()

        for r in rows:
            edges_followed += 1
            src_fid = int(r["src_file_id"])
            tgt_fid = int(r["tgt_file_id"])

            # Determine which end is a neighbor (i.e. not yet in frontier's file)
            for other_fid, other_path, via_fid in (
                (tgt_fid, r["tgt_path"], src_fid),
                (src_fid, r["src_path"], tgt_fid),
            ):
                if other_fid == via_fid:
                    continue  # self-edge
                if other_fid not in frontier_fids and other_fid not in discovered:
                    if len(discovered) >= max_expanded:
                        break
                    via_path = discovered.get(via_fid, {}).get("file_path", "?")
                    discovered[other_fid] = {
                        "file_id": other_fid,
                        "file_path": other_path,
                        "hop": current_hop,
                        "via": via_path,
                        "reason": f"edge:{r['edge_kind']}",
                        "score": 0.0,  # filled in re-rank step below
                    }
                    next_frontier.add(other_fid)

        frontier_fids = next_frontier

    # Step 3 — re-rank the expanded set with BM25, so indirect discoveries
    # that happen to match the query text rise in the list.
    rerank = bm25.query(query, top_k=max(seeds, max_expanded))
    rerank_scores = {int(r["file_id"]): float(r["score"]) for r in rerank}
    for fid, entry in discovered.items():
        if fid in rerank_scores:
            # Seeds keep their score; expanded files get BM25 if they match,
            # otherwise a small baseline so ordering is stable.
            entry["score"] = max(entry["score"], rerank_scores[fid])

    # Build expansion list: seeds first (by rank), then expanded (by score DESC).
    expansion = list(discovered.values())
    expansion.sort(key=lambda e: (e["hop"], -e["score"], e["file_path"]))
    expansion = expansion[:max_expanded]

    seeds_out = [
        {"file_path": s["file_path"], "rank": s["rank"], "score": float(s["score"])}
        for s in seed_results
    ]

    return {
        "query": query,
        "seeds": seeds_out,
        "expansion": expansion,
        "total": len(expansion),
        "edges_followed": edges_followed,
    }


def format_exploration(result: dict[str, Any]) -> str:
    """Render an explore() result as human-readable text."""
    if not result["seeds"]:
        return f"No BM25 seeds for query: {result['query']!r}"

    lines = [
        f"## Multi-hop exploration: {result['query']!r}",
        f"Seeds: {len(result['seeds'])} | Expanded: {result['total']} files | "
        f"Edges followed: {result['edges_followed']}",
        "",
        "### Seeds (BM25 direct hits)",
    ]
    for s in result["seeds"]:
        lines.append(f"  [seed #{s['rank']}] {s['file_path']} (score={s['score']:.3f})")

    lines.append("")
    lines.append("### Expansion (by hop distance)")
    by_hop: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in result["expansion"]:
        by_hop[e["hop"]].append(e)

    for hop in sorted(by_hop):
        lines.append(f"-- hop {hop} --")
        for e in by_hop[hop]:
            via = f" via {e['via']}" if e.get("via") else ""
            reason = e.get("reason", "")
            score = e.get("score", 0.0)
            lines.append(f"  {e['file_path']}{via}  ({reason}, score={score:.3f})")

    return "\n".join(lines)
