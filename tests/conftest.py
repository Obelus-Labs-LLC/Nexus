"""Shared test fixtures for Nexus."""

import tempfile
from pathlib import Path

import pytest

from nexus.store.db import NexusDB
from nexus.util.config import ProjectConfig


@pytest.fixture
def tmp_dir(tmp_path):
    """A temporary directory for test files."""
    return tmp_path


@pytest.fixture
def db(tmp_path):
    """A fresh Nexus database in a temp directory."""
    db_path = tmp_path / ".nexus" / "nexus.db"
    return NexusDB(db_path)


@pytest.fixture
def sample_project(tmp_path):
    """A minimal Python project for testing."""
    proj = tmp_path / "sample_project"
    proj.mkdir()

    # A simple Python file
    (proj / "main.py").write_text(
        'def hello(name: str) -> str:\n'
        '    """Greet someone."""\n'
        '    return f"Hello, {name}!"\n'
        '\n'
        'class Greeter:\n'
        '    """A greeter class."""\n'
        '    def greet(self, name: str) -> str:\n'
        '        return hello(name)\n'
    )

    # A file that imports from main
    (proj / "app.py").write_text(
        'from main import Greeter, hello\n'
        '\n'
        'def run():\n'
        '    g = Greeter()\n'
        '    print(g.greet("World"))\n'
    )

    # A generated file (should be tagged)
    migrations = proj / "migrations"
    migrations.mkdir()
    (migrations / "001_init.py").write_text(
        'def upgrade():\n'
        '    pass\n'
    )

    return proj


@pytest.fixture
def project_config(sample_project):
    """ProjectConfig for the sample project."""
    return ProjectConfig(
        name="test",
        root=sample_project,
        languages=["python"],
    )
