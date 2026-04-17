"""Token-efficiency benchmark: Nexus vs. naive grep-and-read.

For each curated query, we compare two strategies:

  1. NAIVE  -- tokenize the query, grep the project for those tokens,
               and count the total size of every matching source file.
               This is what a cold agent has to do with only a shell.

  2. NEXUS  -- run `nexus_start(query=..., project=...)` and measure the
               size of the packed context string it returns.

The headline number is the *reduction ratio*:

    reduction % = (naive_chars - nexus_chars) / naive_chars * 100

Run
---
  python benchmarks/efficiency.py --project .
  python benchmarks/efficiency.py --project /path/to/repo --queries benchmarks/queries.txt
  python benchmarks/efficiency.py --project . --json out.json

The script never hits the network and never calls an LLM. Indexing happens
via the normal Nexus pipeline, so results are reproducible.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Ensure `src/` is on sys.path when invoked from the repo root without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── Default query set ──────────────────────────────────────────────────────
# Realistic tasks a developer would hand to an agent. Each one targets a
# different corner of the codebase; collectively they exercise BM25, PageRank,
# symbol boosts, and the important-file promotion.

DEFAULT_QUERIES = [
    "fix the BM25 tokenizer for camelCase identifiers",
    "add a new MCP tool to the query module",
    "how does the PageRank file-ranking work",
    "where is the tree-sitter parser initialised",
    "update rate limiting for tool calls",
    "what SQL schema changes are in migration 5",
    "integrate fastembed optional dependency",
    "debug the semantic extract_block refactoring",
]


@dataclass
class QueryResult:
    query: str
    naive_chars: int
    naive_files: int
    nexus_chars: int
    nexus_files: int
    nexus_confidence: str
    nexus_seconds: float
    reduction_pct: float


@dataclass
class BenchmarkReport:
    project: str
    total_source_chars: int
    total_source_files: int
    query_results: list[QueryResult] = field(default_factory=list)

    @property
    def avg_reduction_pct(self) -> float:
        if not self.query_results:
            return 0.0
        return sum(q.reduction_pct for q in self.query_results) / len(self.query_results)

    @property
    def median_reduction_pct(self) -> float:
        if not self.query_results:
            return 0.0
        vals = sorted(q.reduction_pct for q in self.query_results)
        mid = len(vals) // 2
        if len(vals) % 2 == 1:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2

    @property
    def avg_nexus_chars(self) -> int:
        if not self.query_results:
            return 0
        return sum(q.nexus_chars for q in self.query_results) // len(self.query_results)

    @property
    def avg_naive_chars(self) -> int:
        if not self.query_results:
            return 0
        return sum(q.naive_chars for q in self.query_results) // len(self.query_results)


# ── Source file enumeration ────────────────────────────────────────────────

_SOURCE_EXTENSIONS = {
    ".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".c", ".h",
    ".go", ".java", ".kt", ".kts", ".swift", ".rb", ".php",
    ".zig", ".sol",
}

_SKIP_DIRS = {
    ".git", ".nexus", ".pytest_cache", ".claude", "node_modules",
    "target", "build", "dist", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".tox", "vendor", "third_party",
}


def iter_source_files(root: Path):
    """Yield (relative_path, size_bytes) for every source file under root."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        yield path.relative_to(root), size


# ── Naive strategy ──────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_STOP = frozenset({
    "the", "and", "for", "how", "add", "new", "are", "was", "use",
    "with", "without", "can", "does", "fix", "from", "what", "where",
    "when", "why", "this", "that", "into", "out", "about", "some", "all",
    "any", "not", "but", "its", "has", "have", "had", "did", "doing",
})


def query_tokens(query: str) -> list[str]:
    """Extract informative tokens from a query (strip stopwords, short words)."""
    tokens = [t.lower() for t in _TOKEN_RE.findall(query)]
    return [t for t in tokens if t not in _STOP]


