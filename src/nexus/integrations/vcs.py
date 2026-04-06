"""VCS integrations: GitHub, GitLab, Bitbucket, Azure DevOps, Changelogs.md.

Environment variables:
  NEXUS_GITHUB_TOKEN    or  GITHUB_TOKEN   or  GH_TOKEN
  NEXUS_GITLAB_TOKEN    or  GITLAB_TOKEN
  NEXUS_BITBUCKET_TOKEN or  BITBUCKET_TOKEN
  NEXUS_AZURE_DEVOPS_TOKEN  or  AZURE_DEVOPS_TOKEN
  NEXUS_AZURE_ORG           (Azure DevOps org name, e.g. "mycompany")
  Changelogs.md requires no key.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from nexus.integrations.base import _get_env, _http_get, _qs

logger = logging.getLogger("nexus.integrations.vcs")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _detect_repo_slug(project_root: Path) -> tuple[str, str] | None:
    """Auto-detect owner/repo from git remote URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        # Handle: https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
        m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return None


def _detect_vcs_platform(project_root: Path) -> str | None:
    """Detect the VCS platform from the git remote URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip().lower()
        if "github" in url:
            return "github"
        if "gitlab" in url:
            return "gitlab"
        if "bitbucket" in url:
            return "bitbucket"
        if "dev.azure" in url or "visualstudio" in url:
            return "azure"
    except Exception:
        pass
    return None


# ─── GitHub ───────────────────────────────────────────────────────────────────

def github_get_repo(owner: str, repo: str) -> dict | None:
    """Fetch GitHub repository metadata."""
    token = _get_env("NEXUS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _http_get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)


def github_get_recent_commits(owner: str, repo: str, limit: int = 10) -> list[dict]:
    """Fetch recent commits from GitHub."""
    token = _get_env("NEXUS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = _http_get(
        f"https://api.github.com/repos/{owner}/{repo}/commits?per_page={limit}",
        headers=headers,
    )
    if not resp or not isinstance(resp, list):
        return []
    return [
        {
            "sha": c.get("sha", "")[:8],
            "message": (c.get("commit", {}).get("message", "") or "").split("\n")[0][:100],
            "author": c.get("commit", {}).get("author", {}).get("name", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
        }
        for c in resp
    ]


def github_get_open_issues(owner: str, repo: str, limit: int = 10) -> list[dict]:
    """Fetch open issues and PRs from GitHub."""
    token = _get_env("NEXUS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = _http_get(
        f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page={limit}",
        headers=headers,
    )
    if not resp or not isinstance(resp, list):
        return []
    return [
        {
            "number": i.get("number"),
            "title": i.get("title", "")[:100],
            "type": "PR" if i.get("pull_request") else "Issue",
            "labels": [lb.get("name") for lb in i.get("labels", [])],
            "created_at": i.get("created_at", ""),
        }
        for i in resp
    ]


def github_get_workflow_runs(owner: str, repo: str) -> list[dict]:
    """Fetch recent GitHub Actions workflow runs."""
    token = _get_env("NEXUS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    if not token:
        return []
    resp = _http_get(
        f"https://api.github.com/repos/{owner}/{repo}/actions/runs?per_page=5",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if not resp:
        return []
    return [
        {
            "name": r.get("name", ""),
            "status": r.get("status", ""),
            "conclusion": r.get("conclusion", ""),
            "branch": r.get("head_branch", ""),
            "created_at": r.get("created_at", ""),
        }
        for r in resp.get("workflow_runs", [])
    ]


# ─── GitLab ───────────────────────────────────────────────────────────────────

def gitlab_get_project(project_path: str) -> dict | None:
    """Fetch GitLab project metadata. project_path = 'owner/repo' URL-encoded."""
    token = _get_env("NEXUS_GITLAB_TOKEN", "GITLAB_TOKEN")
    import urllib.parse
    encoded = urllib.parse.quote_plus(project_path)
    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token
    return _http_get(
        f"https://gitlab.com/api/v4/projects/{encoded}",
        headers=headers,
    )


def gitlab_get_pipelines(project_path: str, limit: int = 5) -> list[dict]:
    """Fetch recent GitLab CI pipelines."""
    token = _get_env("NEXUS_GITLAB_TOKEN", "GITLAB_TOKEN")
    import urllib.parse
    encoded = urllib.parse.quote_plus(project_path)
    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token
    resp = _http_get(
        f"https://gitlab.com/api/v4/projects/{encoded}/pipelines?per_page={limit}",
        headers=headers,
    )
    if not resp or not isinstance(resp, list):
        return []
    return [
        {
            "id": p.get("id"),
            "status": p.get("status"),
            "ref": p.get("ref"),
            "created_at": p.get("created_at"),
        }
        for p in resp
    ]


# ─── Bitbucket ────────────────────────────────────────────────────────────────

def bitbucket_get_repo(workspace: str, slug: str) -> dict | None:
    """Fetch Bitbucket repository metadata."""
    token = _get_env("NEXUS_BITBUCKET_TOKEN", "BITBUCKET_TOKEN")
    headers = {}
    if token:
        import base64
        # Token can be "user:app_password" or just an app password
        if ":" in token:
            headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
        else:
            headers["Authorization"] = f"Bearer {token}"
    return _http_get(
        f"https://api.bitbucket.org/2.0/repositories/{workspace}/{slug}",
        headers=headers,
    )


def bitbucket_get_pipelines(workspace: str, slug: str, limit: int = 5) -> list[dict]:
    """Fetch recent Bitbucket Pipelines."""
    token = _get_env("NEXUS_BITBUCKET_TOKEN", "BITBUCKET_TOKEN")
    headers = {}
    if token:
        import base64
        if ":" in token:
            headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
        else:
            headers["Authorization"] = f"Bearer {token}"
    resp = _http_get(
        f"https://api.bitbucket.org/2.0/repositories/{workspace}/{slug}/pipelines/"
        f"?sort=-created_on&pagelen={limit}",
        headers=headers,
    )
    if not resp:
        return []
    return [
        {
            "id": p.get("build_number"),
            "status": p.get("state", {}).get("result", {}).get("name", ""),
            "target": p.get("target", {}).get("ref_name", ""),
            "created_on": p.get("created_on", ""),
        }
        for p in resp.get("values", [])
    ]


# ─── Azure DevOps ─────────────────────────────────────────────────────────────

def azure_get_builds(org: str, project: str, limit: int = 5) -> list[dict]:
    """Fetch recent Azure DevOps builds."""
    token = _get_env("NEXUS_AZURE_DEVOPS_TOKEN", "AZURE_DEVOPS_TOKEN")
    if not token:
        return []
    import base64
    auth = base64.b64encode(f":{token}".encode()).decode()
    resp = _http_get(
        f"https://dev.azure.com/{org}/{project}/_apis/build/builds?api-version=7.0&$top={limit}",
        headers={"Authorization": f"Basic {auth}"},
    )
    if not resp:
        return []
    return [
        {
            "id": b.get("id"),
            "status": b.get("status"),
            "result": b.get("result"),
            "branch": b.get("sourceBranch", "").replace("refs/heads/", ""),
            "queue_time": b.get("queueTime", ""),
        }
        for b in resp.get("value", [])
    ]


def azure_get_work_items(org: str, project: str, limit: int = 10) -> list[dict]:
    """Fetch recent Azure DevOps work items (bugs, tasks)."""
    token = _get_env("NEXUS_AZURE_DEVOPS_TOKEN", "AZURE_DEVOPS_TOKEN")
    if not token:
        return []
    import base64
    auth = base64.b64encode(f":{token}".encode()).decode()
    wiql = {"query": f"SELECT [Id],[Title],[State],[WorkItemType] FROM workitems WHERE [System.TeamProject]='{project}' ORDER BY [System.ChangedDate] DESC"}
    resp = _http_post(
        f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.0&$top={limit}",
        payload=wiql,
        headers={"Authorization": f"Basic {auth}"},
    )
    if not resp:
        return []
    items = []
    for wi in resp.get("workItems", [])[:limit]:
        items.append({"id": wi.get("id"), "url": wi.get("url", "")})
    return items


# ─── Changelogs.md ────────────────────────────────────────────────────────────

def changelogs_get(owner: str, repo: str) -> str | None:
    """Fetch structured changelog from changelogs.md for a GitHub repo."""
    resp = _http_get(f"https://changelogs.md/github/{owner}/{repo}/raw")
    if isinstance(resp, str):
        return resp[:3000]
    # The API may return text/plain — try without JSON parsing
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"https://changelogs.md/github/{owner}/{repo}/raw", timeout=10
        ) as r:
            return r.read().decode("utf-8", errors="replace")[:3000]
    except Exception:
        return None


# ─── Combined VCS summary ────────────────────────────────────────────────────

def get_vcs_summary(project_root: Path) -> dict[str, Any]:
    """Auto-detect VCS platform and fetch recent activity."""
    slug = _detect_repo_slug(project_root)
    if not slug:
        return {"error": "Could not detect git remote"}

    owner, repo = slug
    platform = _detect_vcs_platform(project_root)

    summary: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "platform": platform,
        "commits": [],
        "issues": [],
        "ci_runs": [],
        "changelog": None,
    }

    if platform == "github":
        summary["commits"] = github_get_recent_commits(owner, repo)
        summary["issues"] = github_get_open_issues(owner, repo)
        summary["ci_runs"] = github_get_workflow_runs(owner, repo)
        summary["changelog"] = changelogs_get(owner, repo)
    elif platform == "gitlab":
        summary["ci_runs"] = gitlab_get_pipelines(f"{owner}/{repo}")
    elif platform == "bitbucket":
        summary["ci_runs"] = bitbucket_get_pipelines(owner, repo)

    return summary


def format_vcs_summary(summary: dict[str, Any]) -> str:
    """Format VCS summary for MCP output."""
    if "error" in summary:
        return f"VCS: {summary['error']}"

    platform = summary.get("platform", "unknown")
    owner, repo = summary.get("owner", ""), summary.get("repo", "")
    lines = [f"## VCS Summary: {owner}/{repo} ({platform})"]

    commits = summary.get("commits", [])
    if commits:
        lines.append("\n### Recent Commits")
        for c in commits[:5]:
            lines.append(f"  [{c['sha']}] {c['message']} — {c['author']}")

    issues = summary.get("issues", [])
    if issues:
        open_issues = [i for i in issues if i["type"] == "Issue"]
        open_prs = [i for i in issues if i["type"] == "PR"]
        if open_issues:
            lines.append(f"\n### Open Issues ({len(open_issues)})")
            for i in open_issues[:5]:
                labels = f" [{', '.join(i['labels'])}]" if i["labels"] else ""
                lines.append(f"  #{i['number']}{labels}: {i['title']}")
        if open_prs:
            lines.append(f"\n### Open PRs ({len(open_prs)})")
            for pr in open_prs[:5]:
                lines.append(f"  #{pr['number']}: {pr['title']}")

    runs = summary.get("ci_runs", [])
    if runs:
        lines.append("\n### Recent CI Runs")
        for r in runs[:3]:
            conclusion = r.get("conclusion") or r.get("result") or r.get("status") or "?"
            branch = r.get("branch") or r.get("ref") or r.get("target") or "?"
            name = r.get("name") or ""
            lines.append(f"  {name} [{branch}]: {conclusion}")

    return "\n".join(lines)
