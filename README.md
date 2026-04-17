# Nexus

**Semantic codebase graph engine for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).**
Gives Claude instant awareness of your entire codebase via MCP — no manual
context pasting, no stale handoff files.

Nexus scans your projects with tree-sitter, builds a symbol graph, and ranks
files with BM25F + PageRank + RRF fusion. Every Claude Code session starts
with the right code in front of it.

## The headline

Running the built-in token-efficiency benchmark against the Nexus repo
itself:

| Metric | Value |
| --- | ---: |
| **Median context reduction vs. naive grep** | **93.4%** |
| Source corpus | 77 files, 572K chars |
| Queries | 8 realistic dev tasks |
| Avg naive context (grep + read) | 300,625 chars |
| Avg Nexus packed context | 16,043 chars |

Reproduce it:

```bash
python benchmarks/efficiency.py --project .
```

See [`benchmarks/README.md`](benchmarks/README.md) for methodology and how to
run it against any project.

## Features

- **12 languages out of the box** — Python, Rust, TypeScript, JavaScript, C,
  Go, Java, Ruby, PHP, Kotlin, Swift, Zig, Solidity (tree-sitter + plugins)
- **BM25F + PageRank + RRF + recency fusion** — the right files surface first
- **Code-aware tokenizer** — splits `camelCase`, `snake_case`, and
  `PascalCase` so `getUser` matches `get_user_by_id`
- **Compiler-grade lookups for Python** — `nexus_lsp` with
  `definition / references / signatures / infer` via jedi
- **Semantic edits** — `nexus_extract`, `nexus_inline`, `nexus_move` for
  surgical refactors, with diff preview and dry-run by default
- **Concept graph** — `nexus_concept` stores long-lived architectural notes
  alongside the 7-day session memory (`nexus_remember`)
- **Multi-hop exploration** — `nexus_explore` follows the call/import graph
  for agentic deep-dives (RLM pattern)
- **Optional semantic search** — plug in `fastembed` to fuse embedding
  similarity with BM25 + PageRank
- **Cross-project clusters** — trace imports across related repos
- **Incremental reindex** — per-file SHA-256 change detection, not full rescans
- **Zero background processes** — stdio MCP transport, runs only when Claude
  calls a tool
- **SQLite WAL storage** — fast, concurrent, zero-config

## Install

```bash
pip install -e ".[rank]"
```

Optional extras: `[dev]` for pytest and linting, `[embed]` pulls in
`fastembed` for semantic search, `[docstring]` enables
`nexus_docstring` via Claude Haiku.

## Setup

**1. Register as MCP server (one-time):**
```bash
claude mcp add --transport stdio --scope user nexus -- python -m nexus serve
```

**2. Create your project registry:**
```bash
cp nexus.toml.example nexus.toml
# edit nexus.toml with your project paths
```

**3. Scan your projects:**
```bash
python -m nexus scan /path/to/your/project
python -m nexus scan /path/to/rust/project --languages rust
```

**4. Add the Nexus policy to each project's `CLAUDE.md`:**

Copy `templates/NEXUS_CLAUDE_POLICY.md` into the top of each project's
`CLAUDE.md`. This tells Claude to call `nexus_start` first in every session.

That's it. Claude Code now opens every session already knowing your codebase.

## MCP tools (32 total)

### Retrieval
| Tool | Purpose |
| --- | --- |
| `nexus_start` | **Mandatory first call.** Activates project, returns ranked context + session memory |
| `nexus_retrieve` | Targeted search within the active project |
| `nexus_read` | Read a file or specific symbol (`path/file.py::ClassName`) |
| `nexus_symbols` | Search symbols by name across the index |
| `nexus_explore` | Multi-hop graph walk from BM25 seeds (RLM pattern) |
| `nexus_summarize` | Summarize active context with session memory |

### Code intelligence
| Tool | Purpose |
| --- | --- |
| `nexus_lsp` | Compiler-grade `definition / references / signatures / infer` (Python, jedi) |
| `nexus_enrich` | Run SCIP indexer for cross-file compiler-accurate references |
| `nexus_deps` | Dependency map: imports, importers, exports, circular deps |
| `nexus_diff` | Show drift between current files and indexed state |

### Refactoring
| Tool | Purpose |
| --- | --- |
| `nexus_rename` | Cross-file symbol rename (rope for Python, text for others) |
| `nexus_extract` | Hoist a block of lines into a new function with call-site fixup |
| `nexus_inline` | Inline a Python single-return helper at all call sites |
| `nexus_move` | Move a symbol to another file, update imports |
| `nexus_docstring` | Auto-generate docstrings via Claude Haiku |

### Memory
| Tool | Purpose |
| --- | --- |
| `nexus_remember` | Short-lived session decisions (20 words, 7-day TTL) |
| `nexus_concept` | Long-lived concept graph: patterns, conventions, invariants |
| `nexus_feedback` | Record retrieval quality feedback for auto-tuner |

