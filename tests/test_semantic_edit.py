"""Tests for semantic editing primitives (extract/inline/move)."""

import pytest

from nexus.refactor.semantic_edit import (
    EditResult,
    extract_block,
    inline_symbol,
    move_symbol,
)
from nexus.store.db import NexusDB


@pytest.fixture
def db(tmp_path):
    return NexusDB(tmp_path / "n.db")


def _add_file(db, path, lang="python"):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO files (path, sha256, language, line_count, "
            "byte_size, last_parsed) VALUES (?, ?, ?, ?, ?, ?)",
            (path, "h", lang, 0, 0, 0.0),
        )
        return cur.lastrowid


def _add_symbol(db, file_id, name, qualified, line_start, line_end, kind="function"):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO symbols (file_id, name, qualified, kind, line_start, "
            "line_end, signature, docstring, body_text, visibility, decorators) "
            "VALUES (?, ?, ?, ?, ?, ?, '', '', '', 'public', '')",
            (file_id, name, qualified, kind, line_start, line_end),
        )
        return cur.lastrowid


# ── extract_block ───────────────────────────────────────────────────────────

class TestExtractBlock:
    def test_missing_file(self, db, tmp_path):
        r = extract_block(db, tmp_path, "nope.py", 1, 2, "foo")
        assert not r.ok
        assert "not found" in r.error

    def test_invalid_identifier(self, db, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        r = extract_block(db, tmp_path, "a.py", 1, 1, "123bad")
        assert not r.ok
        assert "identifier" in r.error.lower()

    def test_invalid_range(self, db, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        r = extract_block(db, tmp_path, "a.py", 2, 1, "foo")
        assert not r.ok

    def test_dry_run_returns_diff_without_write(self, db, tmp_path):
        f = tmp_path / "a.py"
        content = "def main():\n    x = 1\n    y = 2\n    print(x + y)\n"
        f.write_text(content)
        r = extract_block(db, tmp_path, "a.py", 2, 3, "compute", dry_run=True)
        assert r.ok
        assert r.files_changed == []
        assert "compute" in r.preview
        # File unchanged
        assert f.read_text() == content

    def test_actual_write_changes_file(self, db, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("def main():\n    x = 1\n    y = 2\n    print(x + y)\n")
        r = extract_block(db, tmp_path, "a.py", 2, 3, "compute", dry_run=False)
        assert r.ok
        assert len(r.files_changed) == 1
        text = f.read_text()
        assert "def compute():" in text
        assert "compute()" in text  # call inserted

    def test_extract_preserves_indent_for_call(self, db, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("def main():\n    if True:\n        a = 1\n        b = 2\n")
        r = extract_block(db, tmp_path, "a.py", 3, 4, "helper", dry_run=False)
        assert r.ok
        text = f.read_text()
        # The call replaces the original block at the original indent (8 spaces)
        assert "        helper()" in text

    def test_end_line_beyond_file(self, db, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        r = extract_block(db, tmp_path, "a.py", 1, 99, "foo")
        assert not r.ok
        assert "exceeds" in r.error


# ── inline_symbol ───────────────────────────────────────────────────────────

class TestInlineSymbol:
    def test_missing_symbol(self, db, tmp_path):
        r = inline_symbol(db, tmp_path, "nope")
        assert not r.ok
        assert "not found" in r.error

    def test_ambiguous_symbol(self, db, tmp_path):
        # Same name in two files → error
        f1 = _add_file(db, "a.py")
        f2 = _add_file(db, "b.py")
        _add_symbol(db, f1, "helper", "a.helper", 1, 2)
        _add_symbol(db, f2, "helper", "b.helper", 1, 2)
        r = inline_symbol(db, tmp_path, "helper")
        assert not r.ok
        assert "Ambiguous" in r.error

    def test_non_python_refused(self, db, tmp_path):
        fid = _add_file(db, "a.rs", lang="rust")
        _add_symbol(db, fid, "h", "a.h", 1, 2)
        (tmp_path / "a.rs").write_text("fn h() -> i32 { 1 }\n")
        r = inline_symbol(db, tmp_path, "h")
        assert not r.ok
        assert "Python" in r.error

    def test_refuses_multi_statement_helper(self, db, tmp_path):
        fid = _add_file(db, "a.py")
        _add_symbol(db, fid, "h", "a.h", 1, 3)
        (tmp_path / "a.py").write_text(
            "def h():\n    x = 1\n    return x\n\nval = h()\n"
        )
        r = inline_symbol(db, tmp_path, "h")
        assert not r.ok
        assert "single-return" in r.error or "safe" in r.error

    def test_inlines_single_return_helper(self, db, tmp_path):
        fid = _add_file(db, "a.py")
        _add_symbol(db, fid, "double", "a.double", 1, 2)
        (tmp_path / "a.py").write_text(
            "def double(x):\n    return x * 2\n\nval = double(5)\n"
        )
        r = inline_symbol(db, tmp_path, "double", dry_run=True)
        assert r.ok
        assert "x * 2" in r.preview
        # original file unchanged on dry_run
        assert "def double" in (tmp_path / "a.py").read_text()


# ── move_symbol ─────────────────────────────────────────────────────────────

class TestMoveSymbol:
    def test_missing_symbol(self, db, tmp_path):
        r = move_symbol(db, tmp_path, "nope", "other.py")
        assert not r.ok
        assert "not found" in r.error

    def test_same_file_refused(self, db, tmp_path):
        fid = _add_file(db, "a.py")
        _add_symbol(db, fid, "f", "a.f", 1, 2)
        (tmp_path / "a.py").write_text("def f(): pass\n")
        r = move_symbol(db, tmp_path, "f", "a.py")
        assert not r.ok
        assert "same file" in r.error.lower()

    def test_move_produces_two_file_diff(self, db, tmp_path):
        fid = _add_file(db, "src/a.py")
        _add_symbol(db, fid, "f", "src.a.f", 1, 3)
        src = tmp_path / "src" / "a.py"
        src.parent.mkdir(parents=True)
        src.write_text("def f():\n    return 1\n    # end\n\nvalue = f()\n")

        r = move_symbol(db, tmp_path, "f", "src/b.py", dry_run=True)
        assert r.ok
        assert "src/a.py" in r.preview
        assert "src/b.py" in r.preview
        # Dry-run: no files written
        assert not (tmp_path / "src" / "b.py").exists()

    def test_move_applied_writes_both_files(self, db, tmp_path):
        fid = _add_file(db, "src/a.py")
        _add_symbol(db, fid, "f", "src.a.f", 1, 2)
        src = tmp_path / "src" / "a.py"
        src.parent.mkdir(parents=True)
        src.write_text("def f():\n    return 1\n")

        r = move_symbol(db, tmp_path, "f", "src/b.py", dry_run=False)
        assert r.ok
        assert (tmp_path / "src" / "b.py").exists()
        assert "def f" in (tmp_path / "src" / "b.py").read_text()
        assert "def f" not in src.read_text()
        assert len(r.files_changed) == 2


# ── EditResult ──────────────────────────────────────────────────────────────

def test_edit_result_to_dict():
    r = EditResult(ok=True, files_changed=["x"], preview="diff")
    d = r.to_dict()
    assert d["ok"] is True
    assert d["files_changed"] == ["x"]
    assert d["preview"] == "diff"
    assert d["error"] is None
