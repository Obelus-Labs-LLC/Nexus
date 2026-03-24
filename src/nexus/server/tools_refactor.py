"""Refactoring and enrichment tools: nexus_rename, nexus_enrich, nexus_cross_project, nexus_remember."""

from __future__ import annotations

import logging
from pathlib import Path

from nexus.server.state import (
    check_rate_limit,
    get_config,
    get_db,
    get_tracker,
    invalidate_ranking,
    _find_nexus_toml,
)

logger = logging.getLogger("nexus.server.tools")


def register(mcp):
    """Register refactoring and enrichment tools with the MCP server."""

    @mcp.tool()
    def nexus_rename(
        symbol: str,
        new_name: str,
        file: str = "",
        line: int = 0,
        col: int = 0,
    ) -> str:
        """Rename a symbol across the entire project (compiler-accurate for Python).

        For Python: uses rope for cross-file rename that handles imports, references,
        and re-exports. Compiler-accurate.

        For Rust/TypeScript/other languages: uses word-boundary text replacement
        across all matching files.

        You can identify the symbol in two ways:
        1. By name: just pass symbol="MyClass" -- Nexus finds the definition.
        2. By location: pass file="path/to/file.py", line=10, col=5.

        Args:
            symbol: Name of the symbol to rename (e.g., "MyClass", "process_data").
            new_name: The new name for the symbol.
            file: (Optional) File path containing the symbol definition.
            line: (Optional) 1-based line number of the symbol.
            col: (Optional) 0-based column offset of the symbol.
        """
        check_rate_limit()
        db = get_db()
        config = get_config()
        tracker = get_tracker()

        from nexus.refactor.rename import rename_by_name_python, rename_python, _rename_by_text

        language = config.languages[0] if config.languages else "python"

        if file and line > 0:
            abs_path = config.root / file
            if language == "python":
                result = rename_python(config.root, abs_path, line, col, new_name)
            else:
                result = _rename_by_text(config.root, file, line, col, new_name, language)
        else:
            if language == "python":
                result = rename_by_name_python(config.root, symbol, new_name, file_hint=file or None)
            else:
                symbols = db.find_symbol_by_name(symbol)
                if not symbols:
                    return f"Symbol '{symbol}' not found in index. Run nexus_scan first."

                sym = symbols[0]
                sym_file = sym.get("file_path", "")
                result = _rename_by_text(
                    config.root, sym_file, sym["line_start"], 0, new_name, language,
                )

        if not result.success:
            return f"Rename failed: {result.error}"

        for changed_file in result.files_changed:
            try:
                rel = str(Path(changed_file).relative_to(config.root))
            except ValueError:
                rel = changed_file
            tracker.log_edit(rel, f"renamed {result.old_name} -> {result.new_name}")

        invalidate_ranking()

        lines = [
            f"Renamed '{result.old_name}' -> '{result.new_name}'",
            f"Files changed: {len(result.files_changed)}",
        ]
        for f in result.files_changed:
            try:
                rel = str(Path(f).relative_to(config.root))
            except ValueError:
                rel = f
            lines.append(f"  - {rel}")

        lines.append("\nNote: Run nexus_register_edit to reindex changed files.")

        logger.info("Renamed %s -> %s in %d files", result.old_name, result.new_name, len(result.files_changed))
        return "\n".join(lines)

    @mcp.tool()
    def nexus_enrich(
        language: str = "",
    ) -> str:
        """Run SCIP indexer for compiler-accurate cross-file references (Layer 2).

        This supplements the tree-sitter parsing with precise definitions and references
        from language servers. Requires the appropriate SCIP indexer to be installed:
          - Python: pip install scip-python
          - Rust: rustup component add rust-analyzer
          - TypeScript: npm install -g @sourcegraph/scip-typescript

        Args:
            language: Language to enrich (default: first configured language).
        """
        check_rate_limit()
        from nexus.index.scip import enrich_with_scip

        db = get_db()
        config = get_config()

        lang = language or (config.languages[0] if config.languages else "python")
        result = enrich_with_scip(config.root, lang, db)

        lines = [f"## SCIP Enrichment: {config.name} ({lang})"]

        if result.errors:
            for e in result.errors:
                lines.append(f"Warning: {e}")

        if result.indexer_used:
            lines.append(f"Indexer: {result.indexer_used}")

        lines.append(f"Cross-file references found: {len(result.references)}")
        lines.append(f"Edges added: {result.edges_added}")

        if result.edges_added > 0:
            invalidate_ranking()

        logger.info("SCIP enrichment: %d references, %d edges", len(result.references), result.edges_added)
        return "\n".join(lines)

    @mcp.tool()
    def nexus_cross_project(
        cluster: str = "",
    ) -> str:
        """Resolve cross-project dependencies within a cluster.

        Finds where projects import from each other (e.g., project_a importing from project_b).
        If no cluster specified, uses the current project's cluster.

        Args:
            cluster: Cluster name (e.g., "obelus", "trading", "civic"). Leave empty to auto-detect.
        """
        check_rate_limit()
        config = get_config()

        if not cluster:
            if config.cluster:
                cluster = config.cluster
            else:
                return f"Project '{config.name}' is not in a cluster. Specify a cluster name."

        from nexus.util.config import load_config
        from nexus.index.cross_project import resolve_cross_project_edges

        registry_path = _find_nexus_toml()
        if not registry_path:
            return "nexus.toml not found. Cross-project resolution requires the project registry."

        projects = load_config(registry_path)
        result = resolve_cross_project_edges(projects, cluster)

        lines = [
            f"## Cross-Project Edges: {cluster}",
            f"Edges found: {result.edges_added}",
            f"Projects linked: {len(result.projects_linked)}",
            f"Duration: {result.duration_ms}ms",
        ]

        if result.projects_linked:
            lines.append("")
            lines.append("Connections:")
            for a, b in sorted(result.projects_linked):
                lines.append(f"  {a} <-> {b}")

        return "\n".join(lines)

    @mcp.tool()
    def nexus_remember(
        content: str,
        type: str = "decision",
        tags: str = "",
    ) -> str:
        """Store a cross-session decision that persists across conversations.

        Use this for decisions, blockers, next steps, or facts that future sessions
        should know about. Entries auto-expire after 7 days.

        Max 20 words per entry. Max 15 active decisions shown at session start.

        Args:
            content: What to remember (max 20 words).
            type: One of: decision, task, next, fact, blocker.
            tags: Comma-separated tags for filtering.
        """
        check_rate_limit()
        from nexus.session.memory import remember, get_active_decisions

        # Validate type
        valid_types = {"decision", "task", "next", "fact", "blocker"}
        if type not in valid_types:
            return f"Invalid type '{type}'. Must be one of: {', '.join(sorted(valid_types))}"

        # Validate content length
        word_count = len(content.split())
        if word_count > 20:
            return f"Content too long ({word_count} words). Max 20 words."

        db = get_db()
        tracker = get_tracker()

        decision_id = remember(
            db=db,
            content=content,
            decision_type=type,
            tags=tags or None,
            session_id=tracker.session_id,
        )

        active = get_active_decisions(db)
        logger.info("Remembered [%s]: %s", type, content)
        return (
            f"Remembered [{type}]: {content}\n"
            f"Active decisions: {len(active)}/15"
        )
