"""Identifier splitting for BM25 tokenization.

Splits camelCase, PascalCase, snake_case, SCREAMING_SNAKE, kebab-case,
and mixed identifiers into constituent words. Critical for search recall —
research shows 50%+ improvement when identifiers are split before indexing.
"""

from __future__ import annotations

import re

# Regex patterns for splitting
_CAMEL_BOUNDARY = re.compile(
    r"(?<=[a-z])(?=[A-Z])"      # camelCase boundary
    r"|(?<=[A-Z])(?=[A-Z][a-z])" # XMLParser → XML, Parser
)
_SEPARATORS = re.compile(r"[_\-./\\:]+")
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9\s]")
_DIGITS_SPLIT = re.compile(r"(?<=[a-zA-Z])(?=[0-9])|(?<=[0-9])(?=[a-zA-Z])")


def split_identifier(name: str) -> list[str]:
    """Split a code identifier into constituent words.

    Examples:
        getUserName → [get, user, name]
        HTTPSConnection → [https, connection]
        my_var_name → [my, var, name]
        MAX_RETRY_COUNT → [max, retry, count]
        parseJSON → [parse, json]
        item2vec → [item, 2, vec]
    """
    if not name:
        return []

    # Step 1: Split on explicit separators (_, -, ., etc.)
    parts = _SEPARATORS.split(name)

    # Step 2: Split each part on camelCase boundaries
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        sub_parts = _CAMEL_BOUNDARY.split(part)
        for sp in sub_parts:
            # Step 3: Split digit/letter boundaries
            digit_parts = _DIGITS_SPLIT.split(sp)
            tokens.extend(digit_parts)

    # Step 4: Lowercase and filter empty
    return [t.lower() for t in tokens if t and len(t) > 0]


def tokenize_code(text: str) -> list[str]:
    """Tokenize a code string for BM25 indexing.

    Splits all identifiers, keeps meaningful words, drops noise.
    """
    if not text:
        return []

    # Replace non-alphanumeric with spaces (keep underscores for splitting)
    cleaned = re.sub(r"[^\w\s]", " ", text)

    tokens: list[str] = []
    for word in cleaned.split():
        if len(word) <= 1:
            continue
        parts = split_identifier(word)
        tokens.extend(parts)

    # Filter very short tokens and pure numbers
    return [t for t in tokens if len(t) > 1 or t.isdigit()]
