"""Parser plugin system for extending language support.

Plugins are Python modules that register extractors for additional languages.
They can be loaded from:
  1. Built-in plugins (nexus.index.plugins_builtin)
  2. User plugins directory (~/.nexus/plugins/)
  3. Project-local plugins (.nexus/plugins/)

Each plugin module must define:
  - LANGUAGE: str — the language identifier (e.g., "go")
  - GRAMMAR: str — the tree-sitter grammar name
  - extract(root, source, path) -> ParseResult
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

from nexus.index.parser import ParseResult

# Type for extractor functions
ExtractorFn = Callable[[Any, bytes, Path], ParseResult]

# Global plugin registry
_plugins: dict[str, dict[str, Any]] = {}


def register_plugin(
    language: str,
    grammar: str,
    extractor: ExtractorFn,
    name: str = "",
) -> None:
    """Register a language parser plugin."""
    _plugins[language] = {
        "grammar": grammar,
        "extractor": extractor,
        "name": name or language,
    }


def get_plugin(language: str) -> dict[str, Any] | None:
    """Get a registered plugin for a language."""
    return _plugins.get(language)


def list_plugins() -> dict[str, str]:
    """List all registered plugins."""
    return {lang: info["name"] for lang, info in _plugins.items()}


def load_builtin_plugins() -> int:
    """Load built-in language plugins. Returns count loaded."""
    loaded = 0
    try:
        from nexus.index import plugins_builtin
        for name in dir(plugins_builtin):
            mod = getattr(plugins_builtin, name)
            if hasattr(mod, "LANGUAGE") and hasattr(mod, "extract"):
                register_plugin(
                    language=mod.LANGUAGE,
                    grammar=mod.GRAMMAR,
                    extractor=mod.extract,
                    name=getattr(mod, "NAME", mod.LANGUAGE),
                )
                loaded += 1
    except ImportError:
        pass
    return loaded


def load_plugins_from_dir(plugin_dir: Path) -> int:
    """Load plugins from a directory. Each .py file is a plugin module."""
    loaded = 0
    if not plugin_dir.is_dir():
        return 0

    for py_file in plugin_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"nexus_plugin_{py_file.stem}", py_file
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "LANGUAGE") and hasattr(mod, "extract"):
                    register_plugin(
                        language=mod.LANGUAGE,
                        grammar=mod.GRAMMAR,
                        extractor=mod.extract,
                        name=getattr(mod, "NAME", mod.LANGUAGE),
                    )
                    loaded += 1
        except Exception as e:
            print(f"[nexus] plugin load error {py_file.name}: {e}", file=sys.stderr)

    return loaded


def load_all_plugins() -> int:
    """Load plugins from all sources."""
    loaded = load_builtin_plugins()

    # User plugins
    user_dir = Path.home() / ".nexus" / "plugins"
    loaded += load_plugins_from_dir(user_dir)

    return loaded
