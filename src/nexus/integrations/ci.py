"""CI integrations: CircleCI, Travis CI, Bitrise, Buddy, Codeship.

Environment variables:
  NEXUS_CIRCLECI_TOKEN  or  CIRCLE_TOKEN
  NEXUS_TRAVIS_TOKEN    or  TRAVIS_TOKEN
  NEXUS_BITRISE_TOKEN   or  BITRISE_TOKEN
  NEXUS_BITRISE_APP_SLUG              (Bitrise app slug)
  NEXUS_BUDDY_TOKEN     or  BUDDY_TOKEN
  NEXUS_BUDDY_WORKSPACE               (Buddy workspace domain)
  NEXUS_CODESHIP_TOKEN  or  CODESHIP_TOKEN
  NEXUS_CODESHIP_ORG                  (Codeship organization slug)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from nexus.integrations.base import _get_env, _http_get

logger = logging.getLogger("nexus.integrations.ci")


def _detect_repo_slug(project_root: Path) -> tuple[str, str] | None:
    """Return (owner, repo) from git remote."""
    import re
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return None


# ─── CircleCI ─────────────────────────────────────────────────────────────────

def circleci_get_builds(owner: str, repo: str, vcs: str = "gh", limit: int = 5) -> list[dict]:
    """Fetch recent CircleCI builds for a repo."""
    token = _get_env("NEXUS_CIRCLECI_TOKEN", "CIRCLE_TOKEN")
    if not token:
        return []
    resp = _http_get(
        f"https://circleci.com/api/v1.1/project/{vcs}/{owner}/{repo}"
        f"?circle-token={token}&limit={limit}&shallow=true",
    )
    if not resp or not isinstance(resp, list):
        return []
    return [
        {
            "build_num": b.get("build_num"),
            "status": b.get("status"),
            "outcome": b.get("outcome"),
            "branch": b.get("branch"),
            "subject": (b.get("subject") or "")[:80],
            "started_at": b.get("start_time", ""),
        }
        for b in resp
    ]


def circleci_get_pipeline(owner: str, repo: str) -> dict | None:
    """Fetch latest CircleCI pipeline for a repo (API v2)."""
    token = _get_env("NEXUS_CIRCLECI_TOKEN", "CIRCLE_TOKEN")
    if not token:
        return None
    resp = _http_get(
        f"https://circleci.com/api/v2/project/gh/{owner}/{repo}/pipeline?per-page=1",
        headers={"Circle-Token": token},
    )
    if not resp:
        return None
    items = resp.get("items", [])
    return items[0] if items else None


# ─── Travis CI ────────────────────────────────────────────────────────────────

def travis_get_builds(owner: str, repo: str, limit: int = 5) -> list[dict]:
    """Fetch recent Travis CI builds."""
    token = _get_env("NEXUS_TRAVIS_TOKEN", "TRAVIS_TOKEN")
    slug = f"{owner}/{repo}"
    import urllib.parse
    encoded = urllib.parse.quote_plus(slug)
    headers = {"Travis-API-Version": "3"}
    if token:
        headers["Authorization"] = f"token {token}"
    resp = _http_get(
        f"https://api.travis-ci.com/repo/{encoded}/builds?limit={limit}",
        headers=headers,
    )
    if not resp:
        return []
    return [
        {
            "id": b.get("id"),
            "state": b.get("state"),
            "branch": b.get("branch", {}).get("name", ""),
            "commit": (b.get("commit", {}).get("message") or "")[:80].split("\n")[0],
            "started_at": b.get("started_at", ""),
        }
        for b in resp.get("builds", [])
    ]


# ─── Bitrise ──────────────────────────────────────────────────────────────────

def bitrise_get_builds(app_slug: str | None = None, limit: int = 5) -> list[dict]:
    """Fetch recent Bitrise builds."""
    token = _get_env("NEXUS_BITRISE_TOKEN", "BITRISE_TOKEN")
    if not token:
        return []
    slug = app_slug or _get_env("NEXUS_BITRISE_APP_SLUG") or ""
    if not slug:
        # List all apps and use first
        apps = _http_get(
            "https://api.bitrise.io/v0.1/apps?limit=1",
            headers={"Authorization": token},
        )
        if apps and apps.get("data"):
            slug = apps["data"][0].get("slug", "")
    if not slug:
        return []

    resp = _http_get(
        f"https://api.bitrise.io/v0.1/apps/{slug}/builds?limit={limit}",
        headers={"Authorization": token},
    )
    if not resp:
        return []
    return [
        {
            "build_number": b.get("build_number"),
            "status": b.get("status_text"),
            "branch": b.get("branch"),
            "commit_message": (b.get("commit_message") or "")[:80],
            "triggered_at": b.get("triggered_at", ""),
        }
        for b in resp.get("data", [])
    ]


# ─── Buddy ────────────────────────────────────────────────────────────────────

def buddy_get_pipelines(workspace: str | None = None, project: str | None = None) -> list[dict]:
    """Fetch Buddy pipelines."""
    token = _get_env("NEXUS_BUDDY_TOKEN", "BUDDY_TOKEN")
    if not token:
        return []
    ws = workspace or _get_env("NEXUS_BUDDY_WORKSPACE") or ""
    if not ws:
        return []
    url = f"https://api.buddy.works/workspaces/{ws}/projects"
    if project:
        url = f"https://api.buddy.works/workspaces/{ws}/projects/{project}/pipelines"
    resp = _http_get(url, headers={"Authorization": f"Bearer {token}"})
    if not resp:
        return []
    items = resp.get("pipelines") or resp.get("projects") or []
    return [
        {
            "id": p.get("id"),
            "name": p.get("name", ""),
            "status": p.get("last_execution_status", ""),
            "ref": p.get("refs", [{}])[0] if p.get("refs") else {},
        }
        for p in items[:5]
    ]


# ─── Codeship ─────────────────────────────────────────────────────────────────

def codeship_get_builds(org: str | None = None, limit: int = 5) -> list[dict]:
    """Fetch recent Codeship builds."""
    token = _get_env("NEXUS_CODESHIP_TOKEN", "CODESHIP_TOKEN")
    if not token:
        return []
    org_slug = org or _get_env("NEXUS_CODESHIP_ORG") or ""
    if not org_slug:
        return []
    resp = _http_get(
        f"https://codeship.com/api/v1/builds.json?api_key={token}&org_name={org_slug}&per_page={limit}",
    )
    if not resp:
        return []
    return [
        {
            "id": b.get("id"),
            "status": b.get("status"),
            "branch": b.get("branch"),
            "message": (b.get("message") or "")[:80],
            "finished_at": b.get("finished_at", ""),
        }
        for b in resp.get("builds", [])
    ]


# ─── Combined CI summary ──────────────────────────────────────────────────────

def get_ci_summary(project_root: Path) -> dict[str, Any]:
    """Collect CI status from all configured providers."""
    slug = _detect_repo_slug(project_root)
    owner, repo = slug if slug else ("", "")

    summary: dict[str, Any] = {"project": f"{owner}/{repo}" if owner else "unknown"}
    sources: list[str] = []

    if owner and repo:
        if _get_env("NEXUS_CIRCLECI_TOKEN", "CIRCLE_TOKEN"):
            builds = circleci_get_builds(owner, repo)
            if builds:
                summary["circleci"] = builds
                sources.append("CircleCI")

        if _get_env("NEXUS_TRAVIS_TOKEN", "TRAVIS_TOKEN"):
            builds = travis_get_builds(owner, repo)
            if builds:
                summary["travis"] = builds
                sources.append("TravisCI")

    if _get_env("NEXUS_BITRISE_TOKEN", "BITRISE_TOKEN"):
        builds = bitrise_get_builds()
        if builds:
            summary["bitrise"] = builds
            sources.append("Bitrise")

    if _get_env("NEXUS_BUDDY_TOKEN", "BUDDY_TOKEN"):
        pipelines = buddy_get_pipelines()
        if pipelines:
            summary["buddy"] = pipelines
            sources.append("Buddy")

    if _get_env("NEXUS_CODESHIP_TOKEN", "CODESHIP_TOKEN"):
        builds = codeship_get_builds()
        if builds:
            summary["codeship"] = builds
            sources.append("Codeship")

    summary["sources"] = sources
    return summary


def format_ci_summary(summary: dict[str, Any]) -> str:
    """Format CI summary for MCP output."""
    sources = summary.get("sources", [])
    project = summary.get("project", "")
    lines = [f"## CI Status: {project}"]

    if not sources:
        lines.append("No CI integrations configured.")
        lines.append("Set one of: NEXUS_CIRCLECI_TOKEN, NEXUS_TRAVIS_TOKEN, NEXUS_BITRISE_TOKEN, NEXUS_BUDDY_TOKEN, NEXUS_CODESHIP_TOKEN")
        return "\n".join(lines)

    lines.append(f"Sources: {', '.join(sources)}\n")

    def _fmt_builds(name: str, builds: list[dict]) -> None:
        lines.append(f"### {name}")
        for b in builds[:3]:
            status = b.get("status") or b.get("state") or b.get("outcome") or "?"
            branch = b.get("branch") or "?"
            msg = b.get("subject") or b.get("commit") or b.get("commit_message") or b.get("message") or ""
            icon = "✓" if any(s in status.lower() for s in ("success", "passed", "fixed")) else "✗" if any(s in status.lower() for s in ("fail", "error", "broken")) else "○"
            lines.append(f"  {icon} [{branch}] {status} — {msg[:60]}")

    for provider in ("circleci", "travis", "bitrise", "buddy", "codeship"):
        if provider in summary:
            _fmt_builds(provider.capitalize(), summary[provider])

    return "\n".join(lines)
