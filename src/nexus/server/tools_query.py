"""Query and retrieval tools: nexus_start, nexus_retrieve, nexus_stats, nexus_analytics."""

from __future__ import annotations

import logging

from nexus.server.state import (
    activate_project,
    check_rate_limit,
    ensure_ranking,
    get_config,
    get_db,
    get_tracker,
)

logger = logging.getLogger("nexus.server.tools")


def register(mcp):
    """Register query tools with the MCP server."""

    @mcp.tool()
    def nexus_start(
        query: str,
        project: str,
        languages: str = "python",
        top_k: int = 15,
        budget: int = 32000,
    ) -> str:
        """Mandatory first call. Scans project (if needed), ranks files by relevance,
        and returns packed context with confidence level.

        Call this at the start of every session with a description of what you're working on.

        Args:
            query: What you're working on (e.g., "fix authentication bug", "add caching layer").
            project: Absolute path to the project root directory.
            languages: Comma-separated languages to index (default: "python").
            top_k: Number of top files to return (default: 15).
            budget: Max characters of context to return (default: 32000).
        """
        check_rate_limit()
        from nexus.index.pipeline import index_project
        from nexus.rank.fusion import compute_confidence, fuse_rankings
        from nexus.rank.packer import format_packed_context, pack_context
        from nexus.session.analytics import log_query_result
        from nexus.session.memory import cleanup_expired, format_decisions, get_active_decisions

        lang_list = [lang.strip() for lang in languages.split(",")]
        config, db = activate_project(project, lang_list)

        # Ensure project is indexed
        stats = db.get_stats()
        if stats["files"] == 0:
            result = index_project(config, db)
            stats = db.get_stats()
            scan_msg = (
                f"Indexed {stats['files']} files, {stats['symbols']} symbols, "
                f"{stats['edges']} edges in {result.duration_ms}ms"
            )
        else:
            scan_msg = f"Using existing index: {stats['files']} files, {stats['symbols']} symbols"

        tracker = get_tracker()
        tracker.log_query(query)

        cleanup_expired(db)

        bm25, pr = ensure_ranking(db)

        # Query BM25
        bm25_results = bm25.query(query, top_k=50)

        # Get PageRank rankings
        pr_file_scores = pr.get_file_scores()
        pr_ranked = sorted(pr_file_scores.items(), key=lambda x: x[1], reverse=True)
        pr_results = [{"file_id": fid, "score": score, "rank": i} for i, (fid, score) in enumerate(pr_ranked)]

        for item in pr_results:
            with db.connect() as conn:
                row = conn.execute("SELECT path FROM files WHERE id = ?", (item["file_id"],)).fetchone()
                item["file_path"] = row["path"] if row else ""

        recency_results = tracker.get_recency_rankings(db) or None

        fused = fuse_rankings(bm25_results, pr_results, recency_results=recency_results, top_k=top_k)
        confidence = compute_confidence(fused)

        packed = pack_context(fused, db, config.root, budget=budget)
        context = format_packed_context(packed)

        result_files = [f["file_path"] for f in fused if "file_path" in f]
        log_query_result(db, query, result_files, confidence, tracker.session_id)

        lines = [
            f"## Nexus Start: {config.name}",
            f"Query: {query}",
            f"Confidence: {confidence}",
            scan_msg,
            "",
        ]

        if confidence == "high":
            lines.append("High confidence: The ranked files likely contain what you need.")
        elif confidence == "medium":
            lines.append("Medium confidence: Consider supplementing with 2-3 targeted grep searches.")
        else:
            lines.append("Low confidence: Results may be incomplete. Use grep/read to explore further.")

        decisions = get_active_decisions(db)
        if decisions:
            lines.append("")
            lines.append(format_decisions(decisions))

        lines.append("")
        lines.append(context)

        logger.info("nexus_start: %s, confidence=%s, files=%d", config.name, confidence, len(fused))
        return "\n".join(lines)

    @mcp.tool()
    def nexus_retrieve(
        query: str,
        top_k: int = 20,
        budget: int = 32000,
    ) -> str:
        """Query the graph for relevant files using BM25 + PageRank + RRF fusion.

        Use this for targeted searches within an already-active project.
        Call nexus_start first to activate a project.

        Args:
            query: Search query (natural language or code identifiers).
            top_k: Number of results (default: 20).
            budget: Max characters of context (default: 32000).
        """
        check_rate_limit()
        from nexus.rank.fusion import compute_confidence, fuse_rankings
        from nexus.rank.packer import format_packed_context, pack_context

        db = get_db()
        config = get_config()
        tracker = get_tracker()
        tracker.log_query(query)

        bm25, pr = ensure_ranking(db)

        bm25_results = bm25.query(query, top_k=50)

        pr_file_scores = pr.get_file_scores()
        pr_ranked = sorted(pr_file_scores.items(), key=lambda x: x[1], reverse=True)
        pr_results = [{"file_id": fid, "score": score, "rank": i} for i, (fid, score) in enumerate(pr_ranked)]

        for item in pr_results:
            with db.connect() as conn:
                row = conn.execute("SELECT path FROM files WHERE id = ?", (item["file_id"],)).fetchone()
                item["file_path"] = row["path"] if row else ""

        recency_results = tracker.get_recency_rankings(db) or None

        fused = fuse_rankings(bm25_results, pr_results, recency_results=recency_results, top_k=top_k)
        confidence = compute_confidence(fused)

        packed = pack_context(fused, db, config.root, budget=budget)
        context = format_packed_context(packed)

        lines = [
            f"## Nexus Retrieve: {query}",
            f"Confidence: {confidence}",
            f"Results: {len(fused)} files",
            "",
            context,
        ]

        return "\n".join(lines)

    @mcp.tool()
    def nexus_stats() -> str:
        """Get statistics about the currently indexed project."""
        check_rate_limit()
        db = get_db()
        config = get_config()
        stats = db.get_stats()

        with db.connect() as conn:
            langs = conn.execute(
                "SELECT language, COUNT(*) as c FROM files WHERE language IS NOT NULL GROUP BY language ORDER BY c DESC"
            ).fetchall()
            unresolved = conn.execute("SELECT COUNT(*) as c FROM unresolved_imports").fetchone()["c"]
            last_scan = conn.execute(
                "SELECT * FROM scan_meta ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

        lines = [
            f"Project: {config.name} ({config.root})",
            f"Files: {stats['files']}",
            f"Symbols: {stats['symbols']}",
            f"Edges: {stats['edges']}",
            f"Unresolved imports: {unresolved}",
        ]

        if langs:
            lines.append("Languages:")
            for lang_row in langs:
                lines.append(f"  {lang_row['language']}: {lang_row['c']} files")

        if last_scan:
            lines.append(f"Last scan: {last_scan['duration_ms']}ms")

        return "\n".join(lines)

    @mcp.tool()
    def nexus_deps(
        path: str = "",
        direction: str = "both",
    ) -> str:
        """Get the dependency map for files in the project or a specific directory.

        For each file, shows what it imports from other modules, what imports it,
        and what it exports (public symbols). Essential for refactoring — tells you
        exactly which cross-references break when you move a file.

        Args:
            path: Directory or file path relative to project root. Empty = entire project.
            direction: "imports" (what this file uses), "importers" (what uses this file), or "both".
        """
        check_rate_limit()
        db = get_db()
        config = get_config()

        with db.connect() as conn:
            # Get files, optionally filtered by directory
            if path:
                files = conn.execute(
                    "SELECT id, path, language FROM files WHERE path LIKE ? ORDER BY path",
                    (path.rstrip("/") + "/%",)
                ).fetchall()
                # Also check if it's an exact file match
                exact = conn.execute(
                    "SELECT id, path, language FROM files WHERE path = ?", (path,)
                ).fetchone()
                if exact and not files:
                    files = [exact]
                elif exact:
                    existing_ids = {f["id"] for f in files}
                    if exact["id"] not in existing_ids:
                        files = [exact] + list(files)
            else:
                files = conn.execute(
                    "SELECT id, path, language FROM files ORDER BY path"
                ).fetchall()

            if not files:
                return f"No files found matching '{path}'"

            lines = [f"## Dependency Map: {path or config.name}", f"Files: {len(files)}", ""]

            for f in files:
                file_id = f["id"]
                file_path = f["path"]

                # Get exports: public symbols defined in this file
                exports = conn.execute(
                    "SELECT name, kind, signature FROM symbols "
                    "WHERE file_id = ? AND visibility = 'public' "
                    "ORDER BY kind, name",
                    (file_id,)
                ).fetchall()

                # Get what this file imports FROM other files (outgoing edges)
                imports_from = conn.execute(
                    """SELECT DISTINCT f2.path, s2.name, e.kind as edge_kind
                    FROM edges e
                    JOIN symbols s1 ON e.source_id = s1.id
                    JOIN symbols s2 ON e.target_id = s2.id
                    JOIN files f2 ON s2.file_id = f2.id
                    WHERE s1.file_id = ? AND s2.file_id != ?
                    AND e.kind IN ('imports', 'references', 'calls')
                    ORDER BY f2.path, s2.name""",
                    (file_id, file_id)
                ).fetchall()

                # Get what imports THIS file (incoming edges)
                imported_by = conn.execute(
                    """SELECT DISTINCT f1.path, s1.name, e.kind as edge_kind
                    FROM edges e
                    JOIN symbols s1 ON e.source_id = s1.id
                    JOIN symbols s2 ON e.target_id = s2.id
                    JOIN files f1 ON s1.file_id = f1.id
                    WHERE s2.file_id = ? AND s1.file_id != ?
                    AND e.kind IN ('imports', 'references', 'calls')
                    ORDER BY f1.path, s1.name""",
                    (file_id, file_id)
                ).fetchall()

                # Get unresolved imports for this file
                unresolved = conn.execute(
                    "SELECT import_path FROM unresolved_imports WHERE file_id = ? ORDER BY import_path",
                    (file_id,)
                ).fetchall()

                # Skip files with no dependencies if there are many files
                if len(files) > 10 and not imports_from and not imported_by and not exports:
                    continue

                lines.append(f"### {file_path}")

                if exports and direction in ("both", "exports"):
                    export_strs = [f"{e['kind']} {e['name']}" for e in exports]
                    lines.append(f"  exports: [{', '.join(export_strs)}]")

                if imports_from and direction in ("both", "imports"):
                    # Group by file
                    by_file: dict[str, list[str]] = {}
                    for row in imports_from:
                        by_file.setdefault(row["path"], []).append(row["name"])
                    imports_str = ", ".join(f"{fp}::{','.join(names)}" for fp, names in by_file.items())
                    lines.append(f"  imports_from: [{imports_str}]")

                if imported_by and direction in ("both", "importers"):
                    # Group by file
                    by_file2: dict[str, list[str]] = {}
                    for row in imported_by:
                        by_file2.setdefault(row["path"], []).append(row["name"])
                    importers_str = ", ".join(f"{fp}" for fp in sorted(by_file2.keys()))
                    lines.append(f"  imported_by: [{importers_str}]")

                if unresolved:
                    lines.append(f"  unresolved: [{', '.join(u['import_path'] for u in unresolved)}]")

                lines.append("")

            # Summary: circular dependency detection
            lines.append("---")

            # Build adjacency for cycle detection
            adj: dict[str, set[str]] = {}
            for f in files:
                file_id = f["id"]
                file_path = f["path"]
                deps = conn.execute(
                    """SELECT DISTINCT f2.path
                    FROM edges e
                    JOIN symbols s1 ON e.source_id = s1.id
                    JOIN symbols s2 ON e.target_id = s2.id
                    JOIN files f2 ON s2.file_id = f2.id
                    WHERE s1.file_id = ? AND s2.file_id != ?
                    AND e.kind IN ('imports', 'references', 'calls')""",
                    (file_id, file_id)
                ).fetchall()
                adj[file_path] = {d["path"] for d in deps}

            # Find mutual dependencies (A->B and B->A)
            cycles = []
            seen_pairs: set[tuple[str, str]] = set()
            for a, deps_a in adj.items():
                for b in deps_a:
                    if b in adj and a in adj[b]:
                        pair = tuple(sorted([a, b]))
                        if pair not in seen_pairs:
                            seen_pairs.add(pair)
                            cycles.append(pair)

            if cycles:
                lines.append(f"Circular dependencies found: {len(cycles)}")
                for a, b in cycles[:20]:
                    lines.append(f"  {a} <-> {b}")
            else:
                lines.append("No circular dependencies detected.")

        return "\n".join(lines)

    @mcp.tool()
    def nexus_analytics(days: int = 30) -> str:
        """View query history analytics -- what Claude asks for most, which files are hot/cold.

        Shows top queries, most accessed files, confidence distribution,
        and files that are indexed but never retrieved (candidates for exclusion).

        Args:
            days: Number of days to analyze (default: 30).
        """
        check_rate_limit()
        from nexus.session.analytics import get_analytics_report, format_analytics_report

        db = get_db()
        report = get_analytics_report(db, days=days)
        return format_analytics_report(report)
