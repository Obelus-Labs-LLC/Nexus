"""NLP integrations: NLP Cloud, Datamuse, WolframAlpha.

Environment variables:
  NEXUS_NLPCLOUD_KEY  or  NLPCLOUD_TOKEN
  NEXUS_WOLFRAM_APPID or  WOLFRAM_APPID
  Datamuse requires no key.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.integrations.base import _get_env, _http_get, _http_post, _qs

logger = logging.getLogger("nexus.integrations.nlp")


# ─── NLP Cloud ────────────────────────────────────────────────────────────────

# Available models: https://docs.nlpcloud.com/#models
_NLPCLOUD_DEFAULT_MODEL = "finetuned-llama-3-70b"


def nlpcloud_summarize(text: str, model: str = _NLPCLOUD_DEFAULT_MODEL) -> str | None:
    """Summarize text using NLP Cloud.

    Requires NEXUS_NLPCLOUD_KEY or NLPCLOUD_TOKEN.
    """
    key = _get_env("NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN")
    if not key:
        return None

    resp = _http_post(
        f"https://api.nlpcloud.io/v1/{model}/summarization",
        payload={"text": text[:10000]},
        headers={"Authorization": f"Token {key}"},
    )
    if not resp:
        return None
    return resp.get("summary_text", "")


def nlpcloud_classify(
    text: str,
    labels: list[str],
    model: str = "bart-large-mnli",
) -> dict | None:
    """Zero-shot text classification with NLP Cloud.

    Returns {label: score} dict sorted by score descending.
    """
    key = _get_env("NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN")
    if not key:
        return None

    resp = _http_post(
        f"https://api.nlpcloud.io/v1/{model}/classification",
        payload={"text": text[:2000], "labels": labels, "multi_class": False},
        headers={"Authorization": f"Token {key}"},
    )
    if not resp:
        return None
    scores = resp.get("scores", [])
    resp_labels = resp.get("labels", [])
    return dict(sorted(zip(resp_labels, scores), key=lambda x: x[1], reverse=True))


def nlpcloud_entities(text: str, model: str = "en_core_web_lg") -> list[dict] | None:
    """Named entity recognition with NLP Cloud.

    Returns list of {text, type, start, end}.
    """
    key = _get_env("NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN")
    if not key:
        return None

    resp = _http_post(
        f"https://api.nlpcloud.io/v1/{model}/entities",
        payload={"text": text[:5000]},
        headers={"Authorization": f"Token {key}"},
    )
    if not resp:
        return None
    return [
        {
            "text": e.get("text", ""),
            "type": e.get("type", ""),
            "start": e.get("start", 0),
            "end": e.get("end", 0),
        }
        for e in resp.get("entities", [])
    ]


def nlpcloud_keywords(text: str, model: str = "keyphrase-extraction-kbir-inspec") -> list[str] | None:
    """Extract keywords from text using NLP Cloud."""
    key = _get_env("NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN")
    if not key:
        return None

    resp = _http_post(
        f"https://api.nlpcloud.io/v1/{model}/kw-kp-extraction",
        payload={"text": text[:5000]},
        headers={"Authorization": f"Token {key}"},
    )
    if not resp:
        return None
    return [kw.get("text", "") for kw in resp.get("keywords_and_keyphrases", [])]


def nlpcloud_sentiment(text: str, model: str = "distilbert-base-uncased-finetuned-sst-2-english") -> dict | None:
    """Sentiment analysis with NLP Cloud. Returns {label, score}."""
    key = _get_env("NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN")
    if not key:
        return None

    resp = _http_post(
        f"https://api.nlpcloud.io/v1/{model}/sentiment",
        payload={"text": text[:2000]},
        headers={"Authorization": f"Token {key}"},
    )
    if not resp:
        return None
    scored = resp.get("scored_labels", [])
    if not scored:
        return None
    best = max(scored, key=lambda x: x.get("score", 0))
    return {"label": best.get("label", ""), "score": round(best.get("score", 0), 4)}


# ─── Datamuse ─────────────────────────────────────────────────────────────────

def datamuse_words_like(word: str, limit: int = 10) -> list[str]:
    """Find words with similar meaning (no key required)."""
    resp = _http_get(
        f"https://api.datamuse.com/words?" + _qs({"ml": word, "max": limit})
    )
    if not resp or not isinstance(resp, list):
        return []
    return [w.get("word", "") for w in resp]


def datamuse_rhymes(word: str, limit: int = 10) -> list[str]:
    """Find rhymes for a word (no key required)."""
    resp = _http_get(
        f"https://api.datamuse.com/words?" + _qs({"rel_rhy": word, "max": limit})
    )
    if not resp or not isinstance(resp, list):
        return []
    return [w.get("word", "") for w in resp]


def datamuse_adjectives(noun: str, limit: int = 10) -> list[str]:
    """Find adjectives commonly used with a noun (no key required)."""
    resp = _http_get(
        f"https://api.datamuse.com/words?" + _qs({"rel_jjb": noun, "max": limit})
    )
    if not resp or not isinstance(resp, list):
        return []
    return [w.get("word", "") for w in resp]


def datamuse_suggest(prefix: str, limit: int = 10) -> list[str]:
    """Autocomplete / word suggestions for a prefix."""
    resp = _http_get(
        f"https://api.datamuse.com/sug?" + _qs({"s": prefix, "max": limit})
    )
    if not resp or not isinstance(resp, list):
        return []
    return [w.get("word", "") for w in resp]


# ─── WolframAlpha ─────────────────────────────────────────────────────────────

def wolfram_short_answer(query: str) -> str | None:
    """Get a short plaintext answer from WolframAlpha.

    Requires NEXUS_WOLFRAM_APPID or WOLFRAM_APPID.
    """
    appid = _get_env("NEXUS_WOLFRAM_APPID", "WOLFRAM_APPID")
    if not appid:
        return None

    import urllib.request
    url = "https://api.wolframalpha.com/v1/result?" + _qs({"i": query, "appid": appid})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("WolframAlpha error: %s", e)
        return None


def wolfram_full_results(query: str) -> dict | None:
    """Get full WolframAlpha result pods as structured data.

    Requires NEXUS_WOLFRAM_APPID or WOLFRAM_APPID.
    Returns dict with primary pod results.
    """
    appid = _get_env("NEXUS_WOLFRAM_APPID", "WOLFRAM_APPID")
    if not appid:
        return None

    resp = _http_get(
        "https://api.wolframalpha.com/v2/query?" + _qs({
            "input": query,
            "appid": appid,
            "output": "json",
            "format": "plaintext",
        })
    )
    if not resp:
        return None

    pods = resp.get("queryresult", {}).get("pods", [])
    results = {}
    for pod in pods[:5]:
        title = pod.get("title", "")
        subpods = pod.get("subpods", [])
        texts = [sp.get("plaintext", "") for sp in subpods if sp.get("plaintext")]
        if texts:
            results[title] = " | ".join(texts[:3])

    return results if results else None


# ─── Combined NLP query ───────────────────────────────────────────────────────

def analyze_text(text: str) -> dict[str, Any]:
    """Run all configured NLP analyses on a text snippet."""
    result: dict[str, Any] = {}

    if _get_env("NEXUS_NLPCLOUD_KEY", "NLPCLOUD_TOKEN"):
        sentiment = nlpcloud_sentiment(text)
        if sentiment:
            result["sentiment"] = sentiment
        keywords = nlpcloud_keywords(text)
        if keywords:
            result["keywords"] = keywords[:10]

    return result


def format_nlp_result(result: dict[str, Any]) -> str:
    """Format NLP analysis result for MCP output."""
    if not result:
        return "No NLP integrations configured. Set NEXUS_NLPCLOUD_KEY or NEXUS_WOLFRAM_APPID."

    lines = ["## NLP Analysis"]
    if "sentiment" in result:
        s = result["sentiment"]
        lines.append(f"Sentiment: {s['label']} ({s['score']:.2%})")
    if "keywords" in result:
        lines.append(f"Keywords: {', '.join(result['keywords'])}")
    if "wolfram" in result:
        lines.append(f"Wolfram: {result['wolfram']}")
    return "\n".join(lines)
