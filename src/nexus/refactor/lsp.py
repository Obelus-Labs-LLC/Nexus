"""LSP-style compiler-accurate lookups (Serena/SwiftLens-inspired).

Nexus's tree-sitter index is fast and multi-language, but purely structural:
it knows *what* symbols exist, not how they resolve at import time. For
Python specifically, `jedi` (already a dep) gives us proper:

  - goto_definition (follow imports, class MRO, etc.)
  - get_references (all use-sites, not just textual matches)
  - get_signatures (resolved type-annotated signatures)
  - infer (type of an expression at a location)

This module wraps jedi behind a stable API that other code-language LSPs
can implement later (e.g. `rust-analyzer`, `gopls`, `clangd`) without
callers having to change.

For now: Python-only. Non-Python calls return a "not supported" marker,
matching our graceful-degradation pattern elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Location:
    """A file position returned by an LSP lookup."""
    file_path: str
    line: int       # 1-based
    column: int     # 0-based
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "line": self.line,
            "column": self.column,
            "preview": self.preview,
        }


@dataclass
class LSPResult:
    """Result of an LSP operation."""
    ok: bool
    kind: str                              # 'definition', 'references', 'signatures', 'infer'
    locations: list[Location] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "locations": [loc.to_dict() for loc in self.locations],
            "details": self.details,
            "error": self.error,
        }


# ── Python backend (jedi) ───────────────────────────────────────────────────

def _has_jedi() -> bool:
    try:
        import jedi  # noqa: F401
        return True
    except ImportError:  # pragma: no cover
        return False


def _jedi_script(project_root: Path, file: str, source: str | None = None):
    """Create a jedi Script for the file, using the project as the search root."""
    import jedi
    abs_path = project_root / file
    project = jedi.Project(str(project_root))
    code = source if source is not None else abs_path.read_text(encoding="utf-8", errors="replace")
    return jedi.Script(code=code, path=str(abs_path), project=project)


def _name_location(name: Any) -> Location:
    """Convert a jedi Name to our Location dataclass."""
    line = int(name.line) if name.line else 0
    column = int(name.column) if name.column is not None else 0
    path = str(name.module_path) if name.module_path else "<builtin>"
    preview = ""
    try:
        if name.module_path and line:
            with open(name.module_path, encoding="utf-8", errors="replace") as f:
                for i, ln in enumerate(f, start=1):
                    if i == line:
                        preview = ln.rstrip("\n")[:200]
                        break
    except Exception:
        pass
    return Location(file_path=path, line=line, column=column, preview=preview)


def goto_definition(
    project_root: Path,
    file: str,
    line: int,
    column: int = 0,
    language: str = "python",
) -> LSPResult:
    """Resolve the definition of the symbol at the given location.

    For Python, uses jedi — handles imports, class hierarchy, and stubs.
    For other languages, returns `ok=False` with a "not supported" error.
    """
    if language != "python":
        return LSPResult(ok=False, kind="definition",
                         error=f"LSP goto_definition not supported for {language}")
    if not _has_jedi():
        return LSPResult(ok=False, kind="definition",
                         error="jedi not installed; install Nexus rank extras")

    try:
        script = _jedi_script(project_root, file)
        defs = script.goto(line=line, column=column, follow_imports=True)
    except Exception as e:
        return LSPResult(ok=False, kind="definition", error=f"jedi error: {e}")

    locations = [_name_location(d) for d in defs]
    details = [f"{d.type}: {d.full_name or d.name}" for d in defs]
    return LSPResult(ok=True, kind="definition", locations=locations, details=details)


def find_references(
    project_root: Path,
    file: str,
    line: int,
    column: int = 0,
    language: str = "python",
) -> LSPResult:
    """Find all references to the symbol at the given location."""
    if language != "python":
        return LSPResult(ok=False, kind="references",
                         error=f"LSP find_references not supported for {language}")
    if not _has_jedi():
        return LSPResult(ok=False, kind="references",
                         error="jedi not installed")

    try:
        script = _jedi_script(project_root, file)
        refs = script.get_references(line=line, column=column, include_builtins=False)
    except Exception as e:
        return LSPResult(ok=False, kind="references", error=f"jedi error: {e}")

    locations = [_name_location(r) for r in refs]
    return LSPResult(ok=True, kind="references", locations=locations,
                     details=[f"ref #{i}" for i in range(len(locations))])


def get_signatures(
    project_root: Path,
    file: str,
    line: int,
    column: int,
    language: str = "python",
) -> LSPResult:
    """Return callable signatures at the given position (e.g., inside a call)."""
    if language != "python":
        return LSPResult(ok=False, kind="signatures",
                         error=f"LSP get_signatures not supported for {language}")
    if not _has_jedi():
        return LSPResult(ok=False, kind="signatures", error="jedi not installed")

    try:
        script = _jedi_script(project_root, file)
        sigs = script.get_signatures(line=line, column=column)
    except Exception as e:
        return LSPResult(ok=False, kind="signatures", error=f"jedi error: {e}")

    details = []
    locations = []
    for s in sigs:
        params = ", ".join(p.to_string() for p in s.params)
        details.append(f"{s.name}({params})")
        locations.append(_name_location(s))
    return LSPResult(ok=True, kind="signatures", locations=locations, details=details)


def infer_type(
    project_root: Path,
    file: str,
    line: int,
    column: int,
    language: str = "python",
) -> LSPResult:
    """Infer the type of the expression at a location."""
    if language != "python":
        return LSPResult(ok=False, kind="infer",
                         error=f"LSP infer not supported for {language}")
    if not _has_jedi():
        return LSPResult(ok=False, kind="infer", error="jedi not installed")

    try:
        script = _jedi_script(project_root, file)
        types = script.infer(line=line, column=column)
    except Exception as e:
        return LSPResult(ok=False, kind="infer", error=f"jedi error: {e}")

    details = [f"{t.type}: {t.full_name or t.name}" for t in types]
    locations = [_name_location(t) for t in types]
    return LSPResult(ok=True, kind="infer", locations=locations, details=details)


def format_lsp_result(result: LSPResult) -> str:
    """Render an LSPResult for MCP text output."""
    if not result.ok:
        return f"LSP {result.kind} failed: {result.error}"
    if not result.locations:
        return f"LSP {result.kind}: no results."

    lines = [f"## LSP {result.kind} ({len(result.locations)} result(s))"]
    for loc, detail in zip(result.locations, result.details or [""] * len(result.locations)):
        header = f"  {loc.file_path}:{loc.line}:{loc.column}"
        if detail:
            header += f"   [{detail}]"
        lines.append(header)
        if loc.preview:
            lines.append(f"      {loc.preview}")
    return "\n".join(lines)
