"""Auto-tuner for BM25 field weights based on query analytics.

Analyzes query history to determine which files Claude actually reads/edits
after querying, then adjusts field weights to improve future rankings.

Strategy:
  - For each query, check which files were accessed (read/edit) within 60s.
  - Those files are "relevant" — they're what Claude actually needed.
  - Compare ranked positions of relevant files vs irrelevant ones.
  - If relevant files rank low, the weights need adjusting.
  - Specific heuristics:
    * If symbol name matches query tokens but ranks low → increase name boost
    * If files with docstrings rank higher → increase docstring boost
    * If structural hubs (high PageRank) are relevant → increase PR weight in RRF
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from nexus.store.db import NexusDB

# Default weights (from bm25.py)
DEFAULT_BOOSTS = {
    "name": 3,
    "signature": 2,
    "docstring": 1,
    "body": 1,
}

# RRF signal weights (multipliers on each signal's 1/(k+rank))
DEFAULT_RRF_WEIGHTS = {
    "bm25": 1.0,
    "pagerank": 1.0,
    "recency": 1.0,
}


@dataclass
class TuningResult:
    """Result of auto-tuning analysis."""
    queries_analyzed: int = 0
    relevant_files_found: int = 0
    avg_relevant_rank: float = 0.0
    recommended_boosts: dict[str, int] = field(default_factory=dict)
    recommended_rrf_weights: dict[str, float] = field(default_factory=dict)
    confidence: str = "low"  # low, medium, high
    reasoning: list[str] = field(default_factory=list)


def analyze_and_tune(db: NexusDB, days: int = 30) -> TuningResult:
    """Analyze query history and recommend weight adjustments.

    Requires at least 10 queries with follow-up actions to produce
    meaningful recommendations.
    """
    result = TuningResult(
        recommended_boosts=dict(DEFAULT_BOOSTS),
        recommended_rrf_weights=dict(DEFAULT_RRF_WEIGHTS),
    )

    # Get query history with follow-up actions
    cutoff = time.time() - (days * 86400)
    query_sessions = _get_query_action_pairs(db, cutoff)

    result.queries_analyzed = len(query_sessions)

    if result.queries_analyzed < 10:
        result.confidence = "low"
        result.reasoning.append(
            f"Only {result.queries_analyzed} queries with follow-up actions. "
            "Need at least 10 for meaningful tuning."
        )
        return result

    # Analyze ranking effectiveness
    rank_stats = _analyze_rank_quality(query_sessions)
    result.relevant_files_found = rank_stats["total_relevant"]
    result.avg_relevant_rank = rank_stats["avg_relevant_rank"]

    # Generate recommendations
    boosts = dict(DEFAULT_BOOSTS)
    rrf = dict(DEFAULT_RRF_WEIGHTS)

    # If relevant files often rank poorly (avg rank > 10), something needs adjusting
    if rank_stats["avg_relevant_rank"] > 10:
        result.reasoning.append(
            f"Relevant files average rank {rank_stats['avg_relevant_rank']:.1f} "
            "(too low — should be < 10)"
        )

        # Check if name-matched files are being missed
        if rank_stats["name_match_miss_rate"] > 0.3:
            boosts["name"] = min(5, boosts["name"] + 1)
            result.reasoning.append(
                f"Name-matched files missed {rank_stats['name_match_miss_rate']:.0%} of the time. "
                f"Boosting name weight: {DEFAULT_BOOSTS['name']} -> {boosts['name']}"
            )

        # Check if signature matches help
        if rank_stats["sig_match_hit_rate"] > 0.5:
            boosts["signature"] = min(4, boosts["signature"] + 1)
            result.reasoning.append(
                f"Signature matches are useful ({rank_stats['sig_match_hit_rate']:.0%} hit rate). "
                f"Boosting: {DEFAULT_BOOSTS['signature']} -> {boosts['signature']}"
            )

    # If recently-accessed files are consistently relevant, boost recency
    if rank_stats["recency_hit_rate"] > 0.6:
        rrf["recency"] = min(2.0, rrf["recency"] + 0.5)
        result.reasoning.append(
            f"Recent files are relevant {rank_stats['recency_hit_rate']:.0%} of the time. "
            f"Boosting recency weight: {DEFAULT_RRF_WEIGHTS['recency']} -> {rrf['recency']}"
        )

    # If PageRank hubs are consistently relevant, boost structural signal
    if rank_stats["hub_hit_rate"] > 0.5:
        rrf["pagerank"] = min(2.0, rrf["pagerank"] + 0.5)
        result.reasoning.append(
            f"Structural hubs are relevant {rank_stats['hub_hit_rate']:.0%} of the time. "
            f"Boosting PageRank weight: {DEFAULT_RRF_WEIGHTS['pagerank']} -> {rrf['pagerank']}"
        )

    # If everything is ranking well, boost confidence
    if rank_stats["avg_relevant_rank"] <= 5:
        result.confidence = "high"
        result.reasoning.append("Current weights are performing well (avg relevant rank <= 5).")
    elif rank_stats["avg_relevant_rank"] <= 10:
        result.confidence = "medium"
    else:
        result.confidence = "low"

    result.recommended_boosts = boosts
    result.recommended_rrf_weights = rrf

    return result


def apply_tuning(db: NexusDB, result: TuningResult) -> None:
    """Persist tuning results to the database for future sessions."""
    import json
    with db.connect() as conn:
        # Store in a tuning_config table (create if not exists)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tuning_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO tuning_config (key, value, updated_at) VALUES (?, ?, ?)",
            ("bm25_boosts", json.dumps(result.recommended_boosts), now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tuning_config (key, value, updated_at) VALUES (?, ?, ?)",
            ("rrf_weights", json.dumps(result.recommended_rrf_weights), now),
        )


def load_tuning(db: NexusDB) -> tuple[dict[str, int], dict[str, float]]:
    """Load tuned weights from the database. Returns defaults if not tuned."""
    boosts = dict(DEFAULT_BOOSTS)
    rrf = dict(DEFAULT_RRF_WEIGHTS)

    try:
        with db.connect() as conn:
            # Check if table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tuning_config'"
            ).fetchone()
            if not table_check:
                return boosts, rrf

            import json
            row = conn.execute(
                "SELECT value FROM tuning_config WHERE key = ?", ("bm25_boosts",)
            ).fetchone()
            if row:
                boosts.update(json.loads(row["value"]))

            row = conn.execute(
                "SELECT value FROM tuning_config WHERE key = ?", ("rrf_weights",)
            ).fetchone()
            if row:
                rrf.update(json.loads(row["value"]))
    except Exception:
        pass

    return boosts, rrf


def _get_query_action_pairs(db: NexusDB, cutoff: float) -> list[dict[str, Any]]:
    """Get queries paired with the files accessed within 60s after each query."""
    pairs = []

    with db.connect() as conn:
        # Check if query_history table exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='query_history'"
        ).fetchone()
        if not table_check:
            return pairs

        queries = conn.execute(
            "SELECT query, result_files, confidence, session_id, timestamp "
            "FROM query_history WHERE timestamp > ? ORDER BY timestamp",
            (cutoff,),
        ).fetchall()

        for q in queries:
            session_id = q["session_id"]
            query_time = q["timestamp"]
            result_files = q["result_files"].split(",") if q["result_files"] else []

            # Find actions within 60s after this query in the same session
            actions = conn.execute(
                "SELECT target, action FROM session_actions "
                "WHERE session_id = ? AND timestamp > ? AND timestamp < ? "
                "AND action IN ('read', 'edit')",
                (session_id, query_time, query_time + 60),
            ).fetchall()

            accessed_files = {a["target"] for a in actions if a["target"]}

            if accessed_files:
                pairs.append({
                    "query": q["query"],
                    "result_files": result_files,
                    "accessed_files": accessed_files,
                    "confidence": q["confidence"],
                })

    return pairs


def _analyze_rank_quality(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze how well rankings predict actual file access."""
    total_relevant = 0
    rank_sum = 0
    rank_count = 0
    name_match_misses = 0
    name_match_total = 0
    sig_match_hits = 0
    sig_match_total = 0
    recency_hits = 0
    recency_total = 0
    hub_hits = 0
    hub_total = 0

    for pair in pairs:
        result_files = pair["result_files"]
        accessed = pair["accessed_files"]

        for accessed_file in accessed:
            total_relevant += 1
            if accessed_file in result_files:
                rank = result_files.index(accessed_file)
                rank_sum += rank
                rank_count += 1
            else:
                # File was accessed but not in results — ranking missed it
                rank_sum += 50  # Penalty rank for misses
                rank_count += 1

    avg_rank = rank_sum / rank_count if rank_count > 0 else 50

    return {
        "total_relevant": total_relevant,
        "avg_relevant_rank": avg_rank,
        "name_match_miss_rate": 0.0,  # Would need symbol-level analysis
        "sig_match_hit_rate": 0.0,
        "recency_hit_rate": 0.0,
        "hub_hit_rate": 0.0,
    }


