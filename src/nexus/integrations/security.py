"""Security integrations: GitGuardian, NVD, OSV, VirusTotal, Snyk.

Environment variables:
  NEXUS_GITGUARDIAN_KEY  or  GITGUARDIAN_API_KEY
  NEXUS_VIRUSTOTAL_KEY   or  VIRUSTOTAL_API_KEY
  NEXUS_SNYK_TOKEN       or  SNYK_TOKEN
  NVD and OSV require no key.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from nexus.integrations.base import _get_env, _http_get, _http_post, _qs

logger = logging.getLogger("nexus.integrations.security")


# ─── GitGuardian ─────────────────────────────────────────────────────────────

def gitguardian_scan_files(file_contents: list[dict[str, str]]) -> list[dict]:
    """Scan files for secrets using GitGuardian.

    Args:
        file_contents: list of {"filename": ..., "document": <file text>}

    Returns list of incidents, each with: policy, match, filename.
    """
    key = _get_env("NEXUS_GITGUARDIAN_KEY", "GITGUARDIAN_API_KEY")
    if not key:
        return []

    results = []
    # GitGuardian /v1/scan accepts up to 20 documents per call
    for i in range(0, len(file_contents), 20):
        chunk = file_contents[i:i + 20]
        resp = _http_post(
            "https://api.gitguardian.com/v1/scan",
            payload={"documents": chunk},
            headers={"Authorization": f"Token {key}"},
        )
        if resp and resp.get("policy_break_count", 0) > 0:
            for doc in resp.get("results", []):
                for pb in doc.get("policy_breaks", []):
                    results.append({
                        "filename": doc.get("filename", ""),
                        "policy": pb.get("type", "unknown"),
                        "match": pb.get("match", ""),
                        "detector": pb.get("detector", {}).get("name", ""),
                    })

    return results


def gitguardian_scan_project(project_root: Path, max_files: int = 50) -> list[dict]:
    """Scan source files in a project root for secrets."""
    key = _get_env("NEXUS_GITGUARDIAN_KEY", "GITGUARDIAN_API_KEY")
    if not key:
        return []

    source_exts = {".py", ".ts", ".js", ".rs", ".go", ".java", ".env",
                   ".yaml", ".yml", ".toml", ".json", ".sh", ".bash", ".tf"}
    skip_dirs = {".git", ".nexus", "__pycache__", "node_modules", "target", ".venv", "venv"}

    docs = []
    for path in project_root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix not in source_exts or not path.is_file():
            continue
        try:
            rel = str(path.relative_to(project_root))
            text = path.read_text(encoding="utf-8", errors="replace")[:50000]
            docs.append({"filename": rel, "document": text})
        except Exception:
            continue
        if len(docs) >= max_files:
            break

    return gitguardian_scan_files(docs)


# ─── NVD (National Vulnerability Database) ───────────────────────────────────

def nvd_search_cves(keyword: str, max_results: int = 5) -> list[dict]:
    """Search NVD for CVEs matching a package/library name.

    No API key required. Returns list of CVEs with id, description, severity.
    """
    url = "https://services.nvd.nist.gov/rest/json/cves/2.0?" + _qs({
        "keywordSearch": keyword,
        "resultsPerPage": max_results,
    })
    resp = _http_get(url, timeout=15)
    if not resp:
        return []

    results = []
    for item in resp.get("vulnerabilities", []):
        cve = item.get("cve", {})
        cve_id = cve.get("id", "")
        descriptions = cve.get("descriptions", [])
        desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")
        metrics = cve.get("metrics", {})
        severity = "UNKNOWN"
        for metric_key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            m_list = metrics.get(metric_key, [])
            if m_list:
                severity = m_list[0].get("cvssData", {}).get("baseSeverity", "UNKNOWN")
                break

        results.append({
            "id": cve_id,
            "description": desc[:200],
            "severity": severity,
        })

    return results


def nvd_check_packages(package_names: list[str]) -> dict[str, list[dict]]:
    """Check multiple package names against NVD. Returns {package: [cves]}."""
    results = {}
    for pkg in package_names[:20]:  # Limit to avoid flooding NVD
        cves = nvd_search_cves(pkg, max_results=3)
        if cves:
            results[pkg] = cves
    return results


# ─── OSV (Open Source Vulnerability Database) ────────────────────────────────

def osv_check_package(name: str, version: str = "", ecosystem: str = "PyPI") -> list[dict]:
    """Query OSV for vulnerabilities in a package.

    No API key required. ecosystem examples: PyPI, npm, crates.io, Go, Maven.
    """
    payload: dict[str, Any] = {"package": {"name": name, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version

    resp = _http_post("https://api.osv.dev/v1/query", payload=payload)
    if not resp:
        return []

    vulns = []
    for v in resp.get("vulns", [])[:5]:
        vuln_id = v.get("id", "")
        summary = v.get("summary", v.get("details", "")[:150])
        severity = "UNKNOWN"
        for sev in v.get("severity", []):
            if sev.get("type") == "CVSS_V3":
                score = float(sev.get("score", "0.0").split("/")[0].split(":")[-1] or 0)
                severity = "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW"
                break
        aliases = v.get("aliases", [])
        cve = next((a for a in aliases if a.startswith("CVE-")), "")
        vulns.append({
            "id": vuln_id,
            "cve": cve,
            "summary": summary,
            "severity": severity,
        })

    return vulns


def osv_check_packages(packages: list[tuple[str, str, str]]) -> dict[str, list[dict]]:
    """Batch check packages against OSV.

    Args:
        packages: list of (name, version, ecosystem) tuples.
                  version can be empty string for latest.
    """
    results = {}
    for name, version, ecosystem in packages[:30]:
        vulns = osv_check_package(name, version, ecosystem)
        if vulns:
            results[name] = vulns
    return results


# ─── VirusTotal ───────────────────────────────────────────────────────────────

def virustotal_check_file(file_path: Path) -> dict | None:
    """Check a file's SHA256 hash against VirusTotal.

    Returns scan result dict or None if not found / not configured.
    """
    key = _get_env("NEXUS_VIRUSTOTAL_KEY", "VIRUSTOTAL_API_KEY")
    if not key:
        return None

    try:
        sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
    except Exception:
        return None

    resp = _http_get(
        f"https://www.virustotal.com/vtapi/v2/file/report?apikey={key}&resource={sha256}",
        cache=False,
    )
    if not resp or resp.get("response_code") == 0:
        return None  # Not found

    return {
        "sha256": sha256,
        "positives": resp.get("positives", 0),
        "total": resp.get("total", 0),
        "permalink": resp.get("permalink", ""),
        "scan_date": resp.get("scan_date", ""),
    }


def virustotal_check_suspicious_files(project_root: Path) -> list[dict]:
    """Check executable / binary files in a project against VirusTotal."""
    key = _get_env("NEXUS_VIRUSTOTAL_KEY", "VIRUSTOTAL_API_KEY")
    if not key:
        return []

    suspicious_exts = {".exe", ".dll", ".so", ".dylib", ".bin", ".sh", ".bat", ".ps1"}
    findings = []
    for path in project_root.rglob("*"):
        if path.suffix in suspicious_exts and path.is_file() and path.stat().st_size < 32 * 1024 * 1024:
            result = virustotal_check_file(path)
            if result and result["positives"] > 0:
                result["file"] = str(path.relative_to(project_root))
                findings.append(result)
    return findings


# ─── Snyk ─────────────────────────────────────────────────────────────────────

def snyk_test_package(package: str, version: str = "", ecosystem: str = "pip") -> list[dict]:
    """Test a package against Snyk's vulnerability database.

    Requires NEXUS_SNYK_TOKEN or SNYK_TOKEN.
    """
    token = _get_env("NEXUS_SNYK_TOKEN", "SNYK_TOKEN")
    if not token:
        return []

    # Snyk REST API v2023-11-06 — test by package purl
    purl_ecosystem = {
        "pip": "pypi", "npm": "npm", "cargo": "cargo", "maven": "maven",
    }.get(ecosystem, ecosystem)
    purl = f"pkg:{purl_ecosystem}/{package}"
    if version:
        purl += f"@{version}"

    url = f"https://api.snyk.io/rest/self?version=2023-11-06"
    resp = _http_get(
        f"https://api.snyk.io/rest/packages/{urllib.parse.quote(purl, safe='')}"
        "?version=2023-11-06",
        headers={"Authorization": f"token {token}"},
    )
    if not resp:
        return []

    vulns = []
    for issue in resp.get("data", {}).get("relationships", {}).get("issues", {}).get("data", [])[:5]:
        vulns.append({
            "id": issue.get("id", ""),
            "severity": issue.get("attributes", {}).get("effective_severity_level", "unknown"),
        })
    return vulns


# ─── Combined security scan ───────────────────────────────────────────────────

def run_security_scan(
    project_root: Path,
    dep_names: list[tuple[str, str, str]],  # (name, version, ecosystem)
    deep: bool = False,
) -> dict[str, Any]:
    """Run all configured security checks for a project.

    Returns a dict with sections: secrets, cves, osv_vulns, virustotal.
    """
    report: dict[str, Any] = {
        "secrets": [],
        "cves": {},
        "osv_vulns": {},
        "virustotal": [],
        "sources_used": [],
    }

    # Secrets scan
    if _get_env("NEXUS_GITGUARDIAN_KEY", "GITGUARDIAN_API_KEY"):
        report["secrets"] = gitguardian_scan_project(project_root)
        report["sources_used"].append("GitGuardian")

    # OSV vulnerability check (no key, always available)
    if dep_names:
        report["osv_vulns"] = osv_check_packages(dep_names)
        report["sources_used"].append("OSV")

    # NVD CVE search (no key, always available)
    if dep_names:
        pkg_names = [name for name, _, _ in dep_names[:10]]
        report["cves"] = nvd_check_packages(pkg_names)
        report["sources_used"].append("NVD")

    # VirusTotal (optional, only if key provided and deep=True)
    if deep and _get_env("NEXUS_VIRUSTOTAL_KEY", "VIRUSTOTAL_API_KEY"):
        report["virustotal"] = virustotal_check_suspicious_files(project_root)
        report["sources_used"].append("VirusTotal")

    return report


def format_security_report(report: dict[str, Any], project_name: str) -> str:
    """Format security scan results as readable text."""
    lines = [f"## Security Report: {project_name}"]
    sources = report.get("sources_used", [])
    lines.append(f"Sources: {', '.join(sources) if sources else 'none configured'}")

    secrets = report.get("secrets", [])
    if secrets:
        lines.append(f"\n### Secrets Detected ({len(secrets)}) ⚠")
        for s in secrets[:10]:
            lines.append(f"  [{s['policy']}] {s['filename']}: {s['detector']}")
    else:
        lines.append("\n✓ No secrets detected")

    osv = report.get("osv_vulns", {})
    if osv:
        total_osv = sum(len(v) for v in osv.values())
        lines.append(f"\n### OSV Vulnerabilities ({total_osv} across {len(osv)} packages)")
        for pkg, vulns in sorted(osv.items()):
            for v in vulns[:3]:
                sev = v.get("severity", "?")
                cve = f" [{v['cve']}]" if v.get("cve") else ""
                lines.append(f"  {pkg}{cve} — {sev}: {v['summary'][:80]}")
    else:
        lines.append("✓ No OSV vulnerabilities found")

    cves = report.get("cves", {})
    if cves:
        lines.append(f"\n### NVD CVEs ({sum(len(v) for v in cves.values())} hits)")
        for pkg, pkg_cves in sorted(cves.items()):
            for c in pkg_cves[:2]:
                lines.append(f"  {pkg} — {c['id']} [{c['severity']}]: {c['description'][:80]}")

    vt = report.get("virustotal", [])
    if vt:
        lines.append(f"\n### VirusTotal Alerts ({len(vt)})")
        for f in vt:
            lines.append(f"  {f['file']}: {f['positives']}/{f['total']} detections")

    return "\n".join(lines)
