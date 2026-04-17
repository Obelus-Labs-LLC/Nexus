"""Optional embeddings fallback — semantic search tier (MegaMemory-inspired).

When BM25 produces low-confidence results (few/weak matches), a semantic
embedding search can rescue queries that use synonyms or paraphrases.
This module is strictly OPTIONAL — installs pull in `fastembed` which is
heavy (~80MB ONNX models). If `fastembed` is not installed, the module
returns `is_available() == False` and nexus_retrieve falls back to
pure BM25 + PageRank.

Design:
  - `EmbeddingIndex` wraps a fastembed model (default: BAAI/bge-small-en-v1.5,
    384-dim, ~30MB quantized).
  - Vectors are stored in a new `symbol_embeddings` table (rowid,
    file_id, vector as BLOB). We keep it opt-in — schema already deployed
    via the 1 MB ceiling is not worth burning unless users enable it.
  - Similarity search: cosine over numpy, exact (no ANN library). For
    projects <50K symbols this is fine (~50ms for a 384d dot product).
  - Fusion: scores normalized to [0,1] and RRF-fused into existing pipeline
    when confidence is 'low' or 'medium' and embed_fallback is enabled.

Install: pip install "nexus[embed]"  (adds fastembed + onnxruntime)
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("nexus.rank.embed")

# ── Optional dep detection ──────────────────────────────────────────────────

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover — numpy is a hard dep of the rank extras
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore

try:
    from fastembed import TextEmbedding  # type: ignore
    _FASTEMBED_AVAILABLE = True
except ImportError:
    _FASTEMBED_AVAILABLE = False
    TextEmbedding = None  # type: ignore


DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"  # 384d, ~30MB quantized
DEFAULT_DIM = 384


def is_available() -> bool:
    """True if fastembed (and numpy) are importable."""
    return _FASTEMBED_AVAILABLE and _NUMPY_AVAILABLE


# ── Schema helpers (lazy, only created if embeddings are actually used) ────

_EMBED_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_embeddings (
    symbol_id   INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbol_embeddings_file ON symbol_embeddings(file_id);
"""


def ensure_embed_schema(conn: sqlite3.Connection) -> None:
    """Create the symbol_embeddings table on demand — lazy install."""
    conn.executescript(_EMBED_SCHEMA)


# ── Core index ──────────────────────────────────────────────────────────────

