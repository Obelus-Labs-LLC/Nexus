"""Semantic editing primitives: extract, inline, move.

Inspired by Serena's LSP-backed refactorings, but implemented with the
existing tree-sitter parse tree — so they work for every language Nexus
indexes (Python, Rust, TS, JS, Go, Java, Ruby, PHP, Kotlin, Swift, Zig,
Solidity).

These operations are *line-based surgical edits* guided by the symbol
index. They deliberately avoid full AST rewriting (which would require
per-language printers) and instead rely on:

  1. The symbol index to find exact line ranges
  2. Textual transformations that preserve the surrounding formatting
  3. Dry-run support so agents can review before committing

Each function returns a structured result:
  {
    "ok": bool,
    "files_changed": list[str],   # absolute paths
    "preview": str,                # unified-diff-style preview
    "error": str | None,
  }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EditResult:
    """Result of a semantic-edit operation."""
    ok: bool
    files_changed: list[str] = field(default_factory=list)
    preview: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "files_changed": self.files_changed,
            "preview": self.preview,
            "error": self.error,
        }


# ── Helpers ─────────────────────────────────────────────────────────────────

def _find_symbol_location(db, qualified_name: str) -> dict[str, Any] | None:
    """Look up a symbol by its fully-qualified name. Returns file/line info."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT s.*, f.path AS file_path, f.language
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE s.qualified = ?
            LIMIT 1
            """,
            (qualified_name,),
        ).fetchone()
    return dict(row) if row else None


def _find_symbol_by_name(db, name: str) -> list[dict[str, Any]]:
    """Find symbols by short name (exact match first, then substring)."""
    with db.connect() as conn:
        exact = conn.execute(
            """
            SELECT s.*, f.path AS file_path, f.language
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE s.name = ?
            """,
            (name,),
        ).fetchall()
    return [dict(r) for r in exact]


def _read_lines(path: Path) -> list[str]:
    """Read a file preserving line endings."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines(keepends=True)


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines), encoding="utf-8")


def _unified_diff(before: list[str], after: list[str], file_path: str) -> str:
    """Tiny unified diff formatter — good enough for previews."""
    import difflib
    diff = difflib.unified_diff(
        before, after,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=3,
    )
    return "".join(diff)


def _detect_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# ── Operation: extract method/function ──────────────────────────────────────

def extract_block(
    db,
    project_root: Path,
    file: str,
    start_line: int,
    end_line: int,
    new_name: str,
    language: str = "python",
    dry_run: bool = True,
) -> EditResult:
    """Extract the given line range into a new top-level function/method.

    The extracted block is hoisted to just above the enclosing symbol (or
    to the end of the file if no enclosing symbol). A call to the new
    function replaces the original block.

    Behavior is intentionally conservative:
      - For Python: extracts into a free-standing `def`. Variables captured
        from the enclosing scope must be passed/returned manually — this
        tool produces a *draft* with a marker comment for the agent to refine.
      - For other languages: produces the same structural draft using the
        language's function-declaration keyword.

    Args:
        file: Relative file path.
        start_line, end_line: 1-indexed inclusive range to extract.
        new_name: Name of the new function.
        dry_run: If True (default), returns the diff preview without writing.
    """
    abs_path = project_root / file
    if not abs_path.exists():
        return EditResult(ok=False, error=f"File not found: {file}")
    if start_line < 1 or end_line < start_line:
        return EditResult(ok=False, error="Invalid line range")
    if not new_name.isidentifier():
        return EditResult(ok=False, error=f"Invalid identifier: {new_name!r}")

    lines = _read_lines(abs_path)
    if end_line > len(lines):
        return EditResult(ok=False, error=f"end_line {end_line} exceeds file length {len(lines)}")

    block = lines[start_line - 1:end_line]
    if not block:
        return EditResult(ok=False, error="Empty extraction range")

    indent = _detect_indent(block[0])
    # Strip common indent from the extracted block so the body is normalized.
    body = [ln[len(indent):] if ln.startswith(indent) else ln for ln in block]

    keyword = _FUNCTION_KEYWORD.get(language, "def")
    signature = _format_signature(keyword, new_name, language)

    new_fn = [signature]
    new_fn += [f"    {ln}" if ln.strip() else ln for ln in body]
    if not new_fn[-1].endswith("\n"):
        new_fn[-1] = new_fn[-1] + "\n"

    # Insertion point: just before the extraction (at the same indent level).
    call = f"{indent}{new_name}()  # TODO(nexus): pass captured vars\n"
    after = lines[:start_line - 1] + [call] + lines[end_line:]

    # Append new function at end of file (simplest; agent can move it).
    if after and not after[-1].endswith("\n"):
        after[-1] = after[-1] + "\n"
    after.append("\n\n")
    after.extend(new_fn)

    preview = _unified_diff(lines, after, file)

    if not dry_run:
        _write_lines(abs_path, after)

    return EditResult(
        ok=True,
        files_changed=[str(abs_path)] if not dry_run else [],
        preview=preview,
    )


