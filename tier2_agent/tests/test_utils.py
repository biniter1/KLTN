import pytest

from tier2_agent.utils import canonical_json_key, cap_size, new_id, shannon_entropy


def test_shannon_entropy_empty_is_zero():
    assert shannon_entropy("") == 0.0


def test_shannon_entropy_uniform_text_is_low():
    assert shannon_entropy("aaaaaaaaaa") == 0.0


def test_shannon_entropy_random_looking_text_is_higher():
    low = shannon_entropy("aaaaaaaaaaaaaaaaaaaa")
    high = shannon_entropy("x8K2mQ9zP1vL7nR4wT6y")
    assert high > low


def test_cap_size_no_truncation_when_under_limit():
    text, truncated = cap_size("hello", 100)
    assert text == "hello"
    assert truncated is False


def test_cap_size_truncates_when_over_limit():
    text, truncated = cap_size("hello world", 5)
    assert text == "hello"
    assert truncated is True


def test_canonical_json_key_ignores_dict_key_order():
    a = canonical_json_key({"b": 2, "a": 1})
    b = canonical_json_key({"a": 1, "b": 2})
    assert a == b


def test_canonical_json_key_distinguishes_values():
    a = canonical_json_key({"keyword": "x"})
    b = canonical_json_key({"keyword": "y"})
    assert a != b


def test_new_id_has_prefix_and_is_unique():
    a = new_id("E")
    b = new_id("E")
    assert a.startswith("E_")
    assert a != b
