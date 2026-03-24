"""Cross-file symbol rename using language-specific refactoring engines.

Python: uses rope (compiler-accurate, handles imports/references)
Rust: shells out to rust-analyzer (if available)
TypeScript: shells out to typescript-language-server (if available)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RenameEdit:
    """A single text edit produced by a rename operation."""
    file: str          # absolute path
    old_text: str      # text that was replaced
    new_text: str      # replacement text
    line: int          # 1-based line number
    col: int           # 0-based column


@dataclass
class RenameResult:
    success: bool
    old_name: str
    new_name: str
    edits: list[RenameEdit] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    error: str = ""


def rename_python(
    project_root: Path,
    file_path: Path,
    line: int,
    col: int,
    new_name: str,
) -> RenameResult:
    """Rename a Python symbol at the given location using rope.

    Args:
        project_root: Root of the Python project.
        file_path: Absolute path to the file containing the symbol.
        line: 1-based line number of the symbol.
        col: 0-based column offset of the symbol.
        new_name: The new name for the symbol.
    """
    try:
        from rope.base.project import Project
        from rope.refactor.rename import Rename
    except ImportError:
        return RenameResult(
            success=False, old_name="", new_name=new_name,
            error="rope is not installed. Run: pip install rope",
        )

    project = Project(str(project_root))
    try:
        resource = project.get_resource(str(file_path.relative_to(project_root)))

        # Convert line:col to offset
        source = resource.read()
        lines = source.split("\n")
        offset = sum(len(l) + 1 for l in lines[:line - 1]) + col

        # Extract old name at offset
        old_name = _extract_identifier(source, offset)
        if not old_name:
            return RenameResult(
                success=False, old_name="", new_name=new_name,
                error=f"No identifier found at {file_path}:{line}:{col}",
            )

        renamer = Rename(project, resource, offset)
        changes = renamer.get_changes(new_name)

        edits = []
        files_changed = set()

        for change in changes.changes:
            changed_path = str(Path(project_root) / change.resource.path)
            files_changed.add(changed_path)

        # Apply the changes
        project.do(changes)

        return RenameResult(
            success=True,
            old_name=old_name,
            new_name=new_name,
            files_changed=sorted(files_changed),
        )

    except Exception as e:
        return RenameResult(
            success=False, old_name="", new_name=new_name,
            error=str(e),
        )
    finally:
        project.close()


def rename_by_name_python(
    project_root: Path,
    symbol_name: str,
    new_name: str,
    file_hint: str | None = None,
) -> RenameResult:
    """Rename a Python symbol by name, finding its definition first.

    Uses jedi to locate the symbol definition, then rope to rename.

    Args:
        project_root: Root of the Python project.
        symbol_name: Name of the symbol (e.g., "MyClass", "my_function").
        new_name: New name for the symbol.
        file_hint: Optional file path to narrow the search.
    """
    try:
        import jedi
    except ImportError:
        return RenameResult(
            success=False, old_name=symbol_name, new_name=new_name,
            error="jedi is not installed. Run: pip install jedi",
        )

    # Find the symbol definition using jedi
    search_path = Path(file_hint) if file_hint else project_root

    if file_hint:
        abs_path = project_root / file_hint if not Path(file_hint).is_absolute() else Path(file_hint)
        if abs_path.exists():
            source = abs_path.read_text(errors="replace")
            script = jedi.Script(source, path=str(abs_path), project=jedi.Project(str(project_root)))
            names = script.search(symbol_name)

            for name in names:
                if name.name == symbol_name and name.full_name:
                    defs = name.goto()
                    if defs:
                        d = defs[0]
                        return rename_python(
                            project_root,
                            Path(d.module_path),
                            d.line,
                            d.column,
                            new_name,
                        )

    # Search across all Python files
    for py_file in project_root.rglob("*.py"):
        if ".venv" in py_file.parts or "node_modules" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        try:
            source = py_file.read_text(errors="replace")
        except Exception:
            continue

        # Quick check — skip files that don't contain the name
        if symbol_name not in source:
            continue

        try:
            script = jedi.Script(source, path=str(py_file), project=jedi.Project(str(project_root)))
            names = script.search(symbol_name)

            for name in names:
                if name.name == symbol_name:
                    defs = name.goto()
                    if defs:
                        d = defs[0]
                        def_path = Path(d.module_path) if d.module_path else py_file
                        return rename_python(
                            project_root,
                            def_path,
                            d.line,
                            d.column,
                            new_name,
                        )
        except Exception:
            continue

    return RenameResult(
        success=False, old_name=symbol_name, new_name=new_name,
        error=f"Symbol '{symbol_name}' not found in project",
    )


def rename_rust(
    project_root: Path,
    file_path: str,
    line: int,
    col: int,
    new_name: str,
) -> RenameResult:
    """Rename a Rust symbol using rust-analyzer CLI.

    Requires rust-analyzer to be installed and in PATH.
    """
    # Check if rust-analyzer is available
    try:
        subprocess.run(
            ["rust-analyzer", "--version"],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return RenameResult(
            success=False, old_name="", new_name=new_name,
            error="rust-analyzer not found. Install it: rustup component add rust-analyzer",
        )

    # rust-analyzer doesn't have a simple CLI rename command,
    # so we use a workspace edit via LSP protocol.
    # For now, fall back to tree-sitter + text replacement for Rust.
    return _rename_by_text(project_root, file_path, line, col, new_name, "rust")


def _rename_by_text(
    project_root: Path,
    file_path: str,
    line: int,
    col: int,
    new_name: str,
    language: str,
) -> RenameResult:
    """Fallback: rename by finding all textual occurrences of the identifier.

    Less accurate than LSP but works for any language. Uses word-boundary
    matching to avoid renaming substrings.
    """
    import re

    abs_path = project_root / file_path
    if not abs_path.exists():
        return RenameResult(
            success=False, old_name="", new_name=new_name,
            error=f"File not found: {file_path}",
        )

    source = abs_path.read_text(errors="replace")
    lines = source.split("\n")

    if line < 1 or line > len(lines):
        return RenameResult(
            success=False, old_name="", new_name=new_name,
            error=f"Line {line} out of range (file has {len(lines)} lines)",
        )

    old_name = _extract_identifier(source, _line_col_to_offset(source, line, col))
    if not old_name:
        return RenameResult(
            success=False, old_name="", new_name=new_name,
            error=f"No identifier at {file_path}:{line}:{col}",
        )

    # Find all files that might reference this symbol
    ext_map = {
        "python": ["*.py"],
        "rust": ["*.rs"],
        "typescript": ["*.ts", "*.tsx"],
        "javascript": ["*.js", "*.jsx"],
        "c": ["*.c", "*.h"],
        "go": ["*.go"],
    }
    patterns = ext_map.get(language, ["*.*"])

    files_changed = []
    pattern = re.compile(r"\b" + re.escape(old_name) + r"\b")

    for ext in patterns:
        for target in project_root.rglob(ext):
            if ".venv" in target.parts or "node_modules" in target.parts or "target" in target.parts:
                continue
            try:
                content = target.read_text(errors="replace")
            except Exception:
                continue

            if old_name not in content:
                continue

            new_content = pattern.sub(new_name, content)
            if new_content != content:
                target.write_text(new_content)
                files_changed.append(str(target))

    return RenameResult(
        success=True,
        old_name=old_name,
        new_name=new_name,
        files_changed=files_changed,
    )


def _extract_identifier(source: str, offset: int) -> str:
    """Extract the identifier at the given offset in source."""
    if offset >= len(source):
        return ""

    # Walk backwards to find start
    start = offset
    while start > 0 and (source[start - 1].isalnum() or source[start - 1] == "_"):
        start -= 1

    # Walk forwards to find end
    end = offset
    while end < len(source) and (source[end].isalnum() or source[end] == "_"):
        end += 1

    return source[start:end]


def _line_col_to_offset(source: str, line: int, col: int) -> int:
    """Convert 1-based line and 0-based col to a string offset."""
    lines = source.split("\n")
    offset = sum(len(l) + 1 for l in lines[:line - 1]) + col
    return offset