### Project / analytics
| Tool | Purpose |
| --- | --- |
| `nexus_scan` | Full project scan / re-index |
| `nexus_watch` | Auto-reindex on file save (watchdog) |
| `nexus_register_edit` | Re-index files after an edit |
| `nexus_stats` | Project statistics (files, symbols, edges, languages) |
| `nexus_analytics` | Query history, hot/cold files, confidence stats |
| `nexus_cross_project` | Resolve dependencies across clustered projects |

### External integrations
| Tool | Purpose |
| --- | --- |
| `nexus_integrations` | List configured integrations |
| `nexus_security` | OSV vuln scan for dependencies |
| `nexus_vcs` | GitHub status (PRs, issues) |
| `nexus_ci` | GitHub Actions / CircleCI / Travis status |
| `nexus_packages` | npm / PyPI / CDNJS lookup |
| `nexus_news` | Tech news via NewsAPI / GNews |
| `nexus_nlp` | NLP analysis (Datamuse / NLPCloud / WolframAlpha) |
| `nexus_ext_analytics` | Keen IO / Wikidata analytics |

## Architecture

```
src/nexus/
  server/
    mcp.py            — MCP stdio server orchestrator
    state.py          — Shared state, project activation, validation
    tools_index.py    — scan, read, symbols, register_edit, watch
    tools_query.py    — start, retrieve, stats, analytics, deps, explore, ...
    tools_refactor.py — rename, enrich, extract, inline, move, lsp, concept, ...
    tools_integrations.py — security, vcs, ci, packages, news, nlp, ...
  index/
    scanner.py        — File discovery, .gitignore, SHA-256 change detection
    parser.py         — Tree-sitter extractors (12 languages)
    graph.py          — Edge building (contains, imports, calls, references)
    pipeline.py       — scan -> parse -> graph orchestrator
    scip.py           — SCIP integration for compiler-accurate enrichment
    cross_project.py  — Cross-project dependency resolution
    plugins.py        — Parser plugin system (Kotlin, PHP, Ruby, Swift, Zig, Solidity)
  rank/
    bm25.py           — BM25S with BM25F field boosts + code-aware tokenizer
    pagerank.py       — Undirected PageRank via fast-pagerank
    fusion.py         — RRF fusion (BM25 + PageRank + recency + embed)
    packer.py         — Context packing with granularity fallback
    embed.py          — Optional fastembed semantic search (BAAI/bge-small-en)
    explore.py        — Multi-hop graph walk (RLM)
    tuner.py          — Auto-tuning from query analytics
  refactor/
    rename.py         — rope-based symbol rename
    semantic_edit.py  — extract / inline / move primitives
    lsp.py            — jedi-backed goto/refs/signatures/infer
  session/
    tracker.py        — Read/edit/query action logging
    memory.py         — 7-day session memory
    concepts.py       — Long-lived concept graph
    analytics.py      — Query history analysis
  integrations/       — 8 external API modules (stdlib urllib only)
  store/
    db.py             — SQLite WAL + migrations
    schema.sql        — 12 tables
  util/
    identifiers.py    — camelCase/snake_case splitter
    config.py         — nexus.toml registry parser
```

## How ranking works

1. **BM25F** — every file is indexed as a boosted token document: name (3x),
   signature (2x), docstring (1x), body (1x). Identifiers are split into
   sub-tokens so `UserService` matches both `user` and `service`.
2. **PageRank** — scores files by symbol-graph connectivity; popular modules
   rise to the top.
3. **Recency** — files recently read or edited in the current session get a
   fresh-look boost.
4. **Embeddings** (optional) — fastembed vectors fuse semantic similarity
   alongside BM25.
5. **RRF fusion** — combine signals:
   `score = Σ w_s / (60 + rank_s)` across bm25 / pagerank / recency / embed.
6. **Context packer** — knapsack fit into the char budget with granularity
   fallback (full file → signatures → names → path only) and
   lost-in-the-middle ordering (strongest hits at start and end).

## Cross-session memory

Two tiers of persistent knowledge, both survive across Claude sessions:

- **`nexus_remember`** — short-lived decisions with a 7-day TTL, ≤20 words.
  Ideal for "don't forget we decided X" signals.
- **`nexus_concept`** — long-lived concept graph: patterns, conventions,
  architecture notes, invariants. Link concepts with typed relations
  (`related_to`, `depends_on`, `refines`, `implements`, ...) and attach them
  to specific files or symbols. Traversable via BFS.

## Project clusters

Group related projects for cross-project import resolution:

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

Then `nexus_cross_project(cluster="mystack")` shows who imports what.

## Testing

```bash
pip install -e ".[rank,dev]"
pytest tests/ -q
```

187 tests, 1 skipped (the real-fastembed test runs only when `fastembed` is
installed).

## License

MIT

## Author

[Obelus Labs LLC](https://github.com/Obelus-Labs-LLC)
