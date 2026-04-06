"""File system watcher — auto-triggers nexus_register_edit when files change.

Uses watchdog if installed (pip install watchdog), falls back to polling.
The watcher runs in a background thread and calls the provided callback
whenever source files are created or modified.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("nexus.watch")

# Global watcher state
_watcher_thread: threading.Thread | None = None
_watcher_stop: threading.Event | None = None
_watched_project: str | None = None


def is_running() -> bool:
    """Return True if a watcher is currently active."""
    return _watcher_thread is not None and _watcher_thread.is_alive()


def start_watcher(
    root: Path,
    extensions: set[str],
    on_change: Callable[[list[str]], None],
) -> str:
    """Start a background file watcher on the given project root.

    Args:
        root: Project root directory to watch.
        extensions: File extensions to monitor (e.g., {".py", ".rs"}).
        on_change: Callback called with a list of changed relative paths.

    Returns a string describing the watcher mode (watchdog or polling).
    """
    global _watcher_thread, _watcher_stop, _watched_project

    if is_running():
        stop_watcher()

    stop_event = threading.Event()
    _watcher_stop = stop_event
    _watched_project = str(root)

    # Try watchdog first
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

        class _NexusHandler(FileSystemEventHandler):
            def __init__(self):
                self._pending: set[str] = set()
                self._lock = threading.Lock()
                self._last_flush = time.time()

            def _enqueue(self, path: str) -> None:
                if not any(path.endswith(ext) for ext in extensions):
                    return
                try:
                    rel = str(Path(path).relative_to(root))
                    with self._lock:
                        self._pending.add(rel)
                except ValueError:
                    pass

            def on_created(self, event):
                if not event.is_directory:
                    self._enqueue(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    self._enqueue(event.src_path)

            def flush(self) -> list[str]:
                with self._lock:
                    paths = list(self._pending)
                    self._pending.clear()
                return paths

        handler = _NexusHandler()
        observer = Observer()
        observer.schedule(handler, str(root), recursive=True)
        observer.start()

        def _watchdog_loop():
            try:
                while not stop_event.is_set():
                    time.sleep(2.0)
                    changed = handler.flush()
                    if changed:
                        logger.debug("Watch: %d files changed", len(changed))
                        try:
                            on_change(changed)
                        except Exception as e:
                            logger.warning("Watch callback error: %s", e)
            finally:
                observer.stop()
                observer.join()

        _watcher_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="nexus-watcher")
        _watcher_thread.start()
        logger.info("Watch started (watchdog) on %s", root)
        return "watchdog"

    except ImportError:
        pass

    # Fallback: polling
    def _polling_loop():
        snapshots: dict[str, float] = {}

        def _snapshot() -> dict[str, float]:
            snap = {}
            for ext in extensions:
                for p in root.rglob(f"*{ext}"):
                    try:
                        snap[str(p.relative_to(root))] = p.stat().st_mtime
                    except (OSError, ValueError):
                        pass
            return snap

        snapshots = _snapshot()

        while not stop_event.is_set():
            time.sleep(3.0)
            current = _snapshot()
            changed = [
                path for path, mtime in current.items()
                if snapshots.get(path) != mtime
            ]
            if changed:
                logger.debug("Poll: %d files changed", len(changed))
                try:
                    on_change(changed)
                except Exception as e:
                    logger.warning("Watch callback error: %s", e)
            snapshots = current

    _watcher_thread = threading.Thread(target=_polling_loop, daemon=True, name="nexus-watcher")
    _watcher_thread.start()
    logger.info("Watch started (polling) on %s", root)
    return "polling"


def stop_watcher() -> None:
    """Stop the active file watcher."""
    global _watcher_thread, _watcher_stop, _watched_project
    if _watcher_stop:
        _watcher_stop.set()
    _watcher_thread = None
    _watcher_stop = None
    _watched_project = None
    logger.info("Watch stopped")
