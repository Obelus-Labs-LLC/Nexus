"""CLI entry point for Nexus."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="Semantic codebase graph engine for Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    # nexus serve
    sub.add_parser("serve", help="Start MCP stdio server")

    # nexus scan <project>
    scan = sub.add_parser("scan", help="Scan and index a project")
    scan.add_argument("project", help="Path to the project root")
    scan.add_argument("--force", action="store_true", help="Re-index all files")
    scan.add_argument("--languages", default="python", help="Comma-separated languages (default: python)")

    # nexus stats <project>
    stats = sub.add_parser("stats", help="Show index statistics")
    stats.add_argument("project", help="Path to the project root")

    # nexus dashboard
    dash = sub.add_parser("dashboard", help="Start the web dashboard")
    dash.add_argument("--port", type=int, default=7420, help="Port (default: 7420)")

    # nexus export
    exp = sub.add_parser("export", help="Export session state for multi-machine sync")
    exp.add_argument("output", help="Output file path (.jsonl)")
    exp.add_argument("--machine-id", default="", help="Machine identifier (default: hostname)")

    # nexus import
    imp = sub.add_parser("import", help="Import session state from another machine")
    imp.add_argument("input", help="Input file path (.jsonl)")
    imp.add_argument("--strategy", default="newer_wins",
                     choices=["newer_wins", "source_wins", "skip_existing"],
                     help="Merge strategy (default: newer_wins)")

    # nexus hook  (called by Claude Code PostToolUse hook — reads JSON from stdin)
    sub.add_parser("hook", help="Process a Claude Code PostToolUse hook event (reads JSON from stdin)")

    args = parser.parse_args()

    if args.command == "hook":
        _cmd_hook()
    elif args.command == "serve":
        _cmd_serve()
    elif args.command == "scan":
        _cmd_scan(args)
    elif args.command == "stats":
        _cmd_stats(args)
    elif args.command == "dashboard":
        _cmd_dashboard(args)
    elif args.command == "export":
        _cmd_export(args)
    elif args.command == "import":
        _cmd_import(args)
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_serve() -> None:
    from nexus.server.mcp import run_stdio
    asyncio.run(run_stdio())


def _cmd_scan(args: argparse.Namespace) -> None:
    from nexus.index.pipeline import index_project
    from nexus.store.db import NexusDB
    from nexus.util.config import ProjectConfig

    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    languages = [l.strip() for l in args.languages.split(",")]
    config = ProjectConfig(name=root.name, root=root, languages=languages)
    db = NexusDB(config.db_path)

    print(f"Scanning {root}...")
    result = index_project(config, db, force=args.force)
    stats = db.get_stats()

    print(f"Files:   {stats['files']} ({result.scan.files_new} new, {result.scan.files_changed} changed)")
    print(f"Symbols: {stats['symbols']} ({result.symbols_added} added)")
    print(f"Edges:   {stats['edges']}")
    print(f"Time:    {result.duration_ms}ms")

    if result.parse_errors:
        print(f"\nParse errors ({len(result.parse_errors)}):")
        for e in result.parse_errors[:10]:
            print(f"  {e}")


def _cmd_stats(args: argparse.Namespace) -> None:
    from nexus.store.db import NexusDB
    from nexus.util.config import ProjectConfig

    root = Path(args.project).resolve()
    config = ProjectConfig(name=root.name, root=root)
    db_path = config.db_path

    if not db_path.exists():
        print(f"No index found at {db_path}. Run 'nexus scan' first.")
        sys.exit(1)

    db = NexusDB(db_path)
    stats = db.get_stats()

    print(f"Project: {root.name}")
    print(f"Files:   {stats['files']}")
    print(f"Symbols: {stats['symbols']}")
    print(f"Edges:   {stats['edges']}")

    with db.connect() as conn:
        langs = conn.execute(
            "SELECT language, COUNT(*) as c FROM files WHERE language IS NOT NULL GROUP BY language"
        ).fetchall()
        for l in langs:
            print(f"  {l['language']}: {l['c']} files")

        unresolved = conn.execute("SELECT COUNT(*) as c FROM unresolved_imports").fetchone()["c"]
        print(f"Unresolved imports: {unresolved}")


def _cmd_dashboard(args: argparse.Namespace) -> None:
    from nexus.dashboard.api import serve
    serve(port=args.port)


def _cmd_export(args: argparse.Namespace) -> None:
    from nexus.sync.porter import export_state

    config_path = Path(__file__).parent.parent.parent / "nexus.toml"
    if not config_path.exists():
        print("Error: nexus.toml not found", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    manifest = export_state(config_path, output, machine_id=args.machine_id)

    print(f"Exported to {output}")
    print(f"Machine: {manifest.machine_id}")
    print(f"Projects: {len(manifest.projects)}")
    print(f"Decisions: {manifest.decisions_count}")
    print(f"Actions: {manifest.actions_count}")
    print(f"Queries: {manifest.queries_count}")


def _cmd_import(args: argparse.Namespace) -> None:
    from nexus.sync.porter import import_state

    config_path = Path(__file__).parent.parent.parent / "nexus.toml"
    if not config_path.exists():
        print("Error: nexus.toml not found", file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    counts = import_state(config_path, input_path, merge_strategy=args.strategy)

    print(f"Imported from {input_path}")
    print(f"Strategy: {args.strategy}")
    print(f"Decisions: {counts['decisions']}")
    print(f"Actions: {counts['actions']}")
    print(f"Queries: {counts['queries']}")


_HOOK_DEBOUNCE_MS = 5000  # Minimum 5 seconds between hook-triggered reindexes


def _cmd_hook() -> None:
    """Process a Claude Code PostToolUse hook event.

    Claude Code passes the hook payload as JSON on stdin. We extract the edited
    file path and incrementally reindex the project that contains it.

    Only triggers for Write, Edit, and MultiEdit tools. Exits silently (code 0)
    for anything else so the hook never blocks Claude.

    Debounced: if another hook ran within the last 5 seconds for the same
    project, this invocation exits immediately to avoid redundant reindexing.
    """
    import json
    import time

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)

    file_path_str = data.get("tool_input", {}).get("file_path", "")
    if not file_path_str:
        sys.exit(0)

    file_path = Path(file_path_str).resolve()

    # Walk up from the edited file to find the nearest project root (.nexus dir).
    # Use the scanner guard to reject candidates that are user home / system dirs,
    # otherwise an edit outside any real project climbs up to ~ and the hook
    # happily scans the entire user profile (this actually happened — 1.84 GB
    # of garbage before the guard existed).
    from nexus.index.scanner import _validate_project_root

    project_root: Path | None = None
    for candidate in [file_path.parent, *file_path.parent.parents]:
        if not (candidate / ".nexus").is_dir():
            continue
        try:
            _validate_project_root(candidate)
        except ValueError:
            continue  # user home or system dir — skip and keep walking up
        project_root = candidate
        break

    if project_root is None:
        sys.exit(0)

    # Debounce: skip if another hook reindexed this project recently
    nexus_dir = project_root / ".nexus"
    lock_file = nexus_dir / "_hook_lock"
    try:
        if lock_file.exists():
            age_ms = (time.time() - lock_file.stat().st_mtime) * 1000
            if age_ms < _HOOK_DEBOUNCE_MS:
                sys.exit(0)  # Another hook is handling it
        lock_file.write_text(str(time.time()))
    except Exception:
        pass  # Don't block on lock file errors

    try:
        from nexus.index.pipeline import index_project
        from nexus.store.db import NexusDB
        from nexus.util.config import ProjectConfig

        config = ProjectConfig(name=project_root.name, root=project_root)
        db = NexusDB(config.db_path)
        # force=False: only reindexes files whose SHA-256 changed
        index_project(config, db, force=False)
    except Exception:
        pass  # Never block Claude due to indexing errors

    sys.exit(0)


if __name__ == "__main__":
    main()
