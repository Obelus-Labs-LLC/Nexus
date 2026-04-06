"""Package registry integrations: npm, PyPI, CDNJS, jsDelivr, APIs.guru.

All registries are public and require no API key.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.integrations.base import _http_get, _qs

logger = logging.getLogger("nexus.integrations.packages")


# ─── npm Registry ────────────────────────────────────────────────────────────

def npm_get_package(name: str) -> dict | None:
    """Fetch npm package metadata (latest version, description, license)."""
    resp = _http_get(f"https://registry.npmjs.org/{name}/latest")
    if not resp:
        return None
    return {
        "name": resp.get("name", ""),
        "version": resp.get("version", ""),
        "description": (resp.get("description") or "")[:200],
        "license": resp.get("license", ""),
        "homepage": resp.get("homepage", ""),
        "dependencies": list((resp.get("dependencies") or {}).keys())[:20],
    }


def npm_search(query: str, limit: int = 5) -> list[dict]:
    """Search npm registry for packages."""
    resp = _http_get(
        f"https://registry.npmjs.org/-/v1/search?" + _qs({"text": query, "size": limit})
    )
    if not resp:
        return []
    return [
        {
            "name": obj.get("package", {}).get("name", ""),
            "version": obj.get("package", {}).get("version", ""),
            "description": (obj.get("package", {}).get("description") or "")[:150],
            "score": round(obj.get("score", {}).get("final", 0.0), 3),
        }
        for obj in resp.get("objects", [])
    ]


def npm_get_downloads(name: str, period: str = "last-month") -> int | None:
    """Get download count for an npm package. period: last-day, last-week, last-month."""
    resp = _http_get(f"https://api.npmjs.org/downloads/point/{period}/{name}")
    if not resp:
        return None
    return resp.get("downloads")


def npm_check_packages(names: list[str]) -> list[dict]:
    """Fetch metadata for a list of npm package names."""
    results = []
    for name in names[:20]:
        info = npm_get_package(name)
        if info:
            results.append(info)
    return results


# ─── PyPI ─────────────────────────────────────────────────────────────────────

def pypi_get_package(name: str) -> dict | None:
    """Fetch PyPI package metadata."""
    resp = _http_get(f"https://pypi.org/pypi/{name}/json")
    if not resp:
        return None
    info = resp.get("info", {})
    return {
        "name": info.get("name", ""),
        "version": info.get("version", ""),
        "summary": (info.get("summary") or "")[:200],
        "license": info.get("license", ""),
        "home_page": info.get("home_page", ""),
        "requires_python": info.get("requires_python", ""),
        "requires_dist": (info.get("requires_dist") or [])[:15],
    }


def pypi_search_simple(name: str) -> bool:
    """Check if a package exists on PyPI (simple index lookup)."""
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/simple/{name}/", timeout=5
        ) as r:
            return r.status == 200
    except Exception:
        return False


def pypi_check_packages(names: list[str]) -> list[dict]:
    """Fetch metadata for a list of PyPI package names."""
    results = []
    for name in names[:20]:
        info = pypi_get_package(name)
        if info:
            results.append(info)
    return results


# ─── CDNJS ────────────────────────────────────────────────────────────────────

def cdnjs_search(query: str, limit: int = 5) -> list[dict]:
    """Search CDNJS for JavaScript libraries."""
    resp = _http_get(
        f"https://api.cdnjs.com/libraries?" + _qs({"search": query, "limit": limit, "fields": "name,description,version,homepage"})
    )
    if not resp:
        return []
    return [
        {
            "name": lib.get("name", ""),
            "version": lib.get("version", ""),
            "description": (lib.get("description") or "")[:150],
            "homepage": lib.get("homepage", ""),
        }
        for lib in resp.get("results", [])
    ]


def cdnjs_get_library(name: str) -> dict | None:
    """Fetch CDNJS library details including latest CDN URL."""
    resp = _http_get(
        f"https://api.cdnjs.com/libraries/{name}?fields=name,description,version,homepage,filename,latest"
    )
    if not resp:
        return None
    return {
        "name": resp.get("name", ""),
        "version": resp.get("version", ""),
        "description": (resp.get("description") or "")[:200],
        "latest_url": resp.get("latest", ""),
        "homepage": resp.get("homepage", ""),
    }


# ─── jsDelivr ────────────────────────────────────────────────────────────────

def jsdelivr_get_stats(package: str, period: str = "month") -> dict | None:
    """Get jsDelivr CDN download stats for an npm package.

    period: day, week, month, year
    """
    resp = _http_get(
        f"https://data.jsdelivr.com/v1/stats/packages/npm/{package}?period={period}"
    )
    if not resp:
        return None
    return {
        "package": package,
        "period": period,
        "hits": resp.get("hits", {}).get("total", 0),
        "bandwidth_bytes": resp.get("bandwidth", {}).get("total", 0),
    }


def jsdelivr_resolve(package: str, version: str = "latest") -> dict | None:
    """Resolve an npm package version on jsDelivr and get CDN URL."""
    resp = _http_get(
        f"https://data.jsdelivr.com/v1/packages/npm/{package}@{version}"
    )
    if not resp:
        return None
    files = resp.get("files", [])
    return {
        "package": package,
        "version": resp.get("version", version),
        "cdn_base": f"https://cdn.jsdelivr.net/npm/{package}@{resp.get('version', version)}/",
        "file_count": len(files),
    }


# ─── APIs.guru ────────────────────────────────────────────────────────────────

def apisguru_list() -> list[dict]:
    """List all APIs tracked by APIs.guru (OpenAPI directory)."""
    resp = _http_get("https://api.apis.guru/v2/list.json")
    if not resp or not isinstance(resp, dict):
        return []
    results = []
    for name, data in list(resp.items())[:100]:
        preferred = data.get("preferred", "")
        info = data.get("versions", {}).get(preferred, {}).get("info", {})
        results.append({
            "name": name,
            "title": info.get("title", name),
            "description": (info.get("description") or "")[:100],
            "version": preferred,
            "contact": info.get("contact", {}).get("email", ""),
        })
    return results


def apisguru_get(name: str) -> dict | None:
    """Get OpenAPI spec summary for a specific API (e.g. 'github.com')."""
    resp = _http_get(f"https://api.apis.guru/v2/{name}.json")
    if not resp:
        return None
    preferred = resp.get("preferred", "")
    version_data = resp.get("versions", {}).get(preferred, {})
    info = version_data.get("info", {})
    return {
        "name": name,
        "title": info.get("title", ""),
        "description": (info.get("description") or "")[:300],
        "version": preferred,
        "spec_url": version_data.get("swaggerUrl", "") or version_data.get("openApiUrl", ""),
    }


def apisguru_metrics() -> dict | None:
    """Get APIs.guru platform metrics (total APIs, endpoints, etc.)."""
    resp = _http_get("https://api.apis.guru/v2/metrics.json")
    if not resp:
        return None
    return {
        "num_apis": resp.get("numAPIs", 0),
        "num_endpoints": resp.get("numEndpoints", 0),
        "num_specs": resp.get("numSpecs", 0),
    }


# ─── Combined package summary ────────────────────────────────────────────────

def get_package_info(name: str, ecosystem: str = "auto") -> dict[str, Any]:
    """Fetch package info from all applicable registries.

    ecosystem: "auto", "npm", "pypi", or "both"
    """
    result: dict[str, Any] = {"name": name}

    is_npm = ecosystem in ("npm", "auto", "both")
    is_pypi = ecosystem in ("pypi", "auto", "both")

    if is_npm:
        npm = npm_get_package(name)
        if npm:
            result["npm"] = npm

    if is_pypi:
        pypi = pypi_get_package(name)
        if pypi:
            result["pypi"] = pypi

    return result


def format_package_info(info: dict[str, Any]) -> str:
    """Format package info for MCP output."""
    lines = [f"## Package: {info['name']}"]

    npm = info.get("npm")
    if npm:
        lines.append(f"\n### npm v{npm['version']}")
        lines.append(f"  {npm['description']}")
        if npm.get("license"):
            lines.append(f"  License: {npm['license']}")
        if npm.get("dependencies"):
            lines.append(f"  Dependencies ({len(npm['dependencies'])}): {', '.join(npm['dependencies'][:8])}")

    pypi = info.get("pypi")
    if pypi:
        lines.append(f"\n### PyPI v{pypi['version']}")
        lines.append(f"  {pypi['summary']}")
        if pypi.get("license"):
            lines.append(f"  License: {pypi['license']}")
        if pypi.get("requires_python"):
            lines.append(f"  Requires Python: {pypi['requires_python']}")

    if not npm and not pypi:
        lines.append("  Package not found on npm or PyPI.")

    return "\n".join(lines)
