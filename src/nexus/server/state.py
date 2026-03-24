"""Shared server state and project activation logic."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from nexus.rank.bm25 import NexusBM25
from nexus.rank.pagerank import NexusPageRank
from nexus.session.tracker import SessionTracker
from nexus.store.db import NexusDB
from nexus.util.config import ProjectConfig

logger = logging.getLogger("nexus.server")

# Rate limiting: max tool calls per session
MAX_CALLS_PER_MINUTE = 120
_call_timestamps: list[float] = []


def _find_nexus_toml() -> Path | None:
    """Find nexus.toml by checking known locations."""
    import sys

    # 1. Environment variable override
    import os
    env_path = os.environ.get("NEXUS_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # 2. Next to the installed package (editable install / dev)
    pkg_root = Path(__file__).parent.parent.parent.parent / "nexus.toml"
    if pkg_root.exists():
        return pkg_root

    # 3. User config directory
    if sys.platform == "win32":
        user_cfg = Path.home() / ".nexus" / "nexus.toml"
    else:
        user_cfg = Path.home() / ".config" / "nexus" / "nexus.toml"
    if user_cfg.exists():
        return user_cfg

    # 4. Current working directory
    cwd_cfg = Path.cwd() / "nexus.toml"
    if cwd_cfg.exists():
        return cwd_cfg

    return None


# Runtime state — populated when a project is activated
_state: dict[str, Any] = {
    "db": None,
    "config": None,
    "bm25": None,
    "pagerank": None,
    "tracker": None,
}


def get_db() -> NexusDB:
    """Get the active database, raising if no project is active."""
    if _state["db"] is None:
        raise RuntimeError("No project active. Call nexus_scan or nexus_start first.")
    return _state["db"]


def get_config() -> ProjectConfig:
    """Get the active project config, raising if no project is active."""
    if _state["config"] is None:
        raise RuntimeError("No project active. Call nexus_scan or nexus_start first.")
    return _state["config"]


def get_tracker() -> SessionTracker:
    """Get the active session tracker, raising if no project is active."""
    if _state["tracker"] is None:
        raise RuntimeError("No project active. Call nexus_scan or nexus_start first.")
    return _state["tracker"]


def activate_project(
    project_root: str, languages: list[str] | None = None
) -> tuple[ProjectConfig, NexusDB]:
    """Activate a project by path or name, creating config and DB.

    Tries to match against nexus.toml registry first, then falls back
    to creating a config from the path directly.
    """
    from nexus.util.config import load_config

    root = Path(project_root).resolve()

    # Try loading from nexus.toml registry
    registry_path = _find_nexus_toml()
    if registry_path:
        registry = load_config(registry_path)

        # Match by name or by path
        for name, cfg in registry.items():
            if cfg.root.resolve() == root or name == project_root:
                db = NexusDB(cfg.db_path)
                _state["db"] = db
                _state["config"] = cfg
                _state["bm25"] = None
                _state["pagerank"] = None
                _state["tracker"] = SessionTracker(db)
                logger.info("Activated project %s from registry", name)
                return cfg, db

    # Fallback: create config from path
    if not root.is_dir():
        raise ValueError(f"Project root not found: {root}")

    name = root.name
    config = ProjectConfig(
        name=name,
        root=root,
        languages=languages or ["python"],
    )
    db = NexusDB(config.db_path)

    _state["db"] = db
    _state["config"] = config
    _state["bm25"] = None
    _state["pagerank"] = None
    _state["tracker"] = SessionTracker(db)
    logger.info("Activated project %s from path", name)
    return config, db


def ensure_ranking(db: NexusDB) -> tuple[NexusBM25, NexusPageRank]:
    """Build ranking indices if not already built. Applies tuned weights if available."""
    bm25 = _state.get("bm25")
    pr = _state.get("pagerank")

    if bm25 is None or not bm25.is_built:
        from nexus.rank.tuner import load_tuning
        from nexus.rank.bm25 import set_boosts

        tuned_boosts, _tuned_rrf = load_tuning(db)
        set_boosts(tuned_boosts)

        bm25 = NexusBM25()
        bm25.build(db)
        _state["bm25"] = bm25
        logger.debug("BM25 index built")

    if pr is None or not pr.is_built:
        pr = NexusPageRank()
        pr.build(db)
        _state["pagerank"] = pr
        logger.debug("PageRank built")

    return bm25, pr


def invalidate_ranking() -> None:
    """Clear cached ranking indices after edits."""
    _state["bm25"] = None
    _state["pagerank"] = None


def check_rate_limit() -> None:
    """Enforce rate limiting. Raises RuntimeError if exceeded."""
    import time

    now = time.time()
    # Remove timestamps older than 60 seconds
    while _call_timestamps and _call_timestamps[0] < now - 60:
        _call_timestamps.pop(0)

    if len(_call_timestamps) >= MAX_CALLS_PER_MINUTE:
        raise RuntimeError(
            f"Rate limit exceeded: {MAX_CALLS_PER_MINUTE} calls/minute. "
            "Wait a moment before making more requests."
        )
    _call_timestamps.append(now)


def validate_path(path: str, config: ProjectConfig) -> Path:
    """Validate and resolve a file path, ensuring it stays within the project root.

    Raises ValueError if the path escapes the project root.
    """
    # Normalize the path
    resolved = (config.root / path).resolve()

    # Check it's within the project root
    try:
        resolved.relative_to(config.root.resolve())
    except ValueError:
        raise ValueError(
            f"Path '{path}' resolves to '{resolved}' which is outside "
            f"the project root '{config.root}'"
        )

    return resolved
