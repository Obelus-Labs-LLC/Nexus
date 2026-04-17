"""Tests for context packer improvements (Aider-inspired)."""

from nexus.rank.packer import (
    MAX_LINE_CHARS,
    is_important_file,
    truncate_long_lines,
    _promote_important_files,
)


class TestTruncateLongLines:
    def test_short_lines_unchanged(self):
        text = "short\nlines\nhere"
        assert truncate_long_lines(text) == text

    def test_long_line_truncated(self):
        long_line = "x" * 500
        result = truncate_long_lines(long_line, max_chars=100)
        lines = result.split("\n")
        # First line is truncated to 100 + suffix
        assert lines[0].startswith("x" * 100)
        assert "+400 chars" in lines[0]
        # Summary line appended
        assert "truncated 1 long line" in lines[-1]

    def test_mixed_lines(self):
        text = "ok\n" + ("x" * 500) + "\nalso ok"
        result = truncate_long_lines(text, max_chars=100)
        lines = result.split("\n")
        assert lines[0] == "ok"
        assert lines[1].startswith("x" * 100)
        assert lines[2] == "also ok"
        assert "truncated 1" in lines[3]

    def test_default_limit_is_reasonable(self):
        assert MAX_LINE_CHARS >= 80
        assert MAX_LINE_CHARS <= 300


class TestImportantFiles:
    def test_readme_important(self):
        assert is_important_file("README.md")
        assert is_important_file("docs/README.md")  # name matches regardless of path
        assert is_important_file("README")

    def test_case_insensitive(self):
        assert is_important_file("ReadMe.md")
        assert is_important_file("LICENSE")
        assert is_important_file("license.txt")

    def test_config_files(self):
        assert is_important_file("pyproject.toml")
        assert is_important_file("Cargo.toml")
        assert is_important_file("package.json")
        assert is_important_file("Dockerfile")

    def test_regular_files_not_important(self):
        assert not is_important_file("src/main.py")
        assert not is_important_file("tests/test_foo.py")
        assert not is_important_file("random_file.md")

    def test_claude_ecosystem_files(self):
        assert is_important_file("CLAUDE.md")
        assert is_important_file("AGENTS.md")


class TestPromoteImportantFiles:
    def test_promotes_readme_above_regular(self):
        ranked = [
            {"file_path": "src/a.py", "rank": 0},
            {"file_path": "README.md", "rank": 1},
            {"file_path": "src/b.py", "rank": 2},
        ]
        out = _promote_important_files(ranked)
        assert out[0]["file_path"] == "README.md"
        assert out[1]["file_path"] == "src/a.py"
        assert out[2]["file_path"] == "src/b.py"

    def test_preserves_order_within_groups(self):
        ranked = [
            {"file_path": "src/a.py", "rank": 0},
            {"file_path": "README.md", "rank": 1},
            {"file_path": "pyproject.toml", "rank": 2},
            {"file_path": "src/b.py", "rank": 3},
        ]
        out = _promote_important_files(ranked)
        # README comes before pyproject (original order), both before src/*
        paths = [r["file_path"] for r in out]
        assert paths == ["README.md", "pyproject.toml", "src/a.py", "src/b.py"]

    def test_no_important_files_unchanged(self):
        ranked = [
            {"file_path": "src/a.py", "rank": 0},
            {"file_path": "src/b.py", "rank": 1},
        ]
        out = _promote_important_files(ranked)
        assert out == ranked

    def test_all_important_preserved(self):
        ranked = [
            {"file_path": "README.md", "rank": 0},
            {"file_path": "LICENSE", "rank": 1},
        ]
        out = _promote_important_files(ranked)
        assert out == ranked
