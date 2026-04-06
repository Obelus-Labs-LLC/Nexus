"""External API integration tools for Nexus MCP.

Tools exposed:
  nexus_integrations  — list configured integrations and their status
  nexus_security      — run a security scan (secrets, CVEs, OSV vulns)
  nexus_vcs           — VCS summary (commits, issues, CI runs)
  nexus_ci            — CI build status across providers
  nexus_packages      — npm / PyPI / CDNJS package lookup
  nexus_news          — tech news from configured providers
  nexus_nlp           — NLP analysis (sentiment, keywords, Wolfram Q&A)
  nexus_analytics     — Keen IO event counts / Wikidata entity lookup
"""

from __future__ import annotations

import logging
from pathlib import Path

from nexus.integrations.base import configured_integrations
from nexus.server.state import get_config, validate_path

logger = logging.getLogger("nexus.server.integrations")


def _extract_dep_names(project_root: Path) -> list[tuple[str, str, str]]:
    """Parse dependency names from common package manifest files.

    Returns list of (name, version, ecosystem) tuples.
    Supports: requirements.txt, pyproject.toml, package.json, Cargo.toml.
    """
    deps: list[tuple[str, str, str]] = []

    # requirements.txt
    req_file = project_root / "requirements.txt"
    if req_file.exists():
        try:
            for line in req_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # strip extras, version specifiers: requests[security]>=2.0
                import re
                m = re.match(r"^([A-Za-z0-9_.-]+)", line)
                if m:
                    deps.append((m.group(1), "", "PyPI"))
        except Exception:
            pass

    # pyproject.toml — [project] dependencies
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        try:
            import re
            text = pyproject.read_text(errors="replace")
            # Find dependencies = [...] block
            in_deps = False
            for line in text.splitlines():
                if "dependencies" in line and "=" in line:
                    in_deps = True
                if in_deps:
                    m = re.search(r'"([A-Za-z0-9_.-]+)', line)
                    if m:
                        deps.append((m.group(1), "", "PyPI"))
                    if "]" in line:
                        in_deps = False
        except Exception:
            pass

    # package.json
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            import json
            data = json.loads(pkg_json.read_text(errors="replace"))
            for section in ("dependencies", "devDependencies"):
                for name in (data.get(section) or {}).keys():
                    deps.append((name, "", "npm"))
        except Exception:
            pass

    # Cargo.toml — [dependencies]
    cargo = project_root / "Cargo.toml"
    if cargo.exists():
        try:
            import re
            in_deps = False
            for line in cargo.read_text(errors="replace").splitlines():
                if line.strip() == "[dependencies]":
                    in_deps = True
                    continue
                if in_deps and line.strip().startswith("["):
                    in_deps = False
                if in_deps:
                    m = re.match(r'^([A-Za-z0-9_-]+)\s*=', line)
                    if m:
                        deps.append((m.group(1), "", "crates.io"))
        except Exception:
            pass

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for name, version, eco in deps:
        if name not in seen:
            seen.add(name)
            unique.append((name, version, eco))

    return unique[:50]