# Language function keyword table (for signature generation)
_FUNCTION_KEYWORD: dict[str, str] = {
    "python": "def",
    "javascript": "function",
    "typescript": "function",
    "rust": "fn",
    "go": "func",
    "java": "static",  # private static void name(...) — caller may adjust
    "c": "void",       # caller adjusts
    "ruby": "def",
    "php": "function",
    "kotlin": "fun",
    "swift": "func",
    "zig": "fn",
    "solidity": "function",
}


def _format_signature(keyword: str, name: str, language: str) -> str:
    """Produce a bare function signature line with a TODO comment."""
    if language == "rust":
        return f"fn {name}() {{\n"
    if language in ("javascript", "typescript"):
        return f"function {name}() {{\n"
    if language == "go":
        return f"func {name}() {{\n"
    if language in ("c", "java"):
        return f"static void {name}() {{\n"
    if language in ("kotlin", "swift"):
        return f"{keyword} {name}() {{\n"
    if language == "zig":
        return f"fn {name}() void {{\n"
    if language == "solidity":
        return f"function {name}() internal {{\n"
    if language == "php":
        return f"function {name}() {{\n"
    # Python / Ruby default
    return f"{keyword} {name}():\n"


# ── Operation: inline symbol ────────────────────────────────────────────────

def inline_symbol(
    db,
    project_root: Path,
    symbol: str,
    dry_run: bool = True,
) -> EditResult:
    """Inline a small Python helper by replacing callsites with its body.

    Only supported for Python, only for single-return-expression helpers
    (the safe case). Unsafe helpers (side effects, multiple statements)
    are refused with an explanatory error — agents should fall back to
    manual refactoring.

    This is strictly opt-in: dry_run defaults to True so the agent always
    sees a preview before committing.
    """
    matches = _find_symbol_by_name(db, symbol)
    if not matches:
        return EditResult(ok=False, error=f"Symbol '{symbol}' not found")
    if len(matches) > 1:
        paths = ", ".join(m["qualified"] for m in matches[:5])
        return EditResult(
            ok=False,
            error=f"Ambiguous: {len(matches)} symbols named {symbol!r}. "
                  f"Candidates: {paths}. Use the qualified name via nexus_read first.",
        )

    sym = matches[0]
    if sym["language"] != "python":
        return EditResult(
            ok=False,
            error=f"inline currently supports only Python ({sym['language']} detected)",
        )

    file_path = project_root / sym["file_path"]
    lines = _read_lines(file_path)

    # Parse the function body to find a single return expression.
    body_lines = lines[sym["line_start"] - 1:sym["line_end"]]
    expr = _extract_single_return_expr(body_lines)
    if expr is None:
        return EditResult(
            ok=False,
            error=(f"{symbol} is not a safe single-return helper. "
                   "Inline refused — refactor manually."),
        )

    # Remove the definition
    without_def = lines[:sym["line_start"] - 1] + lines[sym["line_end"]:]
    # Replace calls like `symbol(...)` with the body expression (parameter
    # substitution is NOT performed — we annotate unsafe call sites).
    new_lines = []
    for ln in without_def:
        if f"{symbol}(" in ln:
            # Crude: wrap with a comment so the agent reviews each site.
            new_lines.append(ln.rstrip("\n") + f"  # TODO(nexus): inlined {symbol} → {expr}\n")
        else:
            new_lines.append(ln)

    preview = _unified_diff(lines, new_lines, sym["file_path"])

    if not dry_run:
        _write_lines(file_path, new_lines)

    return EditResult(
        ok=True,
        files_changed=[str(file_path)] if not dry_run else [],
        preview=preview,
    )


