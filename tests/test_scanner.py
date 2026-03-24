"""Tests for the file scanner."""

from nexus.index.scanner import scan_project, is_generated


def test_scan_finds_python_files(project_config, db):
    result = scan_project(project_config, db)
    assert result.files_total >= 2  # main.py, app.py (migrations may be included)
    assert result.files_new >= 2
    assert result.files_changed == 0
    assert result.duration_ms >= 0


def test_scan_detects_unchanged_on_rescan(project_config, db):
    scan_project(project_config, db)
    result = scan_project(project_config, db)
    assert result.files_unchanged >= 2
    assert result.files_new == 0
    assert result.files_changed == 0


def test_scan_detects_changed_file(project_config, db):
    scan_project(project_config, db)

    # Modify a file
    (project_config.root / "main.py").write_text("def changed(): pass\n")

    result = scan_project(project_config, db)
    assert result.files_changed >= 1


def test_scan_detects_deleted_file(project_config, db):
    scan_project(project_config, db)

    # Delete a file
    (project_config.root / "app.py").unlink()

    result = scan_project(project_config, db)
    assert result.files_deleted >= 1


def test_is_generated():
    assert is_generated("migrations/001_init.py")
    assert is_generated("foo_pb2.py")
    assert is_generated("vendor/lib.py")
    assert is_generated("src/types.d.ts")
    assert not is_generated("src/main.py")
    assert not is_generated("app/models.py")
