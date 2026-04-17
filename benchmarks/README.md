# Nexus benchmarks

Reproducible measurements of how much context Nexus saves an agent, compared
to the cold-start "grep and read every matching file" baseline that any
plain-shell agent has to do.

## efficiency.py

Token-efficiency benchmark. For each curated query:

1. **Naive baseline** — tokenize the query, grep the whole project for those
   tokens, and count the total size of every matching source file.
2. **Nexus** — run the same BM25 + PageRank + packer pipeline that
   `nexus_start` uses, and count the packed-context size.

The headline is the **reduction %** = how many fewer characters the agent
has to read to get the same relevant context.

### Run it

```bash
# Against the Nexus repo itself
python benchmarks/efficiency.py --project .

# Against any other project
python benchmarks/efficiency.py --project /path/to/repo

# Write markdown + JSON artifacts
python benchmarks/efficiency.py --project . \
    --md benchmarks/results.md \
    --json benchmarks/results.json
```

Use a custom query set (one query per line):

```bash
python benchmarks/efficiency.py --project . --queries my_queries.txt
```

### Default query set

The eight built-in queries cover realistic developer tasks that exercise
different corners of the ranking pipeline:

- BM25 tokenizer fix (boost-weighted identifier search)
- Add a new MCP tool (cross-module search)
- PageRank explanation (single-concept lookup)
- Tree-sitter parser init (entry-point traversal)
- Rate-limit logic (scattered concern)
- Migration 5 schema (narrow keyword, specific file)
- Fastembed integration (optional-dep surface)
- extract_block debugging (fresh feature)

Replace them with anything meaningful to your project; the methodology is
project-agnostic.

### Methodology notes

- The script uses a separate `.nexus/benchmark.db` so it never pollutes the
  live index.
- Nexus' char budget is 16,000 (the default `nexus_start` budget).
- Naive grep is deliberately generous to the baseline: it reads each matching
  file in full, no truncation. A realistic shell agent usually reads even more
  because it often re-reads files across turns.
- "Files" for Nexus counts packed entries, which may be files or individual
  symbols (mixed granularity packer).
- Confidence is reported as `low | medium | high` from
  `compute_confidence(fused)` — it reflects how well-separated the top hits
  are, not their truth value.

### Interpreting results

On the Nexus repo itself (77 Python files, ~572K chars of source) the median
reduction is in the **~93%** range: ~300K chars of naive context collapses
to a ~16K packed context. Queries that match identifiers scattered across
many files (e.g. "MCP tool") reduce the most; narrow keyword queries
(e.g. "migration 5") already have small naive footprints and reduce less.

The number that matters is **consistency**: across a diverse query mix, the
context you'd paste into the agent stays roughly constant (bounded by the
budget) while the naive baseline grows unboundedly with project size.
