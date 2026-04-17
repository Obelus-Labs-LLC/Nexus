"""Tests for the LSP (jedi) integration.

These tests require jedi — it's in the `rank` extras, so CI installs it.
They create small Python fixtures in tmp_path and exercise goto/refs/infer.
"""

import pytest

from nexus.refactor.lsp import (
    LSPResult,
    Location,
    _has_jedi,
    find_references,
    format_lsp_result,
    goto_definition,
    get_signatures,
    infer_type,
)


pytestmark = pytest.mark.skipif(not _has_jedi(), reason="jedi not installed")


def _write(tmp_path, rel, content):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── Basic graceful degradation ──────────────────────────────────────────────

def test_non_python_language_returns_error(tmp_path):
    r = goto_definition(tmp_path, "file.rs", 1, 0, language="rust")
    assert r.ok is False
    assert "rust" in r.error.lower()


# ── goto_definition ─────────────────────────────────────────────────────────

class TestGotoDefinition:
    def test_local_function_definition(self, tmp_path):
        _write(tmp_path, "m.py", "def greet():\n    return 1\n\nx = greet()\n")
        # cursor on "greet" use-site at line 4, col 4
        r = goto_definition(tmp_path, "m.py", 4, 4)
        assert r.ok
        assert len(r.locations) >= 1
        # Definition is at line 1
        assert any(loc.line == 1 for loc in r.locations)

    def test_missing_file_handled(self, tmp_path):
        r = goto_definition(tmp_path, "nope.py", 1, 0)
        # jedi will raise; our wrapper returns ok=False cleanly
        assert r.ok is False

    def test_follows_import(self, tmp_path):
        _write(tmp_path, "lib.py", "def helper():\n    return 42\n")
        _write(tmp_path, "app.py", "from lib import helper\n\nvalue = helper()\n")
        r = goto_definition(tmp_path, "app.py", 3, 10)
        assert r.ok
        # At least one definition points back to lib.py
        paths = [loc.file_path for loc in r.locations]
        assert any("lib.py" in p for p in paths)


# ── find_references ─────────────────────────────────────────────────────────

class TestFindReferences:
    def test_finds_local_uses(self, tmp_path):
        _write(tmp_path, "m.py", "x = 1\ny = x + 2\nz = x * 3\n")
        r = find_references(tmp_path, "m.py", 1, 0)
        assert r.ok
        # 3 references: definition + 2 uses
        assert len(r.locations) >= 2


# ── get_signatures ──────────────────────────────────────────────────────────

class TestGetSignatures:
    def test_returns_empty_outside_call(self, tmp_path):
        _write(tmp_path, "m.py", "x = 1\n")
        r = get_signatures(tmp_path, "m.py", 1, 2)
        assert r.ok
        # No active call at this position → empty list is fine
        assert r.details == [] or len(r.details) == 0


# ── infer_type ──────────────────────────────────────────────────────────────

class TestInferType:
    def test_infer_int(self, tmp_path):
        _write(tmp_path, "m.py", "x = 42\n")
        # Cursor on the '42' literal's name binding
        r = infer_type(tmp_path, "m.py", 1, 0)
        assert r.ok
        # details may contain 'int' or the full name
        combined = " ".join(r.details).lower()
        assert "int" in combined or r.locations


# ── format_lsp_result ───────────────────────────────────────────────────────

def test_format_error_result():
    r = LSPResult(ok=False, kind="definition", error="test error")
    assert "failed" in format_lsp_result(r)
    assert "test error" in format_lsp_result(r)


def test_format_empty_result():
    r = LSPResult(ok=True, kind="references", locations=[])
    assert "no results" in format_lsp_result(r)


def test_format_with_locations():
    r = LSPResult(
        ok=True, kind="definition",
        locations=[Location(file_path="a.py", line=5, column=2, preview="def foo():")],
        details=["function: foo"],
    )
    out = format_lsp_result(r)
    assert "a.py:5:2" in out
    assert "function: foo" in out
    assert "def foo" in out


# ── Dataclass sanity ────────────────────────────────────────────────────────

def test_location_to_dict():
    loc = Location("a.py", 1, 2, "preview")
    d = loc.to_dict()
    assert d == {"file_path": "a.py", "line": 1, "column": 2, "preview": "preview"}


def test_result_to_dict():
    r = LSPResult(ok=True, kind="definition", locations=[Location("a.py", 1, 0)])
    d = r.to_dict()
    assert d["ok"] is True
    assert d["kind"] == "definition"
    assert len(d["locations"]) == 1
