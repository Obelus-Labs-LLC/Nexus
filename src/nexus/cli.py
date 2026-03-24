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

    args = parser.parse_args()

    if args.command == "serve":
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


if __name__ == "__main__":
    main()
