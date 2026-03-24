"""Tests for server state management, input validation, and rate limiting."""

import time
from pathlib import Path

import pytest

from nexus.server.state import (
    _find_nexus_toml,
    activate_project,
    check_rate_limit,
    get_config,
    get_db,
    get_tracker,
    invalidate_ranking,
    validate_path,
    _state,
    _call_timestamps,
    MAX_CALLS_PER_MINUTE,
)
from nexus.util.config import ProjectConfig


class TestActivateProject:
    def test_activate_by_path(self, sample_project):
        config, db = activate_project(str(sample_project))
        assert config.name == sample_project.name
        assert config.root == sample_project
        assert db is not None

    def test_activate_nonexistent_raises(self, tmp_path):
        fake = tmp_path / "nonexistent"
        with pytest.raises(ValueError, match="not found"):
            activate_project(str(fake))

    def test_state_populated_after_activate(self, sample_project):
        activate_project(str(sample_project))
        assert get_db() is not None
        assert get_config() is not None
        assert get_tracker() is not None


class TestGettersBeforeActivation:
    def setup_method(self):
        # Reset state
        _state["db"] = None
        _state["config"] = None
        _state["tracker"] = None

    def test_get_db_raises(self):
        with pytest.raises(RuntimeError, match="No project active"):
            get_db()

    def test_get_config_raises(self):
        with pytest.raises(RuntimeError, match="No project active"):
            get_config()

    def test_get_tracker_raises(self):
        with pytest.raises(RuntimeError, match="No project active"):
            get_tracker()


class TestValidatePath:
    def test_valid_path(self, sample_project):
        config = ProjectConfig(name="test", root=sample_project, languages=["python"])
        result = validate_path("main.py", config)
        assert result == (sample_project / "main.py").resolve()

    def test_traversal_attack_blocked(self, sample_project):
        config = ProjectConfig(name="test", root=sample_project, languages=["python"])
        with pytest.raises(ValueError, match="outside the project root"):
            validate_path("../../etc/passwd", config)

    def test_absolute_path_outside_root(self, sample_project, tmp_path):
        config = ProjectConfig(name="test", root=sample_project, languages=["python"])
        # This should fail because the path resolves outside root
        evil = tmp_path / "evil.py"
        evil.write_text("x = 1")
        with pytest.raises(ValueError, match="outside the project root"):
            validate_path(f"../../{evil.name}", config)

    def test_nested_path_valid(self, sample_project):
        config = ProjectConfig(name="test", root=sample_project, languages=["python"])
        result = validate_path("migrations/001_init.py", config)
        assert "migrations" in str(result)


class TestRateLimiting:
    def setup_method(self):
        _call_timestamps.clear()

    def test_under_limit_passes(self):
        for _ in range(5):
            check_rate_limit()  # Should not raise

    def test_over_limit_raises(self):
        # Fill up the timestamps
        now = time.time()
        _call_timestamps.extend([now] * MAX_CALLS_PER_MINUTE)
        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            check_rate_limit()

    def test_old_timestamps_cleared(self):
        # Add old timestamps (>60s ago)
        old = time.time() - 61
        _call_timestamps.extend([old] * MAX_CALLS_PER_MINUTE)
        # Should pass because old ones get cleaned
        check_rate_limit()


class TestInvalidateRanking:
    def test_clears_caches(self, sample_project):
        activate_project(str(sample_project))
        _state["bm25"] = "fake"
        _state["pagerank"] = "fake"
        invalidate_ranking()
        assert _state["bm25"] is None
        assert _state["pagerank"] is None
