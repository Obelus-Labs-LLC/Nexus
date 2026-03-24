<!-- NEXUS: MANDATORY FIRST ACTION -->
<!-- THIS BLOCK MUST BE AT THE TOP OF CLAUDE.MD -->

<!-- NEXUS: MANDATORY FIRST ACTION -->
<!-- THIS BLOCK MUST BE AT THE TOP OF CLAUDE.MD -->

# IMPORTANT: Nexus Integration (DO NOT SKIP)

**BEFORE doing anything else — before greeting the user, before reading files, before answering questions — you MUST call `nexus_start` with a description of the current project.** This is not optional. This is your first tool call in every session. If the user asks a question before you've called `nexus_start`, call it first, then answer.

Example: `nexus_start(query="semantic codebase graph engine", project="/path/to/Nexus")`

## After nexus_start
- **high confidence**: Use the returned context directly. No extra searching needed.
- **medium confidence**: Supplement with 2-3 targeted `nexus_retrieve` searches.
- **low confidence**: Results may be incomplete. Use `nexus_retrieve` with different queries, then fall back to grep.

## Nexus Tools — USE THESE FIRST

You have Nexus tools available. **Always prefer Nexus tools over built-in alternatives** when working in an indexed project:

| Instead of... | Use this Nexus tool | Why |
|---|---|---|
| Grep for symbol names | `nexus_symbols(query="name")` | Searches the symbol index, faster and more precise |
| Reading a file to find a function | `nexus_read(file="path::FunctionName")` | Returns just that symbol + its connections |
| Grep for "where is X used" | `nexus_retrieve(query="X usage")` | BM25+PageRank ranked results, not raw grep |
| Manual context gathering | `nexus_start` already did this | Context was loaded at session start |

### Full Tool Reference

- **`nexus_start`** — MANDATORY first call. Returns ranked context + cross-session decisions.
- **`nexus_retrieve`** — Targeted search within the project. Use for follow-up queries when you need context on a different topic than the initial `nexus_start` query.
- **`nexus_read`** — Read a file or specific symbol. Use `path/file.py::ClassName` syntax to read just one symbol and its graph neighbors.
- **`nexus_symbols`** — Search for symbols by name. Returns kind, location, qualified name. Faster than grepping for definitions.
- **`nexus_register_edit`** — Call after every file edit. Pass comma-separated file paths and a summary. Keeps the index current.
- **`nexus_remember`** — Store a decision, blocker, task, or fact for future sessions (max 20 words, 7-day TTL).
- **`nexus_rename`** — Cross-file symbol rename. Compiler-accurate for Python (uses rope), text-based for other languages.
- **`nexus_enrich`** — Run SCIP indexer for compiler-accurate cross-file references (requires scip-python/rust-analyzer/scip-typescript installed).
- **`nexus_analytics`** — View query history, hot/cold files, confidence stats.
- **`nexus_stats`** — Quick project stats (file/symbol/edge counts, languages).
- **`nexus_cross_project`** — Resolve dependencies between projects in the same cluster.
- **`nexus_scan`** — Force a full re-index if something seems stale.

## After Editing Files
Call `nexus_register_edit` with the files you changed and a brief summary. This keeps the index current. Do this every time, not in batches.

## Cross-Session Decisions
When making architectural decisions, discovering blockers, or identifying next steps, call `nexus_remember` (max 20 words). Types: decision, task, next, fact, blocker. Future sessions see these automatically.


# Nexus — Semantic Codebase Graph Engine

## What This Is
Nexus is an MCP server that gives Claude instant codebase awareness. It scans projects, builds a symbol graph with tree-sitter, ranks files using BM25+PageRank+RRF fusion, and serves ranked context via MCP tools.

## Architecture
```
src/nexus/
  server/mcp.py    — MCP stdio server, 8 tools
  index/scanner.py — File discovery, .gitignore, SHA-256 change detection
  index/parser.py  — Tree-sitter extractors (Python, Rust, TS, C)
  index/graph.py   — Edge building (contains, imports)
  index/pipeline.py— Scan → parse → graph orchestrator
  rank/bm25.py     — BM25S with BM25F field boosts
  rank/pagerank.py — Undirected PageRank via fast-pagerank
  rank/fusion.py   — RRF fusion (BM25 + PageRank + recency)
  rank/packer.py   — Context packing with granularity fallback
  session/tracker.py — Read/edit/query action logging
  session/memory.py  — Cross-session decisions (7-day TTL)
  store/db.py      — SQLite WAL persistent connection
  store/schema.sql — 7 tables: files, symbols, edges, etc.
  util/identifiers.py — camelCase/snake_case splitter for BM25
  util/config.py   — nexus.toml project registry parser
```

## Key Patterns
- SQLite WAL mode with persistent connection (not per-call)
- BM25F via token repetition: name 3x, signature 2x, docstring 1x, body 1x
- RRF: score = 1/(60+bm25_rank) + 1/(60+pr_rank) + 1/(60+recency_rank)
- Context packer: full → signatures → names → path (knapsack by value/weight)
- Lost-in-middle ordering: highest relevance at start and end of context

## Testing
- Run tests: `pytest tests/`
- Quick scan: `python -m nexus scan <project_path>`