def register(mcp):
    """Register integration tools with the MCP server."""

    @mcp.tool()
    def nexus_integrations(project: str = "") -> str:
        """List all available external API integrations and their configuration status.

        Returns a table of integration names, categories, and whether they are
        configured (credentials set) or available without a key.

        Args:
            project: Optional project path (used to detect VCS platform).
        """
        configured = configured_integrations()

        categories = {
            "Security": ["GitGuardian", "NVD", "OSV", "VirusTotal", "Snyk"],
            "VCS": ["GitHub", "GitLab", "Bitbucket", "AzureDevOps"],
            "CI": ["CircleCI", "TravisCI", "Bitrise", "Buddy", "Codeship"],
            "Packages": ["npm", "PyPI", "CDNJS", "jsDelivr", "APIsGuru"],
            "NLP": ["NLPCloud", "Datamuse", "WolframAlpha"],
            "News": ["NewsAPI", "GNews", "Currents", "MarketAux"],
            "Analytics": ["KeenIO", "TimeDoor"],
            "Knowledge": ["Wikidata", "ChangelogsMD"],
        }

        lines = ["## Nexus Integrations Status\n"]
        ready_count = 0
        total_count = 0

        for category, names in categories.items():
            lines.append(f"### {category}")
            for name in names:
                status = configured.get(name)
                if status is True:
                    icon = "✓"
                    label = "ready"
                    ready_count += 1
                elif status is False:
                    icon = "○"
                    label = "needs key"
                else:
                    icon = "—"
                    label = "unknown"
                total_count += 1
                lines.append(f"  {icon} {name:<18} {label}")

        lines.append(f"\n{ready_count}/{total_count} integrations ready.")
        return "\n".join(lines)

    @mcp.tool()
    def nexus_security(
        project: str,
        deep: bool = False,
    ) -> str:
        """Run a security scan on the project.

        Checks for exposed secrets (GitGuardian), dependency vulnerabilities
        (OSV, NVD), and optionally binary malware (VirusTotal with deep=True).

        Results are informational — no files are modified.

        Args:
            project: Absolute path to the project root.
            deep: If true, also scan binaries/executables via VirusTotal.
        """
        project_path = validate_path(project)

        from nexus.integrations.security import (
            format_security_report,
            run_security_scan,
        )

        # Gather dep names from the project's package files
        dep_names = _extract_dep_names(project_path)

        report = run_security_scan(project_path, dep_names, deep=deep)
        project_name = project_path.name
        return format_security_report(report, project_name)

    @mcp.tool()
    def nexus_vcs(project: str) -> str:
        """Fetch VCS summary for the project: recent commits, open issues, CI runs.

        Auto-detects platform from git remote URL (GitHub, GitLab, Bitbucket,
        Azure DevOps). Requires the appropriate token to be set.

        Args:
            project: Absolute path to the project root.
        """
        project_path = validate_path(project)

        from nexus.integrations.vcs import format_vcs_summary, get_vcs_summary

        summary = get_vcs_summary(project_path)
        return format_vcs_summary(summary)

    @mcp.tool()
    def nexus_ci(project: str) -> str:
        """Fetch CI build status from all configured providers.

        Checks CircleCI, Travis CI, Bitrise, Buddy, and Codeship. Only providers
        with credentials set will be queried.

        Args:
            project: Absolute path to the project root.
        """
        project_path = validate_path(project)

        from nexus.integrations.ci import format_ci_summary, get_ci_summary

        summary = get_ci_summary(project_path)
        return format_ci_summary(summary)

    @mcp.tool()
    def nexus_packages(
        names: str,
        ecosystem: str = "auto",
    ) -> str:
        """Look up package metadata from npm, PyPI, and CDNJS.

        Fetches version, description, license, and dependency list.
        No API key required for any of these registries.

        Args:
            names: Comma-separated list of package names.
            ecosystem: "auto" (try both npm + PyPI), "npm", or "pypi".
        """
        pkg_names = [n.strip() for n in names.split(",") if n.strip()]
        if not pkg_names:
            return "No package names provided."

        from nexus.integrations.packages import format_package_info, get_package_info

        results = []
        for name in pkg_names[:10]:
            info = get_package_info(name, ecosystem)
            results.append(format_package_info(info))

        return "\n\n".join(results)

    @mcp.tool()
    def nexus_news(
        query: str = "software development",
        limit: int = 5,
    ) -> str:
        """Fetch recent tech news from configured news APIs.

        Queries NewsAPI, GNews, Currents, and/or MarketAux depending on which
        API keys are configured.

        Args:
            query: Search query string (default: "software development").
            limit: Maximum articles per source (default: 5).
        """
        from nexus.integrations.news import format_news_feed, get_tech_news

        feed = get_tech_news(query=query, limit=limit)
        return format_news_feed(feed)

    @mcp.tool()
    def nexus_nlp(
        text: str,
        mode: str = "analyze",
        wolfram_query: str = "",
    ) -> str:
        """Run NLP analysis or ask WolframAlpha a computational question.

        Modes:
          analyze   — sentiment + keyword extraction (NLP Cloud)
          summarize — summarize a long text (NLP Cloud)
          classify  — classify text intent / topic
          wolfram   — get a factual/computational answer from WolframAlpha

        Args:
            text: Text to analyze (or question context).
            mode: One of "analyze", "summarize", "classify", "wolfram".
            wolfram_query: Question to ask WolframAlpha (used when mode="wolfram").
        """
        from nexus.integrations.nlp import (
            analyze_text,
            format_nlp_result,
            nlpcloud_summarize,
            wolfram_short_answer,
        )

        if mode == "wolfram" or wolfram_query:
            q = wolfram_query or text
            answer = wolfram_short_answer(q)
            if answer:
                return f"**Wolfram:** {answer}"
            return "WolframAlpha not configured. Set NEXUS_WOLFRAM_APPID."

        if mode == "summarize":
            summary = nlpcloud_summarize(text)
            if summary:
                return f"**Summary:** {summary}"
            return "NLP Cloud not configured. Set NEXUS_NLPCLOUD_KEY."

        # Default: analyze
        result = analyze_text(text)
        return format_nlp_result(result)

    @mcp.tool()
    def nexus_ext_analytics(
        mode: str = "status",
        collection: str = "",
        wikidata_query: str = "",
    ) -> str:
        """Fetch external analytics data or look up knowledge from Wikidata.

        Modes:
          status   — show Keen IO event counts for a collection
          wikidata — search or look up an entity on Wikidata
          sparql   — run a SPARQL query against Wikidata (wikidata_query used as SPARQL)

        Args:
            mode: One of "status", "wikidata", "sparql".
            collection: Keen IO event collection name (used when mode="status").
            wikidata_query: Entity name or SPARQL string (used when mode="wikidata"/"sparql").
        """
        from nexus.integrations.analytics import (
            format_analytics_summary,
            get_analytics_summary,
            wikidata_get_software,
            wikidata_search,
            wikidata_sparql,
        )

        if mode == "wikidata":
            q = wikidata_query or collection
            if not q:
                return "Provide wikidata_query= to search Wikidata."
            # Try software-specific lookup first
            result = wikidata_get_software(q)
            if result:
                lines = [f"## Wikidata: {result.get('label', q)}"]
                if result.get("description"):
                    lines.append(f"  {result['description']}")
                lines.append(f"  URL: {result.get('wikidata_url', '')}")
                return "\n".join(lines)
            # Fall back to generic search
            entities = wikidata_search(q, limit=5)
            if not entities:
                return f"No Wikidata results for: {q}"
            lines = [f"## Wikidata Search: {q}"]
            for e in entities:
                lines.append(f"  [{e['id']}] {e['label']} — {e['description']}")
            return "\n".join(lines)

        if mode == "sparql":
            sparql = wikidata_query
            if not sparql:
                return "Provide wikidata_query= with a SPARQL query string."
            rows = wikidata_sparql(sparql, limit=10)
            if not rows:
                return "No SPARQL results."
            lines = ["## SPARQL Results"]
            for row in rows:
                lines.append("  " + " | ".join(f"{k}: {v}" for k, v in row.items()))
            return "\n".join(lines)

        # Default: status
        summary = get_analytics_summary(collection or None)
        return format_analytics_summary(summary)
