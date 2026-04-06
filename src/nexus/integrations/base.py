"""Base classes and shared HTTP utilities for all Nexus integrations.

All HTTP calls use stdlib urllib — zero additional dependencies required.
Optional packages (requests, httpx) are NOT required.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nexus.integrations")

_CACHE: dict[str, tuple[float, Any]] = {}  # url -> (timestamp, data)
_CACHE_TTL = 300  # 5 minutes


# ─── Config helpers ───────────────────────────────────────────────────────────

def _get_env(*names: str) -> str | None:
    """Return first non-empty env var from the provided names."""
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


# ─── HTTP utilities ───────────────────────────────────────────────────────────

def _http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    cache: bool = True,
) -> Any:
    """GET a URL and return parsed JSON, or None on any error."""
    if cache and url in _CACHE:
        ts, data = _CACHE[url]
        if time.time() - ts < _CACHE_TTL:
            return data

    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if cache:
                _CACHE[url] = (time.time(), data)
            return data
    except urllib.error.HTTPError as e:
        logger.debug("HTTP %d for %s", e.code, url)
    except urllib.error.URLError as e:
        logger.debug("URL error for %s: %s", url, e.reason)
    except Exception as e:
        logger.debug("GET error for %s: %s", url, e)
    return None


def _http_post(
    url: str,
    payload: dict | list,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> Any:
    """POST JSON and return parsed response, or None on any error."""
    h = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        logger.debug("HTTP %d for POST %s", e.code, url)
    except Exception as e:
        logger.debug("POST error for %s: %s", url, e)
    return None


def _qs(params: dict) -> str:
    """Build a URL query string from a dict, skipping None values."""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


# ─── Integration availability registry ───────────────────────────────────────

_INTEGRATION_ENV_MAP: dict[str, list[str]] = {
    # security
    "GitGuardian": ["NEXUS_GITGUARDIAN_KEY", "GITGUARDIAN_API_KEY"],
    "NVD": [],  # no key required
    "OSV": [],  # no key required
    "VirusTotal": ["NEXUS_VIRUSTOTAL_KEY", "VIRUSTOTAL_API_KEY"],
    "Snyk": ["NEXUS_SNYK_TOKEN", "SNYK_TOKEN"],
    # vcs
    "GitHub": ["NEXUS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"],
    "GitLab": ["NEXUS_GITLAB_TOKEN", "GITLAB_TOKEN"],
    "Bitbucket": ["NEXUS_BITBUCKET_TOKEN", "BITBUCKET_TOKEN"],
    "AzureDevOps": ["NEXUS_AZURE_DEVOPS_TOKEN", "AZURE_DEVOPS_TOKEN"],
    # ci
    "CircleCI": ["NEXUS_CIRCLECI_TOKEN", "CIRCLE_TOKEN"],
    "TravisCI": ["NEXUS_TRAVIS_TOKEN", "TRAVIS_TOKEN"],
    "Bitrise": ["NEXUS_BITRISE_TOKEN", "BITRISE_TOKEN"],
    "Buddy": ["NEXUS_BUDDY_TOKEN", "BUDDY_TOKEN"],
    "Codeship": ["NEXUS_CODESHIP_TOKEN", "CODESHIP_TOKEN"],
    # packages
    "npm": [],       # no key required
    "PyPI": [],      # no key required
    "CDNJS": [],     # no key required
    "jsDelivr": [],  # no key required
    "APIsGuru": [],  # no key required
    # nlp
    "NLPCloud": ["NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN"],
    "Datamuse": [],  # no key required
    "WolframAlpha": ["NEXUS_WOLFRAM_APPID", "WOLFRAM_APPID"],
    # news
    "NewsAPI": ["NEXUS_NEWSAPI_KEY", "NEWS_API_KEY"],
    "GNews": ["NEXUS_GNEWS_TOKEN", "GNEWS_TOKEN"],
    "Currents": ["NEXUS_CURRENTS_KEY", "CURRENTS_API_KEY"],
    "MarketAux": ["NEXUS_MARKETAUX_TOKEN", "MARKETAUX_API_TOKEN"],
    # analytics
    "KeenIO": ["NEXUS_KEEN_PROJECT_ID", "KEEN_PROJECT_ID"],
    "TimeDoor": ["NEXUS_TIMEDOOR_KEY", "TIMEDOOR_API_KEY"],
    # knowledge
    "Wikidata": [],         # no key required
    "ChangelogsMD": [],     # no key required
}


def configured_integrations() -> dict[str, bool]:
    """Return a map of integration name -> whether it's configured."""
    result = {}
    for name, env_names in _INTEGRATION_ENV_MAP.items():
        if not env_names:
            result[name] = True  # No key required — always available
        else:
            result[name] = _get_env(*env_names) is not None
    return result
