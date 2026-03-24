"""Context packer: knapsack-based context selection with granularity levels.

Packs ranked files into a context window budget using a value/weight ratio.
Supports 4 granularity levels per file:
  1. full    — entire file content
  2. sigs    — signatures + docstrings
  3. names   — symbol names + kinds only
  4. path    — just the file path

Uses lost-in-the-middle ordering: highest relevance at start and end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.store.db import NexusDB

# Default context budget in characters
# Claude Code has a ~10K token limit per tool result (~40K chars).
# Stay well under to avoid truncation errors.
DEFAULT_BUDGET = 32_000  # ~8k tokens — fits within Claude Code's tool result limit


def pack_context(
    ranked_files: list[dict[str, Any]],
    db: NexusDB,
    project_root: Path,
    budget: int = DEFAULT_BUDGET,
) -> list[dict[str, Any]]:
    """Pack ranked files into a context budget.

    Each result gets assigned a granularity level based on available budget.
    Returns list of dicts with: file_id, file_path, rank, granularity, content, char_count.
    """
    packed: list[dict[str, Any]] = []
    remaining = budget

    for item in ranked_files:
        file_id = item["file_id"]
        file_path = item["file_path"]
        rrf_score = item.get("rrf_score", 0)

        symbols = db.get_symbols_for_file(file_id)

        # Try each granularity level from richest to leanest
        for granularity, content in _granularity_levels(file_path, symbols, project_root):
            char_count = len(content)
            if char_count <= remaining:
                packed.append({
                    "file_id": file_id,
                    "file_path": file_path,
                    "rank": item["rank"],
                    "rrf_score": rrf_score,
                    "granularity": granularity,
                    "content": content,
                    "char_count": char_count,
                })
                remaining -= char_count
                break
        else:
            # Even path-only didn't fit, skip this file
            continue

    # Lost-in-the-middle reordering: highest relevance at start and end
    if len(packed) > 2:
        packed = _lost_in_middle_order(packed)

    return packed


def _granularity_levels(
    file_path: str,
    symbols: list[dict],
    project_root: Path,
) -> list[tuple[str, str]]:
    """Generate content at each granularity level.

    Returns list of (granularity_name, content) from richest to leanest.
    """
    levels: list[tuple[str, str]] = []

    # Level 1: Full file content
    abs_path = project_root / file_path
    if abs_path.exists():
        try:
            full = abs_path.read_text(errors="replace")
            header = f"### {file_path}\n"
            levels.append(("full", header + full))
        except Exception:
            pass

    # Level 2: Signatures + docstrings
    if symbols:
        sig_lines = [f"### {file_path} (signatures)"]
        for s in symbols:
            sig_lines.append(f"  {s['kind']} {s['qualified']}")
            if s.get("signature"):
                sig_lines.append(f"    {s['signature']}")
            if s.get("docstring"):
                doc = s["docstring"]
                if len(doc) > 200:
                    doc = doc[:200] + "..."
                sig_lines.append(f"    \"\"\"{doc}\"\"\"")
        levels.append(("sigs", "\n".join(sig_lines)))

    # Level 3: Symbol names only
    if symbols:
        name_lines = [f"### {file_path} (symbols)"]
        for s in symbols:
            name_lines.append(f"  {s['kind']:10s} {s['name']}")
        levels.append(("names", "\n".join(name_lines)))

    # Level 4: Path only (always fits)
    levels.append(("path", f"  {file_path}"))

    return levels


def _lost_in_middle_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder items so highest relevance is at start and end.

    Research shows LLMs attend most to the beginning and end of context.
    Place odd-ranked items at the start, even-ranked at the end (reversed).
    """
    start = items[::2]   # indices 0, 2, 4, ... (highest, 3rd highest, ...)
    end = items[1::2]    # indices 1, 3, 5, ... (2nd highest, 4th highest, ...)
    end.reverse()
    return start + end


def format_packed_context(packed: list[dict[str, Any]]) -> str:
    """Format packed context into a single string for delivery."""
    if not packed:
        return "No relevant files found."

    sections: list[str] = []
    total_chars = sum(p["char_count"] for p in packed)

    sections.append(f"## Context ({len(packed)} files, {total_chars} chars)")
    sections.append("")

    for p in packed:
        sections.append(p["content"])
        sections.append("")

    return "\n".join(sections)