def naive_retrieve(root: Path, query: str) -> tuple[int, int]:
    """Grep the project for query tokens, return (total_chars, file_count).

    Mirrors what a cold agent does: `grep -rn "$token"` per token, union the
    hit files, open each and read it entirely into context.
    """
    tokens = query_tokens(query)
    if not tokens:
        return 0, 0

    hit_files: set[Path] = set()
    for rel, _size in iter_source_files(root):
        path = root / rel
        try:
            # Binary-safe read
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lc = content.lower()
        if any(tok in lc for tok in tokens):
            hit_files.add(path)

    total_chars = 0
    for path in hit_files:
        try:
            total_chars += len(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return total_chars, len(hit_files)


# ── Nexus strategy ──────────────────────────────────────────────────────────

class _NexusHarness:
    """Hold a single BM25+PageRank index and serve multiple queries against it.

    Building these indexes is expensive; a typical Nexus session does it once
    and then runs dozens of queries. The benchmark mirrors that by building
    once per project.
    """

    def __init__(self, root: Path, languages: list[str]):
        from nexus.index.pipeline import index_project
        from nexus.rank.bm25 import NexusBM25
        from nexus.rank.pagerank import NexusPageRank
        from nexus.store.db import NexusDB
        from nexus.util.config import ProjectConfig

        project_abs = root.resolve()
        # Scoped DB so we never pollute the user's real .nexus/nexus.db.
        bench_db_path = project_abs / ".nexus" / "benchmark.db"
        bench_db_path.parent.mkdir(parents=True, exist_ok=True)

        self.config = ProjectConfig(
            name=project_abs.name, root=project_abs, languages=languages
        )
        self.db = NexusDB(bench_db_path)

        if self.db.get_stats()["files"] == 0:
            index_project(self.config, self.db)

        self.bm25 = NexusBM25()
        self.bm25.build(self.db)
        self.pr = NexusPageRank()
        self.pr.build(self.db)

        # Pre-compute PageRank rankings (same for every query).
        pr_file_scores = self.pr._file_scores
        pr_ranked = sorted(pr_file_scores.items(), key=lambda x: x[1], reverse=True)
        self._pr_results: list[dict] = [
            {"file_id": fid, "score": score, "rank": i}
            for i, (fid, score) in enumerate(pr_ranked)
        ]
        with self.db.connect() as conn:
            for item in self._pr_results:
                row = conn.execute(
                    "SELECT path FROM files WHERE id = ?", (item["file_id"],)
                ).fetchone()
                item["file_path"] = row["path"] if row else ""

    def run(self, query: str, budget: int) -> tuple[int, int, str, float]:
        from nexus.rank.fusion import compute_confidence, fuse_rankings
        from nexus.rank.packer import format_packed_context, pack_context

        t0 = time.perf_counter()
        bm25_results = self.bm25.query(query, top_k=50)
        fused = fuse_rankings(bm25_results, self._pr_results, top_k=15)
        confidence = compute_confidence(fused)
        packed = pack_context(fused, self.db, self.config.root, budget=budget)
        context = format_packed_context(packed)
        seconds = time.perf_counter() - t0
        return len(context), len(packed), confidence, seconds


# ── Runner ──────────────────────────────────────────────────────────────────

def run_benchmark(
    root: Path,
    queries: list[str],
    budget: int = 16000,
    languages: list[str] | None = None,
) -> BenchmarkReport:
    total_chars = 0
    total_files = 0
    for _rel, size in iter_source_files(root):
        total_chars += size
        total_files += 1

    report = BenchmarkReport(
        project=str(root.resolve()),
        total_source_chars=total_chars,
        total_source_files=total_files,
    )

    print(f"Indexing {root} ...")
    harness = _NexusHarness(root, languages or ["python"])
    print(
        f"  index ready: {harness.db.get_stats()['files']} files, "
        f"{harness.db.get_stats()['symbols']} symbols\n"
    )

    for i, q in enumerate(queries, start=1):
        print(f"[{i}/{len(queries)}] {q}")
        naive_chars, naive_files = naive_retrieve(root, q)
        nexus_chars, nexus_files, confidence, seconds = harness.run(q, budget=budget)
        if naive_chars > 0:
            reduction = (naive_chars - nexus_chars) / naive_chars * 100.0
        else:
            reduction = 0.0
        qr = QueryResult(
            query=q,
            naive_chars=naive_chars,
            naive_files=naive_files,
            nexus_chars=nexus_chars,
            nexus_files=nexus_files,
            nexus_confidence=confidence,
            nexus_seconds=round(seconds, 3),
            reduction_pct=round(reduction, 1),
        )
        report.query_results.append(qr)
        print(
            f"    naive : {naive_chars:>9,} chars across {naive_files:>3} files\n"
            f"    nexus : {nexus_chars:>9,} chars across {nexus_files:>3} files "
            f"({confidence}, {seconds:.2f}s)\n"
            f"    -> {reduction:.1f}% reduction"
        )

    return report


def format_markdown(report: BenchmarkReport) -> str:
    lines = [
        "# Nexus token-efficiency benchmark",
        "",
        f"**Project:** `{report.project}`",
        f"**Source corpus:** {report.total_source_files:,} files, "
        f"{report.total_source_chars:,} chars",
        f"**Queries:** {len(report.query_results)}",
        "",
        "## Headline",
        "",
        f"- **Median context reduction:** **{report.median_reduction_pct:.1f}%**",
        f"- Average context reduction: {report.avg_reduction_pct:.1f}%",
        f"- Average Nexus context: {report.avg_nexus_chars:,} chars",
        f"- Average naive context: {report.avg_naive_chars:,} chars",
        "",
        "## Per-query results",
        "",
        "| Query | Naive chars | Nexus chars | Reduction | Confidence | Time |",
        "| --- | ---: | ---: | ---: | :---: | ---: |",
    ]
    for q in report.query_results:
        lines.append(
            f"| {q.query} | {q.naive_chars:,} | {q.nexus_chars:,} "
            f"| {q.reduction_pct:.1f}% | {q.nexus_confidence} | {q.nexus_seconds:.2f}s |"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Token-efficiency benchmark for Nexus.")
    p.add_argument("--project", default=".", help="Project root to benchmark (default: .)")
    p.add_argument(
        "--queries",
        help="Optional file with one query per line (default: built-in set).",
    )
    p.add_argument("--budget", type=int, default=16000, help="Char budget per Nexus call.")
    p.add_argument(
        "--languages",
        default="python",
        help="Comma-separated languages to index (default: python).",
    )
    p.add_argument("--json", dest="json_out", help="Write raw results to this JSON file.")
    p.add_argument("--md", dest="md_out", help="Write markdown report to this file.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f"error: project not a directory: {root}", file=sys.stderr)
        return 2

    if args.queries:
        qpath = Path(args.queries)
        queries = [
            ln.strip() for ln in qpath.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
    else:
        queries = DEFAULT_QUERIES

    langs = [l.strip() for l in args.languages.split(",") if l.strip()]
    report = run_benchmark(root, queries, budget=args.budget, languages=langs)

    md = format_markdown(report)
    print()
    print(md)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    **{k: v for k, v in asdict(report).items() if k != "query_results"},
                    "query_results": [asdict(q) for q in report.query_results],
                    "median_reduction_pct": report.median_reduction_pct,
                    "avg_reduction_pct": report.avg_reduction_pct,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
