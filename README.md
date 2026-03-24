# Nexus

Semantic codebase graph engine for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Gives Claude instant awareness of your entire codebase via MCP.

Nexus scans your projects with tree-sitter, builds a symbol graph, ranks files using BM25 + PageRank + RRF fusion, and serves ranked context through MCP tools. Claude Code sessions automatically get the right code context without manual copy-pasting.

## What it replaces

If you're manually managing context across projects with session handoff prompts, CLAUDE.md files stuffed with code summaries, or memory tools like Serena/claude-mem — Nexus replaces all of that with one automated system.

## Features

- **Tree-sitter parsing** for Python, Rust, TypeScript, JavaScript, C, Go, Java
- **BM25F + PageRank + RRF** ranked retrieval — the right files surface first
- **Cross-session memory** — decisions, blockers, and next steps persist across conversations (7-day TTL)
- **Incremental reindexing** — edits trigger per-file re-parse, not full rescans
- **Cross-project dependency resolution** — cluster related projects and trace imports across repos
- **SCIP enrichment** — optional compiler-accurate cross-file references via language servers
- **Plugin system** — extend with custom parsers for additional languages
- **Zero background processes** — stdio transport, runs only when Claude calls a tool
- **SQLite WAL** — fast, concurrent, zero-config storage

## Install

```bash
pip install -e ".[rank]"
```

## Setup

**1. Register as MCP server (one-time):**
```bash
claude mcp add --transport stdio --scope user nexus -- python -m nexus serve
```

**2. Create your project registry:**
```bash
cp nexus.toml.example nexus.toml
# Edit nexus.toml with your project paths
```

**3. Scan your projects:**
```bash
python -m nexus scan /path/to/your/project
python -m nexus scan /path/to/rust/project --languages rust
```

**4. Add the CLAUDE.md policy to your projects:**

Copy `templates/NEXUS_CLAUDE_POLICY.md` into each project's `CLAUDE.md` file. This tells Claude to call `nexus_start` automatically at the beginning of every session.

That's it. Claude Code now has full codebase awareness in every session.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `nexus_start` | **Mandatory first call.** Activates project, returns ranked context + session memory |
| `nexus_retrieve` | Targeted search within an active project |
| `nexus_read` | Read a file or specific symbol (`path/file.py::ClassName`) |
| `nexus_symbols` | Search symbols by name across the index |
| `nexus_register_edit` | Re-index files after edits (keeps index current) |
| `nexus_remember` | Store cross-session decisions (max 20 words, 7-day TTL) |
| `nexus_rename` | Cross-file symbol rename (compiler-accurate for Python via rope) |
| `nexus_enrich` | Run SCIP indexer for compiler-accurate references |
| `nexus_cross_project` | Resolve dependencies across clustered projects |
| `nexus_analytics` | Query history, hot/cold files, confidence stats |
| `nexus_stats` | Project statistics (files, symbols, edges, languages) |
| `nexus_scan` | Full project scan / re-index |

## Architecture

```
src/nexus/
  server/
    mcp.py          — MCP stdio server orchestrator
    state.py        — Shared state, project activation, validation
    tools_index.py  — Scan, read, symbols, register_edit
    tools_query.py  — Start, retrieve, stats, analytics
    tools_refactor.py — Rename, enrich, cross_project, remember
  index/
    scanner.py      — File discovery, .gitignore, SHA-256 change detection
    parser.py       — Tree-sitter extractors (Python, Rust, TS, C, Go, Java)
    graph.py        — Edge building (contains, imports, calls)
    pipeline.py     — Scan -> parse -> graph orchestrator
    scip.py         — SCIP integration for Layer 2 enrichment
    cross_project.py — Cross-project dependency resolution
    plugins.py      — Parser plugin system
  rank/
    bm25.py         — BM25S with BM25F field boosts
    pagerank.py     — Undirected PageRank via fast-pagerank
    fusion.py       — RRF fusion (BM25 + PageRank + recency)
    packer.py       — Context packing with granularity fallback
    tuner.py        — Auto-tuning from query analytics
  session/
    tracker.py      — Read/edit/query action logging
    memory.py       — Cross-session decisions (7-day TTL)
    analytics.py    — Query history analysis
  store/
    db.py           — SQLite WAL persistent connection
    schema.sql      — 7 tables: files, symbols, edges, etc.
  sync/
    porter.py       — Multi-machine database export/import
  util/
    identifiers.py  — camelCase/snake_case splitter for BM25
    config.py       — nexus.toml project registry parser
    hashing.py      — SHA-256 file hashing
```

## How ranking works

1. **BM25F** indexes every symbol with field boosts: name (3x), signature (2x), docstring (1x), body (1x)
2. **PageRank** scores files by connectivity in the symbol graph (more imports = more important)
3. **Recency** boosts files recently read or edited in the current session
4. **RRF fusion** combines all three: `score = 1/(60+bm25_rank) + 1/(60+pr_rank) + 1/(60+recency_rank)`
5. **Context packer** fits results into a budget using knapsack with granularity fallback (full file -> signatures -> names -> path only)
6. **Lost-in-middle ordering** places highest relevance at start and end of the response

## Project clusters

Group related projects into clusters for cross-project dependency resolution:

```toml
[cluster.mystack]
cross_project = true

[cluster.mystack.project.core]
root = "/path/to/core"
languages = ["rust"]

[cluster.mystack.project.api]
root = "/path/to/api"
languages = ["python"]
```

Then call `nexus_cross_project(cluster="mystack")` to see which projects import from which.

## Testing

```bash
pip install -e ".[rank,dev]"
pytest tests/ -v
```

## License

MIT

## Author

[Obelus Labs LLC](https://github.com/Obelus-Labs-LLC)
