"""Indexing and scanning tools: nexus_scan, nexus_read, nexus_symbols, nexus_register_edit."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from nexus.server.state import (
    activate_project,
    check_rate_limit,
    get_config,
    get_db,
    get_tracker,
    invalidate_ranking,
    validate_path,
)

logger = logging.getLogger("nexus.server.tools")


def register(mcp):
    """Register indexing tools with the MCP server."""

    @mcp.tool()
    def nexus_scan(
        project: str,
        force: bool = False,
        languages: str = "python",
    ) -> str:
        """Scan a project directory and build the symbol graph.

        This must be called before other nexus tools. Indexes all source files,
        extracts symbols (functions, classes, methods), and builds the reference graph.

        Args:
            project: Absolute path to the project root directory.
            force: If true, re-indexes all files regardless of changes.
            languages: Comma-separated list of languages to index (default: "python").
        """
        check_rate_limit()
        lang_list = [lang.strip() for lang in languages.split(",")]
        config, db = activate_project(project, lang_list)

        from nexus.index.pipeline import index_project

        result = index_project(config, db, force=force)
        stats = db.get_stats()

        lines = [
            f"Scanned: {config.name} ({config.root})",
            f"Files: {stats['files']} total ({result.scan.files_new} new, "
            f"{result.scan.files_changed} changed, {result.scan.files_unchanged} unchanged)",
            f"Symbols: {stats['symbols']} ({result.symbols_added} added)",
            f"Edges: {stats['edges']} ({result.edges_added} intra-file, {result.imports_resolved} import)",
            f"Duration: {result.duration_ms}ms",
        ]

        if result.scan.errors:
            lines.append(f"Scan errors: {len(result.scan.errors)}")
            for e in result.scan.errors[:5]:
                lines.append(f"  - {e}")

        if result.parse_errors:
            lines.append(f"Parse errors: {len(result.parse_errors)}")
            for e in result.parse_errors[:5]:
                lines.append(f"  - {e}")

        logger.info("Scanned %s: %d files, %d symbols", config.name, stats["files"], stats["symbols"])
        return "\n".join(lines)

    @mcp.tool()
    def nexus_read(
        file: str,
        max_chars: int = 0,
    ) -> str:
        """Read a file or a specific symbol from the indexed project.

        Use 'path/to/file.py' to read a full file.
        Use 'path/to/file.py::ClassName' or 'path/to/file.py::function_name'
        to read a specific symbol's source code.

        Also returns neighboring symbols (imports, callers) for context.

        Args:
            file: Relative file path, optionally with ::symbol suffix.
            max_chars: Maximum characters to return (0 = no limit).
        """
        check_rate_limit()
        db = get_db()
        config = get_config()
        tracker = get_tracker()

        # Parse file::symbol syntax
        symbol_name = None
        if "::" in file:
            file, symbol_name = file.rsplit("::", 1)

        # Validate path stays within project root
        validate_path(file, config)

        # Look up file
        file_info = db.get_file_by_path(file)
        if not file_info:
            return f"File not found in index: {file}\nTry running nexus_scan first."

        file_id = file_info["id"]
        abs_path = config.root / file

        tracker.log_read(file, symbol_name)

        if symbol_name:
            symbols = db.get_symbols_for_file(file_id)
            match = None
            for s in symbols:
                if s["name"] == symbol_name or s["qualified"].endswith(f".{symbol_name}"):
                    match = s
                    break

            if not match:
                available = [s["name"] for s in symbols]
                return f"Symbol '{symbol_name}' not found in {file}.\nAvailable: {', '.join(available)}"

            lines = [
                f"# {match['qualified']} ({match['kind']})",
                f"# Lines {match['line_start']}-{match['line_end']} in {file}",
            ]

            if match["signature"]:
                lines.append(f"# Signature: {match['signature']}")

            if match["body_text"]:
                body = match["body_text"]
                if max_chars and len(body) > max_chars:
                    body = body[:max_chars] + "\n... (truncated)"
                lines.append("")
                lines.append(body)

            neighbors = db.get_neighbors(match["id"])
            if neighbors:
                lines.append("")
                refs = [n for n in neighbors if n["edge_kind"] == "references"]
                structural = [n for n in neighbors if n["edge_kind"] != "references"]

                if structural:
                    lines.append("## Connected symbols:")
                    for n in structural[:10]:
                        lines.append(f"  - {n['edge_kind']}: {n['qualified']} ({n['file_path']})")

                if refs:
                    lines.append("")
                    lines.append("## Cross-file references (SCIP):")
                    for n in refs[:15]:
                        lines.append(f"  - {n['qualified']} ({n['file_path']})")

            return "\n".join(lines)

        else:
            if not abs_path.exists():
                return f"File exists in index but not on disk: {file}"

            try:
                content = abs_path.read_text(errors="replace")
            except Exception as e:
                return f"Error reading {file}: {e}"

            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + "\n... (truncated)"

            symbols = db.get_symbols_for_file(file_id)
            header_lines = [
                f"# {file} ({file_info['language']}, {file_info['line_count']} lines)",
                f"# Symbols: {', '.join(s['name'] for s in symbols)}",
                "",
            ]

            return "\n".join(header_lines) + content

    @mcp.tool()
    def nexus_symbols(
        query: str = "",
        file: str = "",
    ) -> str:
        """Search for symbols in the indexed project.

        Args:
            query: Symbol name to search for (substring match).
            file: Limit search to a specific file path.
        """
        check_rate_limit()
        db = get_db()

        if file:
            config = get_config()
            validate_path(file, config)
            file_info = db.get_file_by_path(file)
            if not file_info:
                return f"File not found: {file}"
            symbols = db.get_symbols_for_file(file_info["id"])
        elif query:
            symbols = db.find_symbol_by_name(query)
        else:
            return "Provide either 'query' or 'file' parameter."

        if not symbols:
            return f"No symbols found for {'file=' + file if file else 'query=' + query}"

        lines = [f"Found {len(symbols)} symbols:"]
        for s in symbols[:50]:
            loc = f"{s.get('file_path', file)}:{s['line_start']}"
            lines.append(f"  {s['kind']:10s} {s['qualified']:50s} {loc}")

        if len(symbols) > 50:
            lines.append(f"  ... and {len(symbols) - 50} more")

        return "\n".join(lines)

    @mcp.tool()
    def nexus_register_edit(
        files: str,
        summary: str = "",
    ) -> str:
        """Notify Nexus that files were edited, triggering incremental reindex.

        Call this after editing files so the index stays current.
        Accepts multiple files as comma-separated paths.

        Args:
            files: Comma-separated relative file paths that were edited.
            summary: Brief description of what changed.
        """
        check_rate_limit()
        db = get_db()
        config = get_config()
        tracker = get_tracker()

        file_list = [f.strip() for f in files.split(",")]
        reindexed = 0

        for rel_path in file_list:
            # Validate path
            try:
                validate_path(rel_path, config)
            except ValueError:
                logger.warning("Skipping path outside project root: %s", rel_path)
                continue

            tracker.log_edit(rel_path, summary)

            abs_path = config.root / rel_path
            if not abs_path.exists():
                continue

            file_info = db.get_file_by_path(rel_path)
            if file_info:
                from nexus.index.parser import parse_file
                from nexus.index.graph import build_intra_file_edges
                from nexus.util.hashing import sha256_file

                file_id = file_info["id"]
                db.clear_file(file_id)

                sha = sha256_file(abs_path)
                stat = abs_path.stat()
                db.upsert_file(
                    path=rel_path,
                    sha256=sha,
                    language=file_info["language"],
                    line_count=sum(1 for _ in open(abs_path, errors="replace")),
                    byte_size=stat.st_size,
                    timestamp=time.time(),
                )

                if file_info["language"]:
                    parsed = parse_file(abs_path, file_info["language"])
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
                    build_intra_file_edges(db, file_id, parsed.symbols)

                reindexed += 1

        invalidate_ranking()

        stats = db.get_stats()
        logger.info("Registered edits: %d files, %d reindexed", len(file_list), reindexed)
        return (
            f"Registered edits: {len(file_list)} files, {reindexed} reindexed\n"
            f"Index: {stats['symbols']} symbols, {stats['edges']} edges"
        )
