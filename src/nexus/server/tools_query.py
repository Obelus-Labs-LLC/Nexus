"""Query and retrieval tools: nexus_start, nexus_retrieve, nexus_stats, nexus_analytics."""

from __future__ import annotations

import logging

from nexus.server.state import (
    activate_project,
    check_rate_limit,
    ensure_ranking,
    get_config,
    get_db,
    get_rrf_weights,
    get_tracker,
    register_active_session,
    reload_tuned_weights,
)

logger = logging.getLogger("nexus.server.tools")

_AUTO_TUNE_INTERVAL = 50  # Trigger auto-tune every N queries


def _maybe_auto_tune(db) -> None:
    """Run analyze_and_tune + apply_tuning if enough query history exists."""
    import time

    try:
        # Check query count since last tune
        with db.connect() as conn:
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='query_history'"
            ).fetchone()
            if not table_check:
                return

            count = conn.execute("SELECT COUNT(*) as c FROM query_history").fetchone()["c"]
            if count == 0 or count % _AUTO_TUNE_INTERVAL != 0:
                return

        from nexus.rank.tuner import analyze_and_tune, apply_tuning
        result = analyze_and_tune(db, days=30)
        if result.confidence in ("medium", "high"):
            apply_tuning(db, result)
            reload_tuned_weights(db)
            logger.info(
                "Auto-tuned: %d queries analyzed, confidence=%s",
                result.queries_analyzed, result.confidence,
            )
    except Exception as e:
        logger.debug("Auto-tune skipped: %s", e)


def _detect_circular_deps(db) -> str:
    """Return a warning string if circular dependencies exist, empty string otherwise."""
    try:
        with db.connect() as conn:
            # Build adjacency from edges
            rows = conn.execute(
                """SELECT DISTINCT f1.path as src, f2.path as dst
                FROM edges e
                JOIN symbols s1 ON e.source_id = s1.id
                JOIN symbols s2 ON e.target_id = s2.id
                JOIN files f1 ON s1.file_id = f1.id
                JOIN files f2 ON s2.file_id = f2.id
                WHERE s1.file_id != s2.file_id
                AND e.kind IN ('imports', 'references', 'calls')"""
            ).fetchall()

        adj: dict[str, set[str]] = {}
        for r in rows:
            adj.setdefault(r["src"], set()).add(r["dst"])

        cycles = []
        seen_pairs: set[tuple[str, str]] = set()
        for a, deps_a in adj.items():
            for b in deps_a:
                if b in adj and a in adj[b]:
                    pair = (min(a, b), max(a, b))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        cycles.append(pair)

        if not cycles:
            return ""

        alert_lines = [f"⚠ Circular dependencies detected ({len(cycles)} pair(s)):"]
        for a, b in cycles[:5]:
            alert_lines.append(f"  {a} <-> {b}")
        if len(cycles) > 5:
            alert_lines.append(f"  ... and {len(cycles) - 5} more (run nexus_deps for full list)")
        return "\n".join(alert_lines)
    except Exception:
        return ""


