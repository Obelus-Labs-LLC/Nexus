<!-- NEXUS: MANDATORY FIRST ACTION -->
<!-- THIS BLOCK MUST BE AT THE TOP OF CLAUDE.MD -->

# IMPORTANT: Nexus Integration (DO NOT SKIP)

**BEFORE doing anything else — before greeting the user, before reading files, before answering questions — you MUST call `nexus_start` with a description of the current project.** This is not optional. This is your first tool call in every session. If the user asks a question before you've called `nexus_start`, call it first, then answer.

Example: `nexus_start(query="your project description here", project="/path/to/your/project")`

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
- **`nexus_deps`** — Dependency map for a directory or file. Shows imports, importers, exports, and circular dependencies. Essential before refactoring.
- **`nexus_analytics`** — View query history, hot/cold files, confidence stats.
- **`nexus_stats`** — Quick project stats (file/symbol/edge counts, languages).
- **`nexus_cross_project`** — Resolve dependencies between projects in the same cluster.
- **`nexus_scan`** — Force a full re-index if something seems stale.

## After Editing Files
Call `nexus_register_edit` with the files you changed and a brief summary. This keeps the index current. Do this every time, not in batches.

## Cross-Session Decisions
When making architectural decisions, discovering blockers, or identifying next steps, call `nexus_remember` (max 20 words). Types: decision, task, next, fact, blocker, **locked**. Future sessions see these automatically. **`locked` entries never expire and are pinned to the top of every session** — use for invariants and do-not-violate rules.

---

## Behavior anchors (Claude 4.7+ mitigations)

These anchors counter regressions observed in Claude 4.7 around literal instruction-following, argumentation, and fabricated side effects. They apply to every session in this project.

1. **Literal instruction-following.** Follow the user's instruction as written. Do not reinterpret "fix the failing test" as "edit the test so it passes" — change the code under test unless the user explicitly says otherwise. If an instruction is ambiguous, ask one clarifying question before acting; do not silently substitute your own interpretation.
2. **No re-litigation.** Once a decision is recorded via `nexus_remember(type="locked")` or `nexus_remember(type="decision")`, treat it as settled. Do not re-argue the decision, propose reversing it, or add "but consider..." caveats unless the user reopens the topic. If you believe a locked decision is wrong, surface the concern once, in one sentence, then comply.
3. **Do not modify tests to make them pass.** Tests are the specification. If a test fails, fix the code, not the test — unless the user asks you to change the test contract or the test itself is demonstrably wrong (state your reasoning and cite the broken assertion).
4. **No silent scope expansion.** Do what was asked, nothing more. If you notice an out-of-scope issue worth fixing, flag it separately (e.g. via `nexus_remember(type="next")`). Do not bundle it into the current change.

## Side-effect verification (no fabricated hashes)

Never report a side effect you did not observe. This applies especially to:

- **Git commits** — never state a commit SHA, branch state, or "pushed to remote" without running the command and reading the actual output. If a `git commit` or `git push` was not executed in this session, do not claim it happened. When you do commit, quote the real SHA from `git rev-parse HEAD` or the `git commit` output; do not paraphrase or invent one.
- **File writes** — after editing a file, the Edit/Write tool's own success response is the verification. Do not claim you "also updated X" unless there is a corresponding tool call in this session.
- **Test/build runs** — only report pass/fail based on actual command output. If tests were not run, say so. Do not narrate hypothetical results.

If you are unsure whether a side effect occurred, run a verification command (`git log -1`, `git status`, `ls`, etc.) and quote the output.

## Thinking-display note

If extended-thinking mode is available in this environment, set `thinking.display: "full"` explicitly so reasoning is visible. Summarized thinking hides the chain of reasoning that makes Claude auditable — prefer full display for any session that writes code, modifies state, or executes commands.