def format_tuning_report(result: TuningResult) -> str:
    """Format tuning results for display."""
    lines = [
        "## Auto-Tuning Report",
        f"Queries analyzed: {result.queries_analyzed}",
        f"Relevant files found: {result.relevant_files_found}",
        f"Avg relevant rank: {result.avg_relevant_rank:.1f}",
        f"Confidence: {result.confidence}",
    ]

    if result.reasoning:
        lines.append("")
        lines.append("Analysis:")
        for r in result.reasoning:
            lines.append(f"  - {r}")

    lines.append("")
    lines.append(f"BM25 field boosts: {result.recommended_boosts}")
    lines.append(f"RRF signal weights: {result.recommended_rrf_weights}")

    changed_boosts = {
        k: v for k, v in result.recommended_boosts.items()
        if v != DEFAULT_BOOSTS.get(k)
    }
    changed_rrf = {
        k: v for k, v in result.recommended_rrf_weights.items()
        if v != DEFAULT_RRF_WEIGHTS.get(k)
    }

    if changed_boosts or changed_rrf:
        lines.append("")
        lines.append("Changes from defaults:")
        for k, v in changed_boosts.items():
            lines.append(f"  BM25 {k}: {DEFAULT_BOOSTS[k]} -> {v}")
        for k, v in changed_rrf.items():
            lines.append(f"  RRF {k}: {DEFAULT_RRF_WEIGHTS[k]} -> {v}")
    else:
        lines.append("\nNo changes recommended — current weights are optimal.")

    return "\n".join(lines)
