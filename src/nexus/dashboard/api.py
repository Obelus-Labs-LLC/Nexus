"""Dashboard HTTP API and static file server."""

from __future__ import annotations

import json
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from nexus.store.db import NexusDB
from nexus.util.config import load_config

_STATIC_DIR = Path(__file__).parent
_NEXUS_ROOT = Path(__file__).parent.parent.parent.parent


def _get_all_projects() -> dict[str, dict[str, Any]]:
    """Load all project stats from the registry."""
    config_path = _NEXUS_ROOT / "nexus.toml"
    if not config_path.exists():
        return {}

    projects = load_config(config_path)
    result = {}

    for name, cfg in projects.items():
        if not cfg.db_path.exists():
            result[name] = {
                "name": name,
                "root": str(cfg.root),
                "languages": cfg.languages,
                "cluster": cfg.cluster,
                "indexed": False,
            }
            continue

        try:
            db = NexusDB(cfg.db_path)
            stats = db.get_stats()

            with db.connect() as conn:
                lang_breakdown = conn.execute(
                    "SELECT language, COUNT(*) as c FROM files "
                    "WHERE language IS NOT NULL GROUP BY language"
                ).fetchall()

                last_scan = conn.execute(
                    "SELECT * FROM scan_meta ORDER BY started_at DESC LIMIT 1"
                ).fetchone()

                kind_breakdown = conn.execute(
                    "SELECT kind, COUNT(*) as c FROM symbols GROUP BY kind ORDER BY c DESC"
                ).fetchall()

                edge_breakdown = conn.execute(
                    "SELECT kind, COUNT(*) as c FROM edges GROUP BY kind ORDER BY c DESC"
                ).fetchall()

                total_lines = conn.execute(
                    "SELECT SUM(line_count) as total FROM files"
                ).fetchone()

                total_bytes = conn.execute(
                    "SELECT SUM(byte_size) as total FROM files"
                ).fetchone()

                unresolved = conn.execute(
                    "SELECT COUNT(*) as c FROM unresolved_imports"
                ).fetchone()

                recent_sessions = conn.execute(
                    "SELECT COUNT(DISTINCT session_id) as c FROM session_actions "
                    "WHERE timestamp > ?", (time.time() - 7 * 86400,)
                ).fetchone()

                active_decisions = conn.execute(
                    "SELECT COUNT(*) as c FROM decisions WHERE expires_at > ?",
                    (time.time(),),
                ).fetchone()

                # Top 10 symbols by edge count (most connected)
                try:
                    top_symbols = conn.execute(
                        """SELECT s.name, s.kind, s.qualified, f.path,
                                  (SELECT COUNT(*) FROM edges e WHERE e.source_id = s.id OR e.target_id = s.id) as edge_count
                           FROM symbols s JOIN files f ON s.file_id = f.id
                           ORDER BY edge_count DESC LIMIT 10"""
                    ).fetchall()
                except Exception:
                    top_symbols = []

            result[name] = {
                "name": name,
                "root": str(cfg.root),
                "languages": cfg.languages,
                "cluster": cfg.cluster,
                "indexed": True,
                "files": stats["files"],
                "symbols": stats["symbols"],
                "edges": stats["edges"],
                "total_lines": total_lines["total"] or 0,
                "total_bytes": total_bytes["total"] or 0,
                "unresolved_imports": unresolved["c"],
                "language_breakdown": {r["language"]: r["c"] for r in lang_breakdown},
                "symbol_kinds": {r["kind"]: r["c"] for r in kind_breakdown},
                "edge_kinds": {r["kind"]: r["c"] for r in edge_breakdown},
                "last_scan_ms": last_scan["duration_ms"] if last_scan else None,
                "recent_sessions_7d": recent_sessions["c"],
                "active_decisions": active_decisions["c"],
                "top_symbols": [
                    {"name": s["name"], "kind": s["kind"], "qualified": s["qualified"],
                     "file": s["path"], "edges": s["edge_count"]}
                    for s in top_symbols
                ],
            }
            db.close()
        except Exception as e:
            result[name] = {
                "name": name,
                "root": str(cfg.root),
                "languages": cfg.languages,
                "cluster": cfg.cluster,
                "indexed": False,
                "error": str(e),
            }

    return result


def _get_cluster_edges() -> dict[str, list[dict]]:
    """Get cross-project edges for all clusters."""
    config_path = _NEXUS_ROOT / "nexus.toml"
    if not config_path.exists():
        return {}

    projects = load_config(config_path)
    clusters: dict[str, set[str]] = {}
    for name, cfg in projects.items():
        if cfg.cluster and cfg.cross_project:
            clusters.setdefault(cfg.cluster, set()).add(name)

    result = {}
    for cluster, members in clusters.items():
        edges = []
        for name in members:
            cfg = projects.get(name)
            if not cfg or not cfg.db_path.exists():
                continue
            db = NexusDB(cfg.db_path)
            try:
                with db.connect() as conn:
                    table_check = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='cross_project_edges'"
                    ).fetchone()
                    if table_check:
                        rows = conn.execute(
                            "SELECT source_project, target_project, COUNT(*) as c "
                            "FROM cross_project_edges GROUP BY source_project, target_project"
                        ).fetchall()
                        for r in rows:
                            edges.append({
                                "source": r["source_project"],
                                "target": r["target_project"],
                                "count": r["c"],
                            })
            except Exception:
                pass
            db.close()
        result[cluster] = edges

    return result


def _get_tuning_report() -> dict[str, Any]:
    """Get auto-tuning analysis for the current project."""
    config_path = _NEXUS_ROOT / "nexus.toml"
    if not config_path.exists():
        return {"error": "No nexus.toml"}

    projects = load_config(config_path)
    reports = {}

    for name, cfg in projects.items():
        if not cfg.db_path.exists():
            continue
        try:
            from nexus.rank.tuner import analyze_and_tune
            db = NexusDB(cfg.db_path)
            result = analyze_and_tune(db, days=30)
            reports[name] = {
                "queries_analyzed": result.queries_analyzed,
                "relevant_files": result.relevant_files_found,
                "avg_rank": result.avg_relevant_rank,
                "confidence": result.confidence,
                "boosts": result.recommended_boosts,
                "rrf_weights": result.recommended_rrf_weights,
                "reasoning": result.reasoning,
            }
            db.close()
        except Exception as e:
            reports[name] = {"error": str(e)}

    return reports


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        kwargs["directory"] = str(_STATIC_DIR)
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # API routes — always return JSON, never fall through to file serving
        if path == "/api/projects":
            self._json_response(_get_all_projects())
            return
        elif path == "/api/clusters":
            self._json_response(_get_cluster_edges())
            return
        elif path == "/api/tuning":
            self._json_response(_get_tuning_report())
            return
        elif path.startswith("/api/"):
            self._json_response({"error": f"Unknown API endpoint: {path}"})
            return

        # Serve index.html for root
        if path == "" or path == "/index.html":
            self._serve_html()
            return

        # Everything else: 404
        self.send_error(404, f"Not found: {path}")

    def _serve_html(self):
        html_path = _STATIC_DIR / "index.html"
        try:
            body = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, str(e))

    def _json_response(self, data: Any):
        try:
            body = json.dumps(data, default=str).encode()
        except Exception as e:
            body = json.dumps({"error": f"Serialization error: {e}"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def serve(port: int = 7420):
    import sys
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    # Print to both stdout and stderr so preview systems detect the port
    msg = f"Nexus Dashboard: http://127.0.0.1:{port}"
    print(msg, flush=True)
    print(msg, file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
