"""Tests for config loading and validation."""

import pytest
from pathlib import Path

from nexus.util.config import ProjectConfig, ConfigError, _validate_project


def test_valid_config(tmp_path):
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()

    config = ProjectConfig(
        name="test",
        root=proj_dir,
        languages=["python"],
    )
    _validate_project(config)  # should not raise


def test_invalid_language(tmp_path):
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()

    config = ProjectConfig(
        name="test",
        root=proj_dir,
        languages=["cobol"],
    )
    with pytest.raises(ConfigError, match="unsupported language"):
        _validate_project(config)


def test_nonexistent_root():
    config = ProjectConfig(
        name="test",
        root=Path("/nonexistent/path/123456"),
        languages=["python"],
    )
    with pytest.raises(ConfigError, match="does not exist"):
        _validate_project(config)


def test_max_files_validation(tmp_path):
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()

    config = ProjectConfig(
        name="test",
        root=proj_dir,
        languages=["python"],
        max_files=0,
    )
    with pytest.raises(ConfigError, match="max_files"):
        _validate_project(config)


def test_extensions_property():
    config = ProjectConfig(
        name="test",
        root=Path("."),
        languages=["python", "rust"],
    )
    exts = config.extensions
    assert ".py" in exts
    assert ".rs" in exts
    assert ".js" not in exts