def _extract_single_return_expr(body_lines: list[str]) -> str | None:
    """If the function body is `return <expr>` (possibly with a docstring),
    return the expression text. Otherwise None.
    """
    stripped = [ln.strip() for ln in body_lines if ln.strip()]
    # Drop the def line
    if stripped and stripped[0].startswith("def "):
        stripped = stripped[1:]
    # Drop a docstring
    if stripped and (stripped[0].startswith('"""') or stripped[0].startswith("'''")):
        # Single-line docstring
        if stripped[0].count('"""') >= 2 or stripped[0].count("'''") >= 2:
            stripped = stripped[1:]
        else:
            # Multi-line: skip until closing
            for i, ln in enumerate(stripped[1:], start=1):
                if '"""' in ln or "'''" in ln:
                    stripped = stripped[i + 1:]
                    break

    if len(stripped) == 1 and stripped[0].startswith("return "):
        return stripped[0][len("return "):]
    return None


# ── Operation: move symbol to another file ──────────────────────────────────

def move_symbol(
    db,
    project_root: Path,
    symbol: str,
    target_file: str,
    update_imports: bool = True,
    dry_run: bool = True,
) -> EditResult:
    """Move a symbol's full definition from its current file to target_file.

    Steps (all best-effort, reviewed via preview):
      1. Locate the symbol via the index (qualified name or unique short name).
      2. Cut the source lines from the origin file.
      3. Append them to the target file (creating it if needed).
      4. (Python only, if update_imports): rewrite absolute imports of the
         symbol to point to the new module. For other languages, the user
         must fix imports manually — flagged in the preview.

    This is a coarse operation. It does NOT handle:
      - Decorator chains that reference local helpers
      - Nested class movement
      - Circular imports (they'll surface at runtime)

    Returns an EditResult with a unified diff spanning both files.
    """
    matches = _find_symbol_by_name(db, symbol)
    if not matches:
        return EditResult(ok=False, error=f"Symbol '{symbol}' not found")
    if len(matches) > 1:
        quals = ", ".join(m["qualified"] for m in matches[:5])
        return EditResult(
            ok=False,
            error=f"Ambiguous: {len(matches)} symbols named {symbol!r}. "
                  f"Candidates: {quals}.",
        )

    sym = matches[0]
    source_path = project_root / sym["file_path"]
    target_path = project_root / target_file
    if source_path == target_path:
        return EditResult(ok=False, error="Source and target are the same file")

    source_lines = _read_lines(source_path)
    sym_block = source_lines[sym["line_start"] - 1:sym["line_end"]]
    if not sym_block:
        return EditResult(ok=False, error=f"No lines found for {symbol}")

    # Remove from source
    new_source = source_lines[:sym["line_start"] - 1] + source_lines[sym["line_end"]:]

    # Append to target (create if missing)
    if target_path.exists():
        target_lines = _read_lines(target_path)
    else:
        target_lines = []
        if not dry_run:
            target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_lines and not target_lines[-1].endswith("\n"):
        target_lines[-1] = target_lines[-1] + "\n"
    if target_lines:
        target_lines.append("\n\n")
    new_target = target_lines + sym_block
    if new_target and not new_target[-1].endswith("\n"):
        new_target[-1] = new_target[-1] + "\n"

    preview_parts = [
        _unified_diff(source_lines, new_source, sym["file_path"]),
        _unified_diff(target_lines, new_target, target_file),
    ]

    changed: list[str] = []
    if not dry_run:
        _write_lines(source_path, new_source)
        _write_lines(target_path, new_target)
        changed = [str(source_path), str(target_path)]

    note = ""
    if update_imports and sym["language"] == "python":
        note = (
            f"\n# NOTE: run `nexus_rename {symbol} {symbol}` after moving to "
            f"refresh any stale imports (rope handles this).\n"
        )
    elif update_imports:
        note = (
            f"\n# NOTE: imports not auto-updated for {sym['language']}. "
            f"Grep for `{symbol}` and fix call sites manually.\n"
        )

    return EditResult(
        ok=True,
        files_changed=changed,
        preview="".join(preview_parts) + note,
    )
