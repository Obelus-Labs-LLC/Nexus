"""Tests for the tree-sitter parser."""

from pathlib import Path

from nexus.index.parser import parse_file


def test_parse_python_function(tmp_path):
    f = tmp_path / "mod.py"
    # Write with explicit newlines to avoid Windows \r\n issues
    f.write_bytes(b'def greet(name: str) -> str:\n    """Say hello."""\n    return f"hi {name}"\n')

    result = parse_file(f, "python")
    assert not result.errors
    assert len(result.symbols) == 1

    sym = result.symbols[0]
    assert sym.name == "greet"
    assert sym.kind == "function"
    assert "name: str" in sym.signature
    assert sym.docstring == "Say hello."


def test_parse_python_class_with_methods(tmp_path):
    f = tmp_path / "cls.py"
    f.write_text(
        'class Foo:\n'
        '    """A foo."""\n'
        '    def bar(self):\n'
        '        pass\n'
        '    def baz(self, x: int):\n'
        '        pass\n'
    )

    result = parse_file(f, "python")
    assert not result.errors
    assert len(result.symbols) == 3  # Foo, bar, baz

    cls = result.symbols[0]
    assert cls.name == "Foo"
    assert cls.kind == "class"

    methods = [s for s in result.symbols if s.kind == "method"]
    assert len(methods) == 2
    assert {m.name for m in methods} == {"bar", "baz"}


def test_parse_python_imports(tmp_path):
    f = tmp_path / "imp.py"
    f.write_text(
        'import os\n'
        'from pathlib import Path\n'
        'from typing import Any, Optional\n'
    )

    result = parse_file(f, "python")
    assert len(result.imports) == 3

    assert result.imports[0].module == "os"
    assert result.imports[1].module == "pathlib"
    assert "Path" in result.imports[1].names
    assert "Any" in result.imports[2].names


def test_parse_unsupported_language(tmp_path):
    f = tmp_path / "test.xyz"
    f.write_text("hello")

    result = parse_file(f, "brainfuck")
    assert len(result.errors) == 1
    assert "Unsupported" in result.errors[0]


def test_parse_syntax_error_partial(tmp_path):
    """Tree-sitter should still produce partial results on syntax errors."""
    f = tmp_path / "bad.py"
    f.write_text(
        'def good_func():\n'
        '    pass\n'
        '\n'
        'def bad_func(\n'  # intentionally broken
        '    # missing close paren\n'
    )

    result = parse_file(f, "python")
    # Should still extract good_func even if bad_func fails
    names = [s.name for s in result.symbols]
    assert "good_func" in names


def test_parse_rust_basic(tmp_path):
    f = tmp_path / "lib.rs"
    # Write with explicit Unix newlines for consistent parsing
    f.write_bytes(
        b'/// A public function\n'
        b'pub fn add(a: i32, b: i32) -> i32 {\n'
        b'    a + b\n'
        b'}\n'
        b'\n'
        b'struct Point {\n'
        b'    x: f64,\n'
        b'    y: f64,\n'
        b'}\n'
    )

    result = parse_file(f, "rust")
    assert not result.errors
    names = [s.name for s in result.symbols]
    assert "add" in names
    assert "Point" in names

    add_sym = next(s for s in result.symbols if s.name == "add")
    assert add_sym.visibility == "public"
    assert add_sym.docstring is not None


def test_parse_typescript_basic(tmp_path):
    f = tmp_path / "mod.ts"
    f.write_text(
        'export function greet(name: string): string {\n'
        '    return `Hello ${name}`;\n'
        '}\n'
        '\n'
        'interface User {\n'
        '    id: number;\n'
        '    name: string;\n'
        '}\n'
    )

    result = parse_file(f, "typescript")
    assert not result.errors
    names = [s.name for s in result.symbols]
    assert "greet" in names
    assert "User" in names
