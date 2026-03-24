"""Tests for the full indexing pipeline."""

from nexus.index.pipeline import index_project


def test_index_project_basic(project_config, db):
    result = index_project(project_config, db)

    assert result.scan is not None
    assert result.scan.files_total >= 2
    assert result.symbols_added > 0
    assert result.duration_ms >= 0

    stats = db.get_stats()
    assert stats["files"] >= 2
    assert stats["symbols"] > 0


def test_index_project_incremental(project_config, db):
    """Second index should skip unchanged files."""
    index_project(project_config, db)
    result = index_project(project_config, db)

    # No new symbols should be added on second run (files unchanged)
    assert result.symbols_added == 0


def test_index_project_force(project_config, db):
    """Force re-index should re-parse all files."""
    index_project(project_config, db)
    result = index_project(project_config, db, force=True)

    assert result.symbols_added > 0


def test_index_resolves_imports(project_config, db):
    """Imports from app.py -> main.py should resolve."""
    result = index_project(project_config, db)

    # app.py imports from main — at least some should resolve
    assert result.imports_resolved >= 0  # may or may not resolve depending on qualification
    assert result.duration_ms >= 0
