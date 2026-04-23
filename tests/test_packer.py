"""Tests for context packer improvements (Aider-inspired)."""

from nexus.rank.packer import (
    MAX_LINE_CHARS,
    budget_scale_for_model,
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


class TestBudgetScale:
    """Tokenizer-aware budget compensation for newer Claude models (4.7+)."""

    def test_unknown_model_no_scaling(self):
        assert budget_scale_for_model(None) == 1.0
        assert budget_scale_for_model("") == 1.0
        assert budget_scale_for_model("gpt-5-turbo") == 1.0

    def test_opus_4_7_scales_down(self):
        # Any recognisable 4.7 variant triggers the 0.75 scale.
        assert budget_scale_for_model("claude-opus-4-7") == 0.75
        assert budget_scale_for_model("claude-opus-4.7") == 0.75
        assert budget_scale_for_model("opus-4.7") == 0.75
        assert budget_scale_for_model("opus-4-7") == 0.75

    def test_opus_4_7_fuzzy_match(self):
        # Variants with full Anthropic prefixes or suffixes still match.
        assert budget_scale_for_model("claude-3-opus-4-7-preview") == 0.75
        assert budget_scale_for_model("CLAUDE-OPUS-4.7") == 0.75

    def test_opus_4_6_unchanged(self):
        assert budget_scale_for_model("claude-opus-4-6") == 1.0
        assert budget_scale_for_model("opus-4.6") == 1.0

    def test_sonnet_and_haiku_unchanged(self):
        assert budget_scale_for_model("claude-sonnet-4-5") == 1.0
        assert budget_scale_for_model("claude-haiku-4-5") == 1.0


class TestPackContextBudgetScale:
    """pack_context must honour model/budget_scale params without regressions."""

    def test_explicit_scale_shrinks_budget(self, tmp_path):
        from nexus.rank.packer import pack_context
        from nexus.store.db import NexusDB

        # Create a minimal project with one real file so pack_context can read it.
        src = tmp_path / "big.py"
        # Write ~10K chars so the budget is the binding constraint.
        src.write_text("# comment line\n" * 800)

        db = NexusDB(tmp_path / ".nexus" / "nexus.db")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO files (path, sha256, language, line_count, byte_size, last_parsed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("big.py", "deadbeef", "python", 800, src.stat().st_size, 0.0),
            )
            file_id = conn.execute("SELECT id FROM files WHERE path = ?", ("big.py",)).fetchone()["id"]

        ranked = [{"file_id": file_id, "file_path": "big.py", "rank": 0, "rrf_score": 1.0}]

        full = pack_context(ranked, db, tmp_path, budget=4000, budget_scale=1.0)
        scaled = pack_context(ranked, db, tmp_path, budget=4000, budget_scale=0.5)

        full_chars = sum(p["char_count"] for p in full)
        scaled_chars = sum(p["char_count"] for p in scaled)

        # With scale 0.5, we should either pack less content or drop to a
        # leaner granularity — either way the packed total cannot exceed the
        # scaled budget.
        assert scaled_chars <= 2000
        assert full_chars <= 4000
        assert scaled_chars <= full_chars

    def test_model_param_applies_scale(self, tmp_path):
        from nexus.rank.packer import pack_context
        from nexus.store.db import NexusDB

        src = tmp_path / "big.py"
        src.write_text("# x\n" * 2000)

        db = NexusDB(tmp_path / ".nexus" / "nexus.db")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO files (path, sha256, language, line_count, byte_size, last_parsed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("big.py", "deadbeef", "python", 2000, src.stat().st_size, 0.0),
            )
            file_id = conn.execute("SELECT id FROM files WHERE path = ?", ("big.py",)).fetchone()["id"]

        ranked = [{"file_id": file_id, "file_path": "big.py", "rank": 0, "rrf_score": 1.0}]

        packed_47 = pack_context(ranked, db, tmp_path, budget=5000, model="claude-opus-4-7")
        packed_46 = pack_context(ranked, db, tmp_path, budget=5000, model="claude-opus-4-6")

        chars_47 = sum(p["char_count"] for p in packed_47)
        chars_46 = sum(p["char_count"] for p in packed_46)

        # 4.7 gets effective budget 3750; 4.6 gets 5000.
        assert chars_47 <= 3750
        assert chars_46 <= 5000

    def test_default_no_model_matches_old_behavior(self, tmp_path):
        """Regression guard: existing callers that don't pass `model` are unaffected."""
        from nexus.rank.packer import pack_context
        from nexus.store.db import NexusDB

        src = tmp_path / "big.py"
        src.write_text("# x\n" * 2000)

        db = NexusDB(tmp_path / ".nexus" / "nexus.db")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO files (path, sha256, language, line_count, byte_size, last_parsed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("big.py", "deadbeef", "python", 2000, src.stat().st_size, 0.0),
            )
            file_id = conn.execute("SELECT id FROM files WHERE path = ?", ("big.py",)).fetchone()["id"]

        ranked = [{"file_id": file_id, "file_path": "big.py", "rank": 0, "rrf_score": 1.0}]

        packed = pack_context(ranked, db, tmp_path, budget=5000)  # no model
        chars = sum(p["char_count"] for p in packed)
        assert chars <= 5000  # full budget available
