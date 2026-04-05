"""Configuration loader for nexus.toml project registry."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_IGNORE = [
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".next", "dist", "build",
    "target",  # Rust
    ".nexus",
    ".venv", "venv", ".env",
    "vendor", "third_party", "reference",  # vendored/reference code
    "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll",
    "*.min.js", "*.min.css",
    "*.lock",
]

_LANG_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py", ".pyi"],
    "rust": [".rs"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "c": [".c", ".h"],
    "go": [".go"],
}


@dataclass
class ProjectConfig:
    """Configuration for a single project."""
    name: str
    root: Path
    languages: list[str] = field(default_factory=lambda: ["python"])
    ignore: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    max_files: int = 50_000
    cluster: str | None = None
    cross_project: bool = False

    @property
    def extensions(self) -> set[str]:
        """File extensions to include based on configured languages."""
        exts: set[str] = set()
        for lang in self.languages:
            exts.update(_LANG_EXTENSIONS.get(lang, []))
        return exts

    @property
    def all_ignore(self) -> list[str]:
        """Merged default + project-specific ignore patterns."""
        return _DEFAULT_IGNORE + self.ignore

    @property
    def db_path(self) -> Path:
        return self.root / ".nexus" / "nexus.db"


_VALID_LANGUAGES = set(_LANG_EXTENSIONS.keys())

# Also accept plugin languages
def _get_valid_languages() -> set[str]:
    """Get all valid languages including plugins."""
    langs = set(_LANG_EXTENSIONS.keys())
    try:
        from nexus.index.plugins import list_plugins
        langs.update(list_plugins().keys())
    except ImportError:
        pass
    return langs


class ConfigError(ValueError):
    """Raised when nexus.toml has invalid configuration."""
    pass


def load_config(config_path: Path) -> dict[str, ProjectConfig]:
    """Load nexus.toml and return a dict of project name -> ProjectConfig.

    Raises ConfigError if the config is invalid.
    """
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    projects: dict[str, ProjectConfig] = {}
    errors: list[str] = []

    for cluster_name, cluster_data in data.get("cluster", {}).items():
        cross_project = cluster_data.get("cross_project", False)
        for proj_name, proj_data in cluster_data.get("project", {}).items():
            try:
                proj = _parse_project(proj_name, proj_data, cluster_name, cross_project)
                _validate_project(proj)
                projects[proj_name] = proj
            except (KeyError, ConfigError) as e:
                errors.append(f"cluster.{cluster_name}.{proj_name}: {e}")

    # Top-level projects (not in a cluster)
    for proj_name, proj_data in data.get("project", {}).items():
        try:
            proj = _parse_project(proj_name, proj_data)
            _validate_project(proj)
            projects[proj_name] = proj
        except (KeyError, ConfigError) as e:
            errors.append(f"project.{proj_name}: {e}")

    if errors and not projects:
        raise ConfigError(f"All projects invalid:\n  " + "\n  ".join(errors))

    # Log warnings for invalid projects but continue with valid ones
    if errors:
        import sys
        for err in errors:
            print(f"[nexus] config warning: {err}", file=sys.stderr)

    return projects


def _validate_project(config: ProjectConfig) -> None:
    """Validate a project configuration."""
    if not config.root.exists():
        raise ConfigError(f"root path does not exist: {config.root}")
    if not config.root.is_dir():
        raise ConfigError(f"root path is not a directory: {config.root}")
    valid = _get_valid_languages()
    for lang in config.languages:
        if lang not in valid:
            raise ConfigError(
                f"unsupported language '{lang}', valid: {sorted(valid)}"
            )
    if config.max_files < 1:
        raise ConfigError(f"max_files must be >= 1, got {config.max_files}")


def _parse_project(
    name: str,
    data: dict[str, Any],
    cluster: str | None = None,
    cross_project: bool = False,
) -> ProjectConfig:
    return ProjectConfig(
        name=name,
        root=Path(data["root"]),
        languages=data.get("languages", ["python"]),
        ignore=data.get("ignore", []),
        entry_points=data.get("entry_points", []),
        max_files=data.get("max_files", 50_000),
        cluster=cluster,
        cross_project=cross_project,
    )
