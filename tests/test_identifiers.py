"""Tests for identifier splitting and tokenization."""

from nexus.util.identifiers import split_identifier, tokenize_code


def test_camel_case():
    assert split_identifier("getUserName") == ["get", "user", "name"]


def test_pascal_case():
    assert split_identifier("HTTPSConnection") == ["https", "connection"]


def test_snake_case():
    assert split_identifier("get_user_name") == ["get", "user", "name"]


def test_screaming_snake():
    assert split_identifier("MAX_RETRY_COUNT") == ["max", "retry", "count"]


def test_single_word():
    assert split_identifier("hello") == ["hello"]


def test_tokenize_code_basic():
    tokens = tokenize_code("def get_user(name: str) -> User:")
    assert "get" in tokens
    assert "user" in tokens
    assert "name" in tokens
    assert "str" in tokens


def test_tokenize_filters_short():
    tokens = tokenize_code("a b cd ef")
    # Tokens shorter than 2 chars should be filtered
    assert "a" not in tokens
    assert "b" not in tokens
