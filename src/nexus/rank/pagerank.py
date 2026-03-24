"""PageRank computation on the symbol graph.

Uses fast-pagerank with scipy sparse matrices. Computes undirected PageRank
(edges treated as bidirectional) with optional personalization toward the
working set (recently accessed files).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from fast_pagerank import pagerank_power
from scipy.sparse import csr_matrix

from nexus.store.db import NexusDB


class NexusPageRank:
    """PageRank scorer for the symbol/file graph."""

    def __init__(self) -> None:
        self._file_scores: dict[int, float] = {}
        self._symbol_scores: dict[int, float] = {}

    @property
    def is_built(self) -> bool:
        return len(self._file_scores) > 0

    def build(self, db: NexusDB, personalization: dict[int, float] | None = None) -> int:
        """Compute PageRank from the edge graph.

        Args:
            db: Database with symbols and edges.
            personalization: Optional dict of file_id -> weight for personalized PageRank.

        Returns:
            Number of nodes in the graph.
        """
        with db.connect() as conn:
            # Get all symbols with their file IDs
            symbols = conn.execute("SELECT id, file_id FROM symbols").fetchall()
            edges = conn.execute("SELECT source_id, target_id FROM edges").fetchall()

        if not symbols or not edges:
            return 0

        # Build node index: symbol_id -> matrix index
        sym_ids = [s["id"] for s in symbols]
        sym_to_file = {s["id"]: s["file_id"] for s in symbols}
        id_to_idx = {sid: i for i, sid in enumerate(sym_ids)}
        n = len(sym_ids)

        # Build adjacency matrix (undirected — add both directions)
        rows, cols, data = [], [], []
        for e in edges:
            src, tgt = e["source_id"], e["target_id"]
            if src in id_to_idx and tgt in id_to_idx:
                si, ti = id_to_idx[src], id_to_idx[tgt]
                rows.extend([si, ti])
                cols.extend([ti, si])
                data.extend([1.0, 1.0])

        if not rows:
            return 0

        adj = csr_matrix((data, (rows, cols)), shape=(n, n))

        # Personalization vector
        personalize = None
        if personalization:
            personalize = np.zeros(n, dtype=np.float64)
            for sym_id, idx in id_to_idx.items():
                file_id = sym_to_file[sym_id]
                if file_id in personalization:
                    personalize[idx] = personalization[file_id]
            total = personalize.sum()
            if total > 0:
                personalize /= total
            else:
                personalize = None

        # Compute PageRank
        pr = pagerank_power(adj, p=0.85, personalize=personalize, tol=1e-6)

        # Store symbol-level scores
        self._symbol_scores = {sym_ids[i]: float(pr[i]) for i in range(n)}

        # Aggregate to file-level scores (sum of symbol PageRanks)
        self._file_scores = {}
        for sym_id, score in self._symbol_scores.items():
            file_id = sym_to_file[sym_id]
            self._file_scores[file_id] = self._file_scores.get(file_id, 0.0) + score

        return n

    def get_file_scores(self) -> dict[int, float]:
        """Get PageRank scores aggregated by file."""
        return dict(self._file_scores)

    def rank_files(self, top_k: int = 50) -> list[dict[str, Any]]:
        """Get files ranked by PageRank score.

        Returns list of dicts with: file_id, score, rank.
        """
        sorted_files = sorted(
            self._file_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

        return [
            {"file_id": fid, "score": score, "rank": rank}
            for rank, (fid, score) in enumerate(sorted_files)
        ]
