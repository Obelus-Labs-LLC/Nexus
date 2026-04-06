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

# Docstring generation budget: hard cap per MCP server session
MAX_DOCSTRING_CALLS_PER_SESSION = 40
_docstring_calls_this_session = 0


def _insert_docstring(file_path: Path, def_line: int, kind: str, docstring: str) -> bool:
    """Insert a docstring into a Python source file after a def/class line.

    Finds the colon that ends the function/class signature (handles multi-line sigs),
    then inserts the docstring as the first statement in the body.

    Returns True if the file was modified.
    """
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if not lines or def_line < 1 or def_line > len(lines):
            return False

        # Find the line where the body starts (after the trailing colon)
        body_start = def_line - 1  # 0-indexed
        while body_start < len(lines) and ":" not in lines[body_start]:
            body_start += 1
        if body_start >= len(lines):
            return False

        body_line_idx = body_start + 1  # First line of body

        # Detect indentation from the def line
        def_text = lines[def_line - 1]
        indent = len(def_text) - len(def_text.lstrip())
        body_indent = " " * (indent + 4)

        # Check if docstring already exists
        if body_line_idx < len(lines):
            stripped = lines[body_line_idx].strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                return False  # Already has docstring

        # Build docstring lines
        if "\n" in docstring or len(docstring) > 88:
            doc_lines = [
                f'{body_indent}"""{docstring}\n',
                f'{body_indent}"""\n',
            ]
        else:
            doc_lines = [f'{body_indent}"""{docstring}"""\n']

        # Insert
        lines[body_line_idx:body_line_idx] = doc_lines
        file_path.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


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
            f"Projects skipped (unchanged imports): {result.projects_skipped}",
            f"Duration: {result.duration_ms}ms",
        ]

        if result.projects_linked:
            lines.append("")
            lines.append("Connections:")
            for a, b in sorted(result.projects_linked):
                lines.append(f"  {a} <-> {b}")

        return "\n".join(lines)

    @mcp.tool()
    def nexus_diff(
        ref: str = "HEAD~1",
        show_dependents: bool = True,
    ) -> str:
        """Show files changed since a git ref and their downstream dependents in the graph.

        Answers the question: "If I changed these files, what else could break?"
        Runs git diff to get changed files, then traces the dependency graph to find
        all files that import or reference those changed files.

        Args:
            ref: Git ref to diff against (default: "HEAD~1", the last commit).
                 Examples: "main", "HEAD~3", a commit SHA.
            show_dependents: If True, trace downstream dependents (default: True).
        """
        check_rate_limit()
        import subprocess

        db = get_db()
        config = get_config()

        # Run git diff to get changed files
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", ref, "HEAD"],
                cwd=str(config.root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return f"git diff failed: {result.stderr.strip()}"
        except FileNotFoundError:
            return "git not found. nexus_diff requires git in PATH."
        except subprocess.TimeoutExpired:
            return "git diff timed out."

        changed_rel = [
            line.strip() for line in result.stdout.splitlines()
            if line.strip()
        ]

        if not changed_rel:
            return f"No files changed between {ref} and HEAD."

        # Resolve to files in the index
        indexed_changed = []
        not_indexed = []
        for rel in changed_rel:
            f = db.get_file_by_path(rel)
            if f:
                indexed_changed.append((rel, f["id"]))
            else:
                not_indexed.append(rel)

        lines = [
            f"## nexus_diff: {ref} → HEAD",
            f"Changed files: {len(changed_rel)} ({len(indexed_changed)} indexed)",
            "",
        ]

        for rel in changed_rel:
            marker = "📄" if any(r == rel for r, _ in indexed_changed) else "·"
            lines.append(f"  {marker} {rel}")

        if not_indexed:
            lines.append(f"\n  (+ {len(not_indexed)} files not in index)")

        if not show_dependents or not indexed_changed:
            return "\n".join(lines)

        # Trace dependents: find all files that import changed files
        lines.append("\n## Downstream Dependents")

        all_dependents: dict[str, set[str]] = {}  # changed_file -> {dependent_files}

        with db.connect() as conn:
            for rel, file_id in indexed_changed:
                deps = conn.execute(
                    """SELECT DISTINCT f1.path
                    FROM edges e
                    JOIN symbols s1 ON e.source_id = s1.id
                    JOIN symbols s2 ON e.target_id = s2.id
                    JOIN files f1 ON s1.file_id = f1.id
                    WHERE s2.file_id = ? AND s1.file_id != ?
                    AND e.kind IN ('imports', 'references', 'calls')""",
                    (file_id, file_id),
                ).fetchall()
                dep_paths = {d["path"] for d in deps}
                # Exclude files that are themselves changed
                changed_paths = {r for r, _ in indexed_changed}
                dep_paths -= changed_paths
                if dep_paths:
                    all_dependents[rel] = dep_paths

        if not all_dependents:
            lines.append("  No dependents found in index.")
        else:
            total_unique = len(set().union(*all_dependents.values()))
            lines.append(f"  {total_unique} unique file(s) may be affected:\n")
            for changed_file, dependents in sorted(all_dependents.items()):
                lines.append(f"  {changed_file}")
                for dep in sorted(dependents)[:10]:
                    lines.append(f"    ← {dep}")
                if len(dependents) > 10:
                    lines.append(f"    ← ... and {len(dependents) - 10} more")

        logger.info("nexus_diff: %d changed, %d with dependents", len(changed_rel), len(all_dependents))
        return "\n".join(lines)

    @mcp.tool()
    def nexus_docstring(
        path: str = "",
        limit: int = 20,
        dry_run: bool = False,
    ) -> str:
        """Auto-generate docstrings for undocumented Python symbols using Claude Haiku.

        Finds public functions and classes with no docstring, calls Claude Haiku
        (claude-haiku-4-5-20251001) with the symbol's signature and body, and writes
        the generated docstring back into the source file.

        Requires the 'anthropic' package: pip install anthropic
        Requires ANTHROPIC_API_KEY environment variable.

        Args:
            path: File or directory to process (relative to project root). Empty = whole project.
            limit: Max number of symbols to process per call (default: 20).
            dry_run: If True, shows what would be generated without writing to files.
        """
        check_rate_limit()
        import os

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return (
                "ANTHROPIC_API_KEY not set. Export it before using nexus_docstring.\n"
                "Example: set ANTHROPIC_API_KEY=sk-ant-..."
            )

        try:
            import anthropic
        except ImportError:
            return (
                "anthropic package not installed. Run: pip install anthropic\n"
                "Then re-run nexus_docstring."
            )

        db = get_db()
        config = get_config()
        tracker = get_tracker()

        # Find undocumented public symbols
        with db.connect() as conn:
            if path:
                rows = conn.execute(
                    """SELECT s.id, s.name, s.qualified, s.kind, s.signature, s.body_text,
                              s.line_start, f.path as file_path, f.language
                       FROM symbols s JOIN files f ON s.file_id = f.id
                       WHERE (f.path = ? OR f.path LIKE ?)
                       AND s.docstring IS NULL AND s.visibility = 'public'
                       AND s.kind IN ('function', 'class', 'method')
                       AND f.language = 'python'
                       ORDER BY f.path, s.line_start
                       LIMIT ?""",
                    (path, path.rstrip("/") + "/%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT s.id, s.name, s.qualified, s.kind, s.signature, s.body_text,
                              s.line_start, f.path as file_path, f.language
                       FROM symbols s JOIN files f ON s.file_id = f.id
                       WHERE s.docstring IS NULL AND s.visibility = 'public'
                       AND s.kind IN ('function', 'class', 'method')
                       AND f.language = 'python'
                       ORDER BY f.path, s.line_start
                       LIMIT ?""",
                    (limit,),
                ).fetchall()

        if not rows:
            return f"No undocumented public symbols found{' in ' + path if path else ''}."

        # Session-wide budget: cap total Anthropic API calls across invocations
        global _docstring_calls_this_session
        remaining_budget = MAX_DOCSTRING_CALLS_PER_SESSION - _docstring_calls_this_session
        if remaining_budget <= 0:
            return (
                f"Session docstring budget exhausted ({MAX_DOCSTRING_CALLS_PER_SESSION} calls). "
                "Start a new session to generate more docstrings."
            )

        effective_limit = min(len(rows), remaining_budget)
        if effective_limit < len(rows):
            logger.info("Docstring budget: %d remaining, capping at %d (of %d found)",
                        remaining_budget, effective_limit, len(rows))

        client = anthropic.Anthropic(api_key=api_key)
        processed = 0
        skipped = 0
        results = []

        for sym in rows[:effective_limit]:
            sig = sym["signature"] or sym["name"]
            body = (sym["body_text"] or "")[:600]  # Limit body to avoid huge prompts

            prompt = (
                f"Write a concise, accurate Python docstring for this {sym['kind']}.\n"
                f"Return ONLY the docstring text (no quotes, no def line).\n"
                f"One sentence for simple functions, multi-line for complex ones.\n\n"
                f"Signature: {sig}\n"
                f"Body:\n{body}"
            )

            try:
                # Throttle: minimum 1.5s between Anthropic API calls
                import time as _time
                _time.sleep(1.5)

                message = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                _docstring_calls_this_session += 1
                docstring = message.content[0].text.strip().strip('"\'')

                if not docstring:
                    skipped += 1
                    continue

                result_line = f"  {sym['qualified']}: {docstring[:80]}{'...' if len(docstring) > 80 else ''}"
                results.append(result_line)

                if not dry_run:
                    # Write docstring into the source file
                    abs_path = config.root / sym["file_path"]
                    if abs_path.exists() and abs_path.suffix == ".py":
                        _insert_docstring(abs_path, sym["line_start"], sym["kind"], docstring)
                        tracker.log_edit(sym["file_path"], f"added docstring to {sym['name']}")

                processed += 1

            except Exception as e:
                skipped += 1
                _docstring_calls_this_session += 1  # Count failed attempts too
                logger.warning("Docstring generation failed for %s: %s", sym["qualified"], e)

        budget_remaining = MAX_DOCSTRING_CALLS_PER_SESSION - _docstring_calls_this_session
        lines = [
            f"## nexus_docstring{'(dry run)' if dry_run else ''}",
            f"Processed: {processed} symbols | Skipped: {skipped}",
            f"Total undocumented: {len(rows)}",
            f"API budget remaining: {budget_remaining}/{MAX_DOCSTRING_CALLS_PER_SESSION} calls this session",
            "",
        ]

        if results:
            lines.append("Generated docstrings:")
            lines.extend(results)

        if not dry_run and processed > 0:
            lines.append("\nRun nexus_register_edit to update the index with new docstrings.")

        logger.info("nexus_docstring: %d generated, %d skipped", processed, skipped)
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
