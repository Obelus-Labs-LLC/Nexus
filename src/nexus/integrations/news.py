"""News integrations: NewsAPI, GNews, Currents, MarketAux.

Environment variables:
  NEXUS_NEWSAPI_KEY     or  NEWS_API_KEY
  NEXUS_GNEWS_TOKEN     or  GNEWS_TOKEN
  NEXUS_CURRENTS_KEY    or  CURRENTS_API_KEY
  NEXUS_MARKETAUX_TOKEN or  MARKETAUX_API_TOKEN
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.integrations.base import _get_env, _http_get, _qs

logger = logging.getLogger("nexus.integrations.news")


# ─── NewsAPI ──────────────────────────────────────────────────────────────────

def newsapi_top_headlines(
    query: str = "",
    category: str = "technology",
    language: str = "en",
    limit: int = 5,
) -> list[dict]:
    """Fetch top headlines from NewsAPI.

    Requires NEXUS_NEWSAPI_KEY or NEWS_API_KEY.
    category: business, entertainment, general, health, science, sports, technology
    """
    key = _get_env("NEXUS_NEWSAPI_KEY", "NEWS_API_KEY")
    if not key:
        return []

    params: dict = {"language": language, "pageSize": limit, "apiKey": key}
    if query:
        params["q"] = query
    else:
        params["category"] = category

    resp = _http_get(
        "https://newsapi.org/v2/top-headlines?" + _qs(params),
        cache=False,
    )
    if not resp or resp.get("status") != "ok":
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "source": a.get("source", {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
            "url": a.get("url", ""),
            "description": (a.get("description") or "")[:200],
        }
        for a in resp.get("articles", [])
    ]


def newsapi_search(query: str, language: str = "en", limit: int = 5) -> list[dict]:
    """Search all articles via NewsAPI /everything endpoint."""
    key = _get_env("NEXUS_NEWSAPI_KEY", "NEWS_API_KEY")
    if not key:
        return []

    resp = _http_get(
        "https://newsapi.org/v2/everything?" + _qs({
            "q": query,
            "language": language,
            "pageSize": limit,
            "sortBy": "relevancy",
            "apiKey": key,
        }),
        cache=False,
    )
    if not resp or resp.get("status") != "ok":
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "source": a.get("source", {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
            "url": a.get("url", ""),
        }
        for a in resp.get("articles", [])
    ]


# ─── GNews ────────────────────────────────────────────────────────────────────

def gnews_search(query: str, language: str = "en", limit: int = 5) -> list[dict]:
    """Search news articles via GNews API.

    Requires NEXUS_GNEWS_TOKEN or GNEWS_TOKEN.
    """
    token = _get_env("NEXUS_GNEWS_TOKEN", "GNEWS_TOKEN")
    if not token:
        return []

    resp = _http_get(
        "https://gnews.io/api/v4/search?" + _qs({
            "q": query,
            "lang": language,
            "max": limit,
            "apikey": token,
        }),
        cache=False,
    )
    if not resp:
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "source": a.get("source", {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
            "url": a.get("url", ""),
            "description": (a.get("description") or "")[:200],
        }
        for a in resp.get("articles", [])
    ]


def gnews_top_headlines(
    topic: str = "technology",
    language: str = "en",
    limit: int = 5,
) -> list[dict]:
    """Fetch top headlines by topic via GNews.

    topic: breaking-news, world, nation, business, technology, entertainment,
           sports, science, health
    """
    token = _get_env("NEXUS_GNEWS_TOKEN", "GNEWS_TOKEN")
    if not token:
        return []

    resp = _http_get(
        "https://gnews.io/api/v4/top-headlines?" + _qs({
            "topic": topic,
            "lang": language,
            "max": limit,
            "apikey": token,
        }),
        cache=False,
    )
    if not resp:
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "source": a.get("source", {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
            "url": a.get("url", ""),
        }
        for a in resp.get("articles", [])
    ]


# ─── Currents ─────────────────────────────────────────────────────────────────

def currents_latest(language: str = "en", limit: int = 5) -> list[dict]:
    """Fetch latest news from Currents API.

    Requires NEXUS_CURRENTS_KEY or CURRENTS_API_KEY.
    """
    key = _get_env("NEXUS_CURRENTS_KEY", "CURRENTS_API_KEY")
    if not key:
        return []

    resp = _http_get(
        "https://api.currentsapi.services/v1/latest-news?" + _qs({
            "language": language,
            "apiKey": key,
        }),
        cache=False,
    )
    if not resp or resp.get("status") != "ok":
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "category": a.get("category", []),
            "published": a.get("published", ""),
            "url": a.get("url", ""),
            "description": (a.get("description") or "")[:200],
        }
        for a in resp.get("news", [])[:limit]
    ]


def currents_search(query: str, language: str = "en", limit: int = 5) -> list[dict]:
    """Search news articles via Currents API."""
    key = _get_env("NEXUS_CURRENTS_KEY", "CURRENTS_API_KEY")
    if not key:
        return []

    resp = _http_get(
        "https://api.currentsapi.services/v1/search?" + _qs({
            "keywords": query,
            "language": language,
            "limit": limit,
            "apiKey": key,
        }),
        cache=False,
    )
    if not resp or resp.get("status") != "ok":
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "published": a.get("published", ""),
            "url": a.get("url", ""),
        }
        for a in resp.get("news", [])
    ]


# ─── MarketAux ────────────────────────────────────────────────────────────────

def marketaux_news(
    symbols: list[str] | None = None,
    query: str = "",
    limit: int = 5,
) -> list[dict]:
    """Fetch financial/market news from MarketAux.

    Requires NEXUS_MARKETAUX_TOKEN or MARKETAUX_API_TOKEN.
    symbols: list of ticker symbols like ["AAPL", "MSFT"]
    """
    token = _get_env("NEXUS_MARKETAUX_TOKEN", "MARKETAUX_API_TOKEN")
    if not token:
        return []

    params: dict = {"api_token": token, "limit": limit, "language": "en"}
    if symbols:
        params["symbols"] = ",".join(symbols[:10])
    if query:
        params["search"] = query

    resp = _http_get(
        "https://api.marketaux.com/v1/news/all?" + _qs(params),
        cache=False,
    )
    if not resp:
        return []

    return [
        {
            "title": a.get("title", "")[:150],
            "source": a.get("source", ""),
            "published_at": a.get("published_at", ""),
            "url": a.get("url", ""),
            "summary": (a.get("description") or "")[:200],
            "entities": [
                e.get("symbol", "") for e in a.get("entities", []) if e.get("symbol")
            ][:5],
            "sentiment_score": a.get("sentiment_score"),
        }
        for a in resp.get("data", [])
    ]


def marketaux_entity_stats(symbol: str) -> dict | None:
    """Get news sentiment stats for a ticker symbol from MarketAux."""
    token = _get_env("NEXUS_MARKETAUX_TOKEN", "MARKETAUX_API_TOKEN")
    if not token:
        return None

    resp = _http_get(
        "https://api.marketaux.com/v1/entity/stats/aggregated?" + _qs({
            "symbols": symbol,
            "api_token": token,
        }),
        cache=False,
    )
    if not resp or not resp.get("data"):
        return None

    stats = resp["data"][0] if resp["data"] else {}
    return {
        "symbol": symbol,
        "avg_sentiment": stats.get("avg_sentiment_score"),
        "count": stats.get("count", 0),
        "positive": stats.get("positive_sentiment_count", 0),
        "negative": stats.get("negative_sentiment_count", 0),
    }


# ─── Combined news feed ───────────────────────────────────────────────────────

def get_tech_news(query: str = "software development", limit: int = 5) -> dict[str, Any]:
    """Pull tech news from all configured sources."""
    result: dict[str, Any] = {"query": query, "sources": []}

    if _get_env("NEXUS_NEWSAPI_KEY", "NEWS_API_KEY"):
        articles = newsapi_search(query, limit=limit)
        if articles:
            result["newsapi"] = articles
            result["sources"].append("NewsAPI")

    if _get_env("NEXUS_GNEWS_TOKEN", "GNEWS_TOKEN"):
        articles = gnews_search(query, limit=limit)
        if articles:
            result["gnews"] = articles
            result["sources"].append("GNews")

    if _get_env("NEXUS_CURRENTS_KEY", "CURRENTS_API_KEY"):
        articles = currents_search(query, limit=limit)
        if articles:
            result["currents"] = articles
            result["sources"].append("Currents")

    if _get_env("NEXUS_MARKETAUX_TOKEN", "MARKETAUX_API_TOKEN"):
        articles = marketaux_news(query=query, limit=limit)
        if articles:
            result["marketaux"] = articles
            result["sources"].append("MarketAux")

    return result


def format_news_feed(feed: dict[str, Any]) -> str:
    """Format news feed for MCP output."""
    lines = [f"## News: {feed.get('query', '')}"]
    sources = feed.get("sources", [])

    if not sources:
        lines.append("No news integrations configured.")
        lines.append("Set one of: NEXUS_NEWSAPI_KEY, NEXUS_GNEWS_TOKEN, NEXUS_CURRENTS_KEY, NEXUS_MARKETAUX_TOKEN")
        return "\n".join(lines)

    lines.append(f"Sources: {', '.join(sources)}\n")

    for source_key in ("newsapi", "gnews", "currents", "marketaux"):
        articles = feed.get(source_key, [])
        if not articles:
            continue
        lines.append(f"### {source_key.capitalize()}")
        for a in articles[:3]:
            published = a.get("published_at") or a.get("published", "")
            date = published[:10] if published else ""
            lines.append(f"  [{date}] {a['title']}")

    return "\n".join(lines)
