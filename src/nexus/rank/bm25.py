"""BM25S index with BM25F-style field boosts for symbol search.

Field boost strategy (from research):
  - Symbol name:  3x (most discriminative)
  - Signature:    2x (parameters, return types)
  - Docstring:    1.5x
  - Body text:    1x (baseline)

BM25F is approximated by repeating tokens proportionally to their field boost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bm25s
import numpy as np

from nexus.store.db import NexusDB
from nexus.util.identifiers import tokenize_code

# Default field boost weights (overridable via auto-tuner)
_BOOSTS = {
    "name": 3,
    "signature": 2,
    "docstring": 1,  # 1.5 rounded — bm25s works with token counts
    "body": 1,
}


def set_boosts(boosts: dict[str, int]) -> None:
    """Override field boost weights (called by auto-tuner)."""
    global _BOOSTS
    _BOOSTS.update(boosts)


class NexusBM25:
    """BM25S search index over project symbols."""

    def __init__(self) -> None:
        self._model: bm25s.BM25 | None = None
        self._file_ids: list[int] = []      # parallel to corpus — file_id per doc
        self._file_paths: list[str] = []    # parallel to corpus — path per doc
        self._symbol_ids: list[int] = []    # parallel to corpus — symbol_id per doc (0 = file-level)
        self._dirty: bool = True            # Needs rebuild

    @property
    def is_built(self) -> bool:
        return self._model is not None and not self._dirty

    def invalidate(self, file_ids: list[int] | None = None) -> None:
        """Mark the index as dirty. Called after edits."""
        self._dirty = True

    def build(self, db: NexusDB) -> int:
        """Build the BM25 index from the database.

        Creates one document per file, with boosted fields from all symbols in that file.
        Returns the number of documents indexed.
        """
        corpus_texts: list[list[str]] = []
        self._file_ids = []
        self._file_paths = []
        self._symbol_ids = []

        with db.connect() as conn:
            files = conn.execute("SELECT id, path FROM files").fetchall()

        # Pre-load generated file IDs for deprioritization
        with db.connect() as conn:
            gen_rows = conn.execute(
                "SELECT DISTINCT file_id FROM file_tags WHERE tag = 'generated'"
            ).fetchall()
        generated_ids = {r["file_id"] for r in gen_rows}

        for file_row in files:
            file_id = file_row["id"]
            file_path = file_row["path"]
            symbols = db.get_symbols_for_file(file_id)

            # Deprioritize generated files — halve their boosts
            boost_factor = 0.5 if file_id in generated_ids else 1.0

            # Build boosted token list for this file
            tokens: list[str] = []

            # Add file path tokens (module names are important)
            path_parts = Path(file_path).stem
            tokens.extend(tokenize_code(path_parts) * 2)

            for sym in symbols:
                # Name tokens — boosted 3x (halved for generated)
                name_tokens = tokenize_code(sym["name"])
                name_repeat = max(1, int(_BOOSTS["name"] * boost_factor))
                tokens.extend(name_tokens * name_repeat)

                # Qualified name tokens
                tokens.extend(tokenize_code(sym["qualified"]))

                # Signature tokens — boosted 2x
                if sym["signature"]:
                    sig_tokens = tokenize_code(sym["signature"])
                    sig_repeat = max(1, int(_BOOSTS["signature"] * boost_factor))
                    tokens.extend(sig_tokens * sig_repeat)

                # Docstring tokens
                if sym["docstring"]:
                    doc_tokens = tokenize_code(sym["docstring"])
                    tokens.extend(doc_tokens * _BOOSTS["docstring"])

                # Body tokens — 1x (no boost)
                if sym["body_text"]:
                    body_tokens = tokenize_code(sym["body_text"])
                    # Limit body tokens to prevent huge files from dominating
                    tokens.extend(body_tokens[:500] * _BOOSTS["body"])

            if tokens:
                corpus_texts.append(tokens)
                self._file_ids.append(file_id)
                self._file_paths.append(file_path)
                self._symbol_ids.append(0)  # file-level doc

        if not corpus_texts:
            return 0

        # Build BM25 index — join pre-tokenized lists into strings for bm25s
        self._model = bm25s.BM25()
        corpus_strings = [" ".join(tokens) for tokens in corpus_texts]
        tokenized = bm25s.tokenize(corpus_strings)
        self._model.index(tokenized)
        self._dirty = False

        return len(corpus_texts)

    def build_if_needed(self, db: NexusDB) -> int:
        """Build only if the index is dirty or unbuilt. Returns corpus size."""
        if self.is_built:
            return len(self._file_ids)
        return self.build(db)

    def query(self, query: str, top_k: int = 20) -> list[dict[str, Any]]:
        """Search for files matching a query.

        Returns a list of dicts with: file_id, file_path, score, rank.
        """
        if not self._model or not self._file_ids:
            return []

        query_tokens = tokenize_code(query)
        if not query_tokens:
            return []

        k = min(top_k, len(self._file_ids))
        query_string = " ".join(query_tokens)
        query_tokenized = bm25s.tokenize([query_string])
        results, scores = self._model.retrieve(query_tokenized, k=k)

        ranked: list[dict[str, Any]] = []
        for rank, (idx, score) in enumerate(zip(results[0], scores[0])):
            if score <= 0:
                continue
            ranked.append({
                "file_id": self._file_ids[idx],
                "file_path": self._file_paths[idx],
                "score": float(score),
                "rank": rank,
            })

        return ranked

    def save(self, path: Path) -> None:
        """Save the index and metadata to disk."""
        if self._model:
            path.mkdir(parents=True, exist_ok=True)
            self._model.save(path, corpus=None)
            meta = {
                "file_ids": self._file_ids,
                "file_paths": self._file_paths,
                "symbol_ids": self._symbol_ids,
            }
            (path / "meta.json").write_text(json.dumps(meta))

    def load(self, path: Path) -> bool:
        """Load the index from disk. Returns True if loaded successfully."""
        meta_path = path / "meta.json"
        if path.exists() and meta_path.exists():
            try:
                self._model = bm25s.BM25.load(path, load_corpus=False)
                meta = json.loads(meta_path.read_text())
                self._file_ids = meta["file_ids"]
                self._file_paths = meta["file_paths"]
                self._symbol_ids = meta.get("symbol_ids", [])
                self._dirty = False
                return True
            except Exception:
                self._dirty = True
                return False
        return False
