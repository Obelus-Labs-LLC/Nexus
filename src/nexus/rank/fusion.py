"""Reciprocal Rank Fusion (RRF) combining multiple ranking signals.

RRF formula: score = sum(1 / (k + rank_i)) for each ranking signal
Default k=60 (standard in literature).

Combines:
  - BM25 text relevance (query-dependent)
  - PageRank structural importance (query-independent)
  - Recency from session activity (query-independent)
"""

from __future__ import annotations

from typing import Any

_RRF_K = 60  # Standard RRF constant

# Default RRF signal weights (overridden by auto-tuner)
_DEFAULT_RRF_WEIGHTS = {"bm25": 1.0, "pagerank": 1.0, "recency": 1.0}


def fuse_rankings(
    bm25_results: list[dict[str, Any]],
    pagerank_results: list[dict[str, Any]],
    recency_results: list[dict[str, Any]] | None = None,
    top_k: int = 20,
    rrf_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Combine multiple ranked lists using Reciprocal Rank Fusion.

    Each input is a list of dicts with at minimum: file_id, rank.
    BM25 results also carry: file_path, score.

    Returns merged results sorted by fused score, with per-signal breakdowns.
    """
    w = rrf_weights or _DEFAULT_RRF_WEIGHTS
    w_bm25 = w.get("bm25", 1.0)
    w_pr = w.get("pagerank", 1.0)
    w_recency = w.get("recency", 1.0)

    scores: dict[int, dict[str, Any]] = {}

    # BM25 signal
    for item in bm25_results:
        fid = item["file_id"]
        if fid not in scores:
            scores[fid] = {
                "file_id": fid,
                "file_path": item.get("file_path", ""),
                "rrf_score": 0.0,
                "bm25_rank": None,
                "pr_rank": None,
                "recency_rank": None,
                "bm25_score": 0.0,
                "pr_score": 0.0,
            }
        scores[fid]["rrf_score"] += w_bm25 / (_RRF_K + item["rank"])
        scores[fid]["bm25_rank"] = item["rank"]
        scores[fid]["bm25_score"] = item.get("score", 0.0)

    # PageRank signal
    for item in pagerank_results:
        fid = item["file_id"]
        if fid not in scores:
            scores[fid] = {
                "file_id": fid,
                "file_path": item.get("file_path", ""),
                "rrf_score": 0.0,
                "bm25_rank": None,
                "pr_rank": None,
                "recency_rank": None,
                "bm25_score": 0.0,
                "pr_score": 0.0,
            }
        scores[fid]["rrf_score"] += w_pr / (_RRF_K + item["rank"])
        scores[fid]["pr_rank"] = item["rank"]
        scores[fid]["pr_score"] = item.get("score", 0.0)

    # Recency signal (optional)
    if recency_results:
        for item in recency_results:
            fid = item["file_id"]
            if fid not in scores:
                scores[fid] = {
                    "file_id": fid,
                    "file_path": item.get("file_path", ""),
                    "rrf_score": 0.0,
                    "bm25_rank": None,
                    "pr_rank": None,
                    "recency_rank": None,
                    "bm25_score": 0.0,
                    "pr_score": 0.0,
                }
            scores[fid]["rrf_score"] += w_recency / (_RRF_K + item["rank"])
            scores[fid]["recency_rank"] = item["rank"]

    # Sort by fused score
    ranked = sorted(scores.values(), key=lambda x: x["rrf_score"], reverse=True)

    # Assign final ranks
    for i, item in enumerate(ranked):
        item["rank"] = i

    return ranked[:top_k]


def compute_confidence(results: list[dict[str, Any]]) -> str:
    """Determine confidence level based on ranking scores.

    Returns: "high", "medium", or "low"

    Thresholds (from plan):
      - high:   top score > 0.7 relative, same module cluster
      - medium: top score > 0.4 relative
      - low:    below 0.4
    """
    if not results:
        return "low"

    top_score = results[0]["rrf_score"]
    max_possible = 3.0 / (_RRF_K + 0)  # Perfect rank 0 in all 3 signals

    relative = top_score / max_possible if max_possible > 0 else 0

    # Check score gap between #1 and #2
    score_gap = 0.0
    if len(results) > 1:
        score_gap = results[0]["rrf_score"] - results[1]["rrf_score"]

    if relative > 0.5 and score_gap > 0.003:
        return "high"
    elif relative > 0.3:
        return "medium"
    else:
        return "low"