def _background_checks(project_root, db) -> str:
    """Run silent background checks and return only actionable alerts.

    Checks: OSV vulnerability scan on project deps, GitHub VCS status.
    Results are cached per project (1-hour TTL for security, 15-min for VCS).
    Returns empty string if nothing actionable found.
    """
    import time
    from pathlib import Path

    alerts: list[str] = []
    now = time.time()

    # ── Security: OSV dep scan (no key needed, cached 1h) ────────────────────
    try:
        cache_file = db._db_path.parent / "_bg_security_cache.txt"
        run_security = True
        if cache_file.exists():
            age = now - cache_file.stat().st_mtime
            if age < 3600:  # 1-hour cache
                cached = cache_file.read_text().strip()
                if cached:
                    alerts.append(cached)
                run_security = False

        if run_security:
            from nexus.server.tools_integrations import _extract_dep_names
            dep_names = _extract_dep_names(Path(project_root))
            if dep_names:
                from nexus.integrations.security import osv_check_packages
                pypi_deps = [(n, v) for n, v, eco in dep_names if eco == "PyPI"]
                npm_deps = [(n, v) for n, v, eco in dep_names if eco == "npm"]
                vulns = []
                if pypi_deps:
                    vulns.extend(osv_check_packages(pypi_deps[:20], ecosystem="PyPI"))
                if npm_deps:
                    vulns.extend(osv_check_packages(npm_deps[:20], ecosystem="npm"))

                if vulns:
                    alert = f"⚠ Security: {len(vulns)} known vulnerabilities in dependencies. Run `nexus_security` for details."
                    cache_file.write_text(alert)
                    alerts.append(alert)
                else:
                    cache_file.write_text("")  # Empty = clean, still cached
    except Exception:
        pass

    # ── VCS: GitHub recent activity (cached 15min) ────────────────────────────
    try:
        import os
        if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
            vcs_cache = db._db_path.parent / "_bg_vcs_cache.txt"
            run_vcs = True
            if vcs_cache.exists():
                age = now - vcs_cache.stat().st_mtime
                if age < 900:  # 15-min cache
                    cached = vcs_cache.read_text().strip()
                    if cached:
                        alerts.append(cached)
                    run_vcs = False

            if run_vcs:
                from nexus.integrations.vcs import get_vcs_summary
                summary = get_vcs_summary(Path(project_root))
                vcs_alerts = []

                # Surface failed CI runs
                failed = [
                    r for r in summary.get("github_workflows", [])
                    if r.get("conclusion") in ("failure", "cancelled")
                ]
                if failed:
                    vcs_alerts.append(f"CI: {len(failed)} failed run(s) — {failed[0].get('name', '')}")

                # Surface recent open issues count
                issues = summary.get("github_issues", [])
                if len(issues) >= 5:
                    vcs_alerts.append(f"GitHub: {len(issues)}+ open issues")

                alert_str = " | ".join(vcs_alerts) if vcs_alerts else ""
                vcs_cache.write_text(alert_str)
                if alert_str:
                    alerts.append(f"📡 VCS: {alert_str}")
    except Exception:
        pass

    return "\n".join(alerts)


