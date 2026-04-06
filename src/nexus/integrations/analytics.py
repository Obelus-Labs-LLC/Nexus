"""Analytics and knowledge integrations: Keen IO, Time Door, Wikidata.

Environment variables:
  NEXUS_KEEN_PROJECT_ID  or  KEEN_PROJECT_ID
  NEXUS_KEEN_READ_KEY    or  KEEN_READ_KEY
  NEXUS_TIMEDOOR_KEY     or  TIMEDOOR_API_KEY
  Wikidata requires no key.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.integrations.base import _get_env, _http_get, _http_post, _qs

logger = logging.getLogger("nexus.integrations.analytics")


# ─── Keen IO ─────────────────────────────────────────────────────────────────

def keen_count_events(
    collection: str,
    timeframe: str = "this_7_days",
    filters: list[dict] | None = None,
) -> int | None:
    """Count events in a Keen IO collection.

    Requires NEXUS_KEEN_PROJECT_ID / KEEN_PROJECT_ID and
             NEXUS_KEEN_READ_KEY  / KEEN_READ_KEY.
    """
    project_id = _get_env("NEXUS_KEEN_PROJECT_ID", "KEEN_PROJECT_ID")
    read_key = _get_env("NEXUS_KEEN_READ_KEY", "KEEN_READ_KEY")
    if not project_id or not read_key:
        return None

    params: dict = {"timeframe": timeframe}
    if filters:
        import json
        params["filters"] = json.dumps(filters)

    resp = _http_get(
        f"https://api.keen.io/3.0/projects/{project_id}/queries/count?"
        + _qs(params),
        headers={"Authorization": read_key},
    )
    if not resp:
        return None
    return resp.get("result")


def keen_count_unique(
    collection: str,
    target_property: str,
    timeframe: str = "this_7_days",
) -> int | None:
    """Count unique values of a property in a Keen IO collection."""
    project_id = _get_env("NEXUS_KEEN_PROJECT_ID", "KEEN_PROJECT_ID")
    read_key = _get_env("NEXUS_KEEN_READ_KEY", "KEEN_READ_KEY")
    if not project_id or not read_key:
        return None

    resp = _http_get(
        f"https://api.keen.io/3.0/projects/{project_id}/queries/count_unique?"
        + _qs({"event_collection": collection, "target_property": target_property, "timeframe": timeframe}),
        headers={"Authorization": read_key},
    )
    if not resp:
        return None
    return resp.get("result")


def keen_funnel(steps: list[dict], timeframe: str = "this_30_days") -> list[int] | None:
    """Run a funnel analysis in Keen IO.

    steps: list of {"event_collection": ..., "actor_property": ...}
    """
    project_id = _get_env("NEXUS_KEEN_PROJECT_ID", "KEEN_PROJECT_ID")
    read_key = _get_env("NEXUS_KEEN_READ_KEY", "KEEN_READ_KEY")
    if not project_id or not read_key:
        return None

    resp = _http_post(
        f"https://api.keen.io/3.0/projects/{project_id}/queries/funnel",
        payload={"steps": steps, "timeframe": timeframe},
        headers={"Authorization": read_key},
    )
    if not resp:
        return None
    return resp.get("result")


def keen_series(
    collection: str,
    interval: str = "daily",
    timeframe: str = "this_7_days",
) -> list[dict] | None:
    """Get a time series count from Keen IO.

    interval: minutely, hourly, daily, weekly, monthly, yearly
    """
    project_id = _get_env("NEXUS_KEEN_PROJECT_ID", "KEEN_PROJECT_ID")
    read_key = _get_env("NEXUS_KEEN_READ_KEY", "KEEN_READ_KEY")
    if not project_id or not read_key:
        return None

    resp = _http_get(
        f"https://api.keen.io/3.0/projects/{project_id}/queries/count?"
        + _qs({"event_collection": collection, "interval": interval, "timeframe": timeframe}),
        headers={"Authorization": read_key},
    )
    if not resp:
        return None
    return [
        {"value": pt.get("value", 0), "start": pt.get("timeframe", {}).get("start", "")}
        for pt in resp.get("result", [])
    ]


# ─── Time Door ────────────────────────────────────────────────────────────────

def timedoor_anomalies(
    series: list[float],
    sensitivity: float = 0.95,
) -> list[int] | None:
    """Detect anomalies in a time series using Time Door API.

    Requires NEXUS_TIMEDOOR_KEY or TIMEDOOR_API_KEY.
    Returns list of anomaly indices.
    """
    key = _get_env("NEXUS_TIMEDOOR_KEY", "TIMEDOOR_API_KEY")
    if not key:
        return None

    resp = _http_post(
        "https://timeseer.ai/api/anomaly",
        payload={"series": series[:1000], "sensitivity": sensitivity},
        headers={"X-API-Key": key},
    )
    if not resp:
        return None
    return resp.get("anomalies", [])


def timedoor_forecast(
    series: list[float],
    horizon: int = 7,
) -> list[float] | None:
    """Forecast future values for a time series.

    Requires NEXUS_TIMEDOOR_KEY or TIMEDOOR_API_KEY.
    Returns list of forecasted values for next `horizon` periods.
    """
    key = _get_env("NEXUS_TIMEDOOR_KEY", "TIMEDOOR_API_KEY")
    if not key:
        return None

    resp = _http_post(
        "https://timeseer.ai/api/forecast",
        payload={"series": series[:1000], "horizon": horizon},
        headers={"X-API-Key": key},
    )
    if not resp:
        return None
    return resp.get("forecast", [])


# ─── Wikidata ─────────────────────────────────────────────────────────────────

def wikidata_search(query: str, language: str = "en", limit: int = 5) -> list[dict]:
    """Search Wikidata entities by label (no key required)."""
    resp = _http_get(
        "https://www.wikidata.org/w/api.php?" + _qs({
            "action": "wbsearchentities",
            "search": query,
            "language": language,
            "limit": limit,
            "format": "json",
        })
    )
    if not resp:
        return []
    return [
        {
            "id": e.get("id", ""),
            "label": e.get("label", ""),
            "description": (e.get("description") or "")[:200],
            "url": e.get("concepturi", ""),
        }
        for e in resp.get("search", [])
    ]


def wikidata_get_entity(qid: str, props: list[str] | None = None) -> dict | None:
    """Fetch a Wikidata entity by QID (e.g. 'Q42' for Douglas Adams).

    No API key required. Returns simplified entity dict.
    """
    if not qid.upper().startswith("Q"):
        return None

    fields = props or ["labels", "descriptions", "claims"]
    resp = _http_get(
        "https://www.wikidata.org/w/api.php?" + _qs({
            "action": "wbgetentities",
            "ids": qid.upper(),
            "props": "|".join(fields),
            "languages": "en",
            "format": "json",
        })
    )
    if not resp:
        return None

    entity = resp.get("entities", {}).get(qid.upper(), {})
    if not entity or entity.get("missing") == "":
        return None

    result: dict[str, Any] = {"id": qid.upper()}

    labels = entity.get("labels", {})
    if "en" in labels:
        result["label"] = labels["en"].get("value", "")

    descriptions = entity.get("descriptions", {})
    if "en" in descriptions:
        result["description"] = descriptions["en"].get("value", "")

    return result


def wikidata_sparql(query: str, limit: int = 10) -> list[dict]:
    """Run a SPARQL query against Wikidata (no key required).

    Returns simplified list of binding dicts.
    """
    import urllib.parse
    url = (
        "https://query.wikidata.org/sparql?query="
        + urllib.parse.quote(query)
        + "&format=json"
    )
    resp = _http_get(url, headers={"Accept": "application/sparql-results+json"})
    if not resp:
        return []

    bindings = resp.get("results", {}).get("bindings", [])[:limit]
    results = []
    for b in bindings:
        row = {}
        for key, val in b.items():
            row[key] = val.get("value", "")
        results.append(row)
    return results


def wikidata_get_software(name: str) -> dict | None:
    """Look up a software project on Wikidata by name.

    Returns entity ID, description, license, repo URL if available.
    """
    entities = wikidata_search(name, limit=3)
    for entity in entities:
        if name.lower() in entity.get("label", "").lower():
            detail = wikidata_get_entity(entity["id"], props=["labels", "descriptions", "claims"])
            if detail:
                detail["wikidata_url"] = f"https://www.wikidata.org/wiki/{entity['id']}"
                return detail
    return None


# ─── Combined analytics summary ──────────────────────────────────────────────

def get_analytics_summary(collection: str | None = None) -> dict[str, Any]:
    """Pull analytics data from all configured providers."""
    result: dict[str, Any] = {"sources": []}

    project_id = _get_env("NEXUS_KEEN_PROJECT_ID", "KEEN_PROJECT_ID")
    read_key = _get_env("NEXUS_KEEN_READ_KEY", "KEEN_READ_KEY")
    if project_id and read_key and collection:
        count = keen_count_events(collection)
        if count is not None:
            result["keen"] = {"collection": collection, "count_7d": count}
            result["sources"].append("KeenIO")

    return result


def format_analytics_summary(summary: dict[str, Any]) -> str:
    """Format analytics summary for MCP output."""
    lines = ["## Analytics Summary"]
    sources = summary.get("sources", [])

    if not sources:
        lines.append("No analytics integrations configured.")
        lines.append("Set NEXUS_KEEN_PROJECT_ID + NEXUS_KEEN_READ_KEY for Keen IO.")
        return "\n".join(lines)

    lines.append(f"Sources: {', '.join(sources)}")

    if "keen" in summary:
        k = summary["keen"]
        lines.append(f"\n### Keen IO — {k['collection']}")
        lines.append(f"  Events (last 7 days): {k['count_7d']:,}")

    return "\n".join(lines)
