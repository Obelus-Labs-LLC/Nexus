"""Query history analytics — tracks what Claude asks for and which files get retrieved.

Used to tune ranking weights and identify gaps in the index.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from nexus.store.db import NexusDB


@dataclass
class QueryStats:
    """Aggregate stats for a query pattern."""
    query: str
    count: int
    last_used: float
    avg_confidence: float


@dataclass
class FileStats:
    """Stats for how often a file is accessed."""
    path: str
    read_count: int
    edit_count: int
    query_hit_count: int  # How often returned in search results
    last_accessed: float
    never_retrieved: bool  # In index but never returned by a query


@dataclass
class AnalyticsReport:
    """Full analytics report for a project."""
    total_queries: int = 0
    total_reads: int = 0
    total_edits: int = 0
    unique_queries: int = 0
    top_queries: list[QueryStats] = field(default_factory=list)
    most_accessed_files: list[FileStats] = field(default_factory=list)
    never_retrieved_files: list[str] = field(default_factory=list)
    confidence_distribution: dict[str, int] = field(default_factory=dict)
    avg_session_actions: float = 0.0


def log_query_result(
    db: NexusDB,
    query: str,
    result_files: list[str],
    confidence: str,
    session_id: str,
) -> None:
    """Log a query and its results for analytics tracking."""
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO query_history
               (query, result_files, confidence, session_id, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (query, ",".join(result_files), confidence, session_id, time.time()),
        )


def get_analytics_report(db: NexusDB, days: int = 30) -> AnalyticsReport:
    """Generate an analytics report for the project."""
    report = AnalyticsReport()
    cutoff = time.time() - (days * 86400)

    with db.connect() as conn:
        # Total action counts
        actions = conn.execute(
            "SELECT action, COUNT(*) as c FROM session_actions "
            "WHERE timestamp > ? GROUP BY action",
            (cutoff,),
        ).fetchall()
        for a in actions:
            if a["action"] == "query":
                report.total_queries = a["c"]
            elif a["action"] == "read":
                report.total_reads = a["c"]
            elif a["action"] == "edit":
                report.total_edits = a["c"]

        # Unique queries
        unique = conn.execute(
            "SELECT COUNT(DISTINCT query) as c FROM query_history WHERE timestamp > ?",
            (cutoff,),
        ).fetchone()
        report.unique_queries = unique["c"] if unique else 0

        # Top queries
        top_q = conn.execute(
            """SELECT query, COUNT(*) as cnt, MAX(timestamp) as last_used,
                      AVG(CASE confidence
                          WHEN 'high' THEN 3
                          WHEN 'medium' THEN 2
                          ELSE 1 END) as avg_conf
               FROM query_history WHERE timestamp > ?
               GROUP BY query ORDER BY cnt DESC LIMIT 20""",
            (cutoff,),
        ).fetchall()
        for q in top_q:
            conf_val = q["avg_conf"] or 1
            conf_label = "high" if conf_val > 2.5 else "medium" if conf_val > 1.5 else "low"
            report.top_queries.append(QueryStats(
                query=q["query"],
                count=q["cnt"],
                last_used=q["last_used"],
                avg_confidence=conf_val,
            ))

        # Confidence distribution
        conf_dist = conn.execute(
            "SELECT confidence, COUNT(*) as c FROM query_history "
            "WHERE timestamp > ? GROUP BY confidence",
            (cutoff,),
        ).fetchall()
        report.confidence_distribution = {c["confidence"]: c["c"] for c in conf_dist}

        # Most accessed files
        file_stats = conn.execute(
            """SELECT target as path,
                      SUM(CASE WHEN action='read' THEN 1 ELSE 0 END) as reads,
                      SUM(CASE WHEN action='edit' THEN 1 ELSE 0 END) as edits,
                      MAX(timestamp) as last_accessed
               FROM session_actions
               WHERE timestamp > ? AND target != ''
               GROUP BY target
               ORDER BY (reads + edits * 2) DESC
               LIMIT 30""",
            (cutoff,),
        ).fetchall()
        for f in file_stats:
            report.most_accessed_files.append(FileStats(
                path=f["path"],
                read_count=f["reads"],
                edit_count=f["edits"],
                query_hit_count=0,  # filled below
                last_accessed=f["last_accessed"],
                never_retrieved=False,
            ))

        # Files in index but never accessed
        all_files = conn.execute("SELECT path FROM files").fetchall()
        accessed_paths = {f["path"] for f in file_stats}
        for f in all_files:
            if f["path"] not in accessed_paths:
                report.never_retrieved_files.append(f["path"])

        # Avg actions per session
        sessions = conn.execute(
            """SELECT session_id, COUNT(*) as c FROM session_actions
               WHERE timestamp > ?
               GROUP BY session_id""",
            (cutoff,),
        ).fetchall()
        if sessions:
            report.avg_session_actions = sum(s["c"] for s in sessions) / len(sessions)

    return report


def format_analytics_report(report: AnalyticsReport) -> str:
    """Format analytics report for display."""
    lines = [
        "## Query Analytics",
        f"Total: {report.total_queries} queries, {report.total_reads} reads, {report.total_edits} edits",
        f"Unique queries: {report.unique_queries}",
        f"Avg actions/session: {report.avg_session_actions:.1f}",
    ]

    if report.confidence_distribution:
        lines.append("")
        lines.append("Confidence distribution:")
        for conf, count in sorted(report.confidence_distribution.items()):
            pct = count / max(report.total_queries, 1) * 100
            lines.append(f"  {conf}: {count} ({pct:.0f}%)")

    if report.top_queries:
        lines.append("")
        lines.append("Top queries:")
        for q in report.top_queries[:10]:
            lines.append(f"  [{q.count}x] {q.query}")

    if report.most_accessed_files:
        lines.append("")
        lines.append("Most accessed files:")
        for f in report.most_accessed_files[:15]:
            lines.append(f"  {f.read_count}R {f.edit_count}E  {f.path}")

    if report.never_retrieved_files:
        lines.append("")
        lines.append(f"Never retrieved: {len(report.never_retrieved_files)} files in index")

    return "\n".join(lines)