def register(mcp):
    """Register query tools with the MCP server."""

    @mcp.tool()
    def nexus_start(
        query: str,
        project: str,
        languages: str = "python",
        top_k: int = 15,
        budget: int = 16000,
    ) -> str:
        """Mandatory first call. Scans project (if needed), ranks files by relevance,
        and returns packed context with confidence level.

        Call this at the start of every session with a description of what you're working on.

        Args:
            query: What you're working on (e.g., "fix authentication bug", "add caching layer").
            project: Absolute path to the project root directory.
            languages: Comma-separated languages to index (default: "python").
            top_k: Number of top files to return (default: 15).
            budget: Max characters of context to return (default: 16000).
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

        register_active_session(db, tracker.session_id, config.name)
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
        rrf_weights = get_rrf_weights()

        fused = fuse_rankings(
            bm25_results, pr_results,
            recency_results=recency_results, top_k=top_k,
            rrf_weights=rrf_weights,
        )
        confidence = compute_confidence(fused)

        packed = pack_context(fused, db, config.root, budget=budget)
        context = format_packed_context(packed)

        result_files = [f["file_path"] for f in fused if "file_path" in f]
        log_query_result(db, query, result_files, confidence, tracker.session_id)

        # Auto-trigger tuning every 50 queries when enough data exists
        _maybe_auto_tune(db)

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

        # Inject circular dependency alert if cycles exist in project
        cycle_alert = _detect_circular_deps(db)
        if cycle_alert:
            lines.append("")
            lines.append(cycle_alert)

        # Silent background checks: security vulns + VCS/CI alerts
        bg_alerts = _background_checks(config.root, db)
        if bg_alerts:
            lines.append("")
            lines.append(bg_alerts)

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
        budget: int = 16000,
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
        from nexus.session.analytics import detect_and_log_feedback

        db = get_db()
        config = get_config()
        tracker = get_tracker()
        detect_and_log_feedback(db, tracker.session_id, query)
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
        rrf_weights = get_rrf_weights()

        fused = fuse_rankings(
            bm25_results, pr_results,
            recency_results=recency_results, top_k=top_k,
            rrf_weights=rrf_weights,
        )
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

        # Suggest package vulnerability check if OSV/NVD available
        try:
            from nexus.integrations.base import _INTEGRATION_ENV_MAP
            from nexus.integrations.base import _get_env as _iget
            osv_ready = True  # no key required
            nvd_ready = True  # no key required
            if osv_ready or nvd_ready:
                lines.append("")
                lines.append("Tip: Run `nexus_security` to check dependencies for known CVEs/vulnerabilities.")
        except Exception:
            pass

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

    @mcp.tool()
    def nexus_summarize(
        path: str = "",
        depth: str = "medium",
    ) -> str:
        """Generate a natural-language summary of a file or directory from its symbols and docstrings.

        Useful for understanding unfamiliar code without reading every line.
        Summarizes purpose, public API, key classes/functions, and dependencies.

        Args:
            path: File or directory path relative to project root. Empty = whole project.
            depth: "brief" (one-liners only), "medium" (default), or "detailed" (include signatures).
        """
        check_rate_limit()
        db = get_db()
        config = get_config()

        with db.connect() as conn:
            if path:
                # Try exact file match first
                file_row = conn.execute(
                    "SELECT id, path, language, line_count FROM files WHERE path = ?",
                    (path,),
                ).fetchone()
                if file_row:
                    files = [file_row]
                else:
                    files = conn.execute(
                        "SELECT id, path, language, line_count FROM files "
                        "WHERE path LIKE ? ORDER BY path",
                        (path.rstrip("/") + "/%",),
                    ).fetchall()
            else:
                files = conn.execute(
                    "SELECT id, path, language, line_count FROM files ORDER BY path"
                ).fetchall()

        if not files:
            return f"No files found matching '{path}'"

        lines = [f"## Summary: {path or config.name}", ""]

        for f in files[:30]:
            file_id = f["id"]
            file_path = f["path"]

            with db.connect() as conn:
                syms = conn.execute(
                    "SELECT name, kind, signature, docstring, visibility "
                    "FROM symbols WHERE file_id = ? AND visibility = 'public' "
                    "ORDER BY kind, name",
                    (file_id,),
                ).fetchall()

                imports_count = conn.execute(
                    """SELECT COUNT(DISTINCT f2.id) as c
                    FROM edges e
                    JOIN symbols s1 ON e.source_id = s1.id
                    JOIN symbols s2 ON e.target_id = s2.id
                    JOIN files f2 ON s2.file_id = f2.id
                    WHERE s1.file_id = ? AND s2.file_id != ?""",
                    (file_id, file_id),
                ).fetchone()["c"]

            if not syms and len(files) > 1:
                continue

            classes = [s for s in syms if s["kind"] in ("class", "struct", "enum")]
            functions = [s for s in syms if s["kind"] in ("function", "method") and s["visibility"] == "public"]

            lines.append(f"### {file_path}")
            lines.append(f"  Language: {f['language']} | Lines: {f['line_count']} | Deps: {imports_count}")

            if classes:
                lines.append(f"  Classes: {', '.join(c['name'] for c in classes)}")
                if depth in ("medium", "detailed"):
                    for cls in classes[:5]:
                        doc = (cls["docstring"] or "").split("\n")[0][:100]
                        lines.append(f"    {cls['name']}: {doc}" if doc else f"    {cls['name']}")

            if functions:
                func_names = [fn["name"] for fn in functions[:10]]
                lines.append(f"  Public API: {', '.join(func_names)}")
                if depth == "detailed":
                    for fn in functions[:8]:
                        doc = (fn["docstring"] or "").split("\n")[0][:120]
                        sig = fn["signature"] or ""
                        if doc:
                            lines.append(f"    {fn['name']}({sig}): {doc}")
                        elif sig:
                            lines.append(f"    {fn['name']}({sig})")

            lines.append("")

        if len(files) > 30:
            lines.append(f"... and {len(files) - 30} more files (use a narrower path for detail)")

        return "\n".join(lines)

    @mcp.tool()
    def nexus_feedback(
        query: str,
        found_files: str,
    ) -> str:
        """Provide explicit feedback that the index missed files for a query.

        Call this when nexus_start or nexus_retrieve returned low confidence
        and you found the relevant files manually (via grep, read, etc.).
        This signal is used to improve future ranking for this project.

        Args:
            query: The original query that had poor results.
            found_files: Comma-separated paths of files that were actually relevant.
        """
        check_rate_limit()
        from nexus.session.analytics import log_feedback

        db = get_db()
        tracker = get_tracker()

        file_list = [f.strip() for f in found_files.split(",") if f.strip()]
        if not file_list:
            return "No files provided. Pass comma-separated file paths in found_files."

        log_feedback(
            db=db,
            session_id=tracker.session_id,
            original_query=query,
            original_confidence="unknown",
            manual_files=file_list,
        )

        logger.info("Feedback logged: query=%r, files=%d", query, len(file_list))
        return (
            f"Feedback recorded: {len(file_list)} files marked as relevant for '{query}'.\n"
            "This will improve ranking for future queries on this project."
        )