class EmbeddingIndex:
    """Thin wrapper around a fastembed TextEmbedding model + SQLite BLOB store.

    Usage:
        idx = EmbeddingIndex()
        if idx.is_available:
            idx.build(db)
            results = idx.query("authentication flow", top_k=10)
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM):
        self.model_name = model_name
        self.dim = dim
        self._model: Any | None = None
        self._symbol_ids: list[int] = []
        self._file_ids: list[int] = []
        self._matrix: Any | None = None  # numpy ndarray, shape (N, dim)

    @property
    def is_available(self) -> bool:
        return is_available()

    def _load_model(self) -> None:
        if self._model is None:
            if not self.is_available:
                raise RuntimeError(
                    "Embeddings unavailable. Install with: pip install 'nexus[embed]'"
                )
            logger.info("Loading embedding model: %s", self.model_name)
            self._model = TextEmbedding(model_name=self.model_name)

    def _embed_batch(self, texts: list[str]) -> Any:
        """Embed a batch of texts; returns a (len(texts), dim) numpy array."""
        self._load_model()
        vectors = list(self._model.embed(texts))  # type: ignore[union-attr]
        arr = np.asarray(vectors, dtype=np.float32)  # type: ignore[union-attr]
        # L2-normalize so cosine == dot product
        norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
        return arr / norms

    # ── Build / persist ────────────────────────────────────────────────────

    def build(self, db, batch_size: int = 64) -> int:
        """Embed every symbol in the db; persist vectors in symbol_embeddings.

        Returns the number of symbols embedded. Safe to call repeatedly —
        existing rows are replaced.
        """
        if not self.is_available:
            logger.warning("fastembed not installed; skipping embedding build")
            return 0

        with db.connect() as conn:
            ensure_embed_schema(conn)
            rows = conn.execute(
                "SELECT s.id AS sid, s.file_id, s.name, s.qualified, s.kind, "
                "       s.signature, s.docstring "
                "FROM symbols s"
            ).fetchall()

        if not rows:
            return 0

        # Compose a short descriptive text per symbol — what the model sees.
        texts: list[str] = []
        sids: list[int] = []
        fids: list[int] = []
        for r in rows:
            parts = [r["kind"], r["name"], r["qualified"] or ""]
            if r["signature"]:
                parts.append(r["signature"])
            if r["docstring"]:
                parts.append(r["docstring"][:200])  # cap for speed
            texts.append(" | ".join(p for p in parts if p))
            sids.append(int(r["sid"]))
            fids.append(int(r["file_id"]))

        all_vectors: list[Any] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            all_vectors.append(self._embed_batch(batch))
        matrix = np.vstack(all_vectors)  # type: ignore[union-attr]

        # Persist
        with db.connect() as conn:
            ensure_embed_schema(conn)
            conn.execute("DELETE FROM symbol_embeddings")
            for sid, fid, vec in zip(sids, fids, matrix):
                conn.execute(
                    "INSERT OR REPLACE INTO symbol_embeddings "
                    "(symbol_id, file_id, model, dim, vector) VALUES (?, ?, ?, ?, ?)",
                    (sid, fid, self.model_name, self.dim, vec.tobytes()),
                )

        self._symbol_ids = sids
        self._file_ids = fids
        self._matrix = matrix
        logger.info("Embedded %d symbols (%s, dim=%d)", len(sids), self.model_name, self.dim)
        return len(sids)

    def load(self, db) -> bool:
        """Load persisted embeddings into memory. Returns True on success."""
        if not self.is_available:
            return False
        try:
            with db.connect() as conn:
                ensure_embed_schema(conn)
                rows = conn.execute(
                    "SELECT symbol_id, file_id, dim, vector FROM symbol_embeddings"
                ).fetchall()
        except sqlite3.OperationalError:
            return False

        if not rows:
            return False

        sids: list[int] = []
        fids: list[int] = []
        vectors: list[Any] = []
        for r in rows:
            sids.append(int(r["symbol_id"]))
            fids.append(int(r["file_id"]))
            vectors.append(np.frombuffer(r["vector"], dtype=np.float32))  # type: ignore[union-attr]
        self._symbol_ids = sids
        self._file_ids = fids
        self._matrix = np.vstack(vectors)  # type: ignore[union-attr]
        return True

    # ── Query ──────────────────────────────────────────────────────────────

    def query(self, query_text: str, top_k: int = 20) -> list[dict[str, Any]]:
        """Return top-k symbols by cosine similarity to the query.

        Results are aggregated to the file level (max-pool) and sorted.
        """
        if not self.is_available or self._matrix is None or not self._symbol_ids:
            return []

        q = self._embed_batch([query_text])[0]  # (dim,)
        scores = self._matrix @ q  # (N,) — cosine since everything normalized

        # Aggregate to files via max-pool (best-matching symbol per file).
        best_by_file: dict[int, float] = {}
        best_symbol_by_file: dict[int, int] = {}
        for sid, fid, score in zip(self._symbol_ids, self._file_ids, scores):
            s = float(score)
            if fid not in best_by_file or s > best_by_file[fid]:
                best_by_file[fid] = s
                best_symbol_by_file[fid] = sid

        ranked = sorted(best_by_file.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {
                "file_id": fid,
                "symbol_id": best_symbol_by_file[fid],
                "score": float(score),
                "rank": i,
            }
            for i, (fid, score) in enumerate(ranked)
        ]
