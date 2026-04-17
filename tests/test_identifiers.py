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


# ── Parity tests: match pitlane-mcp's tokenizer expectations ────────────────

def test_lower_instruction_pattern():
    """pitlane's canonical test case — must split PascalCase."""
    assert split_identifier("LowerInstruction") == ["lower", "instruction"]


def test_kebab_case():
    assert split_identifier("some-kebab-case") == ["some", "kebab", "case"]


def test_dotted_identifier():
    assert split_identifier("module.ClassName.method") == ["module", "class", "name", "method"]


def test_mixed_camel_snake():
    assert split_identifier("parse_jsonHTTP") == ["parse", "json", "http"]


def test_digits_inline():
    assert split_identifier("item2vec") == ["item", "2", "vec"]
    assert split_identifier("md5sum") == ["md", "5", "sum"]


def test_code_stopwords_preserved():
    """Code-meaningful stopwords like 'is', 'and', 'or' must survive."""
    tokens = tokenize_code("is_valid and_then or_else the_value")
    assert "is" in tokens
    assert "and" in tokens
    assert "or" in tokens
    assert "the" in tokens


def test_tokenize_preserves_valid():
    tokens = tokenize_code("def is_valid(x): return x > 0")
    assert "is" in tokens
    assert "valid" in tokens
