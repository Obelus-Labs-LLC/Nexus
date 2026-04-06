"""Nexus external API integrations.

Each sub-module wraps one category of public APIs. All integrations:
  - Load credentials from environment variables (see each module for names)
  - Return None / empty results gracefully when not configured
  - Never crash the MCP server on network errors
  - Cache results in the project DB to avoid re-fetching

Available categories:
  security   -- GitGuardian, NVD, OSV, VirusTotal, Snyk
  vcs        -- GitHub, GitLab, Bitbucket, Azure DevOps, Changelogs.md
  ci         -- CircleCI, Travis CI, Bitrise, Buddy, Codeship
  packages   -- npm, PyPI, CDNJS, jsDelivr, APIs.guru
  nlp        -- NLP Cloud, Datamuse, WolframAlpha
  news       -- NewsAPI, GNews, Currents, MarketAux
  analytics  -- Keen IO, Time Door
  knowledge  -- Wikidata
"""
from nexus.integrations.base import _get_env, configured_integrations

__all__ = ["_get_env", "configured_integrations"]
