from decimal import Decimal

import pytest

from damicore_normalizer import NormalizerError
from damicore_normalizer.numeric_column import (
    MAX_SPAN_DIGITS,
    is_numeric,
    parse_value,
    resolve_separator,
    scaled_integers,
)

pytestmark = pytest.mark.unit

ACCEPTED = [".5", "5.", "+5", "1e5", " 12\t", "-0.25E-3", "1e999999", "-0", "0", "1e-999999"]


@pytest.mark.parametrize("text", ACCEPTED)
def test_the_grammar_accepts_a_decimal_literal_and_decimal_constructs_it(text: str) -> None:
    assert is_numeric(text, ".")
    assert parse_value(text, ".").is_finite()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("NaN", id="nan"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("", id="empty"),
        pytest.param("1_000", id="underscore"),
        pytest.param("1.2.3", id="two-separators"),
        pytest.param("1e", id="dangling-exponent"),
        pytest.param("1e1000000", id="seven-digit-exponent"),
        pytest.param("١٢", id="arabic-indic-digits"),
        pytest.param("1,5", id="other-separator"),
        pytest.param("1\n", id="trailing-newline"),
        pytest.param("1x5", id="letter"),
        pytest.param("0x10", id="hex"),
        pytest.param("e5", id="no-significand"),
        pytest.param(" 1", id="non-breaking-space"),
    ],
)
def test_the_grammar_rejects_what_decimal_would_accept_or_misread(text: str) -> None:
    assert not is_numeric(text, ".")


def test_the_comma_grammar_mirrors_the_dot_grammar() -> None:
    assert is_numeric("1,5", ",")
    assert not is_numeric("1.5", ",")
    assert parse_value("1,5", ",") == Decimal("1.5")
    assert parse_value(" 7 ", ".") == 7
    assert parse_value("1,234", ",") == Decimal("1.234")


def test_resolution_prefers_the_dot_when_no_cell_contains_a_separator() -> None:
    assert resolve_separator(["1", "2", "3"], "score", None) == (".", 3)
    assert resolve_separator([], "score", None) == (".", 0)


def test_resolution_detects_the_one_separator_every_cell_satisfies() -> None:
    assert resolve_separator(["1,5", "2", "3,25"], "score", None) == (",", 3)
    assert resolve_separator(["1.5", "2", "3.25"], "score", None) == (".", 3)


def test_a_mixed_column_is_refused_naming_the_first_failing_row_of_each_hypothesis() -> None:
    with pytest.raises(NormalizerError) as raised:
        resolve_separator(["1", "1.5", "2,5"], "score", None)
    assert raised.value.code == "dataset_format_error"
    assert "data row 3" in str(raised.value) and "data row 2" in str(raised.value)
    assert "decimal=" in str(raised.value)
    with pytest.raises(NormalizerError) as thousands:
        resolve_separator(["1.234,56"], "score", None)
    assert "data row 1" in str(thousands.value)


def test_a_declared_separator_is_a_single_hypothesis() -> None:
    assert resolve_separator(["1,5", "2"], "score", ",") == (",", 2)
    with pytest.raises(NormalizerError) as raised:
        resolve_separator(["1", "2", "3,5"], "score", ".")
    assert raised.value.code == "dataset_format_error"
    assert "data row 3" in str(raised.value)
    assert "3,5" not in str(raised.value)


def test_scaling_uses_the_column_minimum_exponent_and_strips_trailing_zeros() -> None:
    assert scaled_integers([Decimal("1.500"), Decimal("2"), Decimal("0.00"), Decimal("-3")]) == [
        15,
        20,
        0,
        -30,
    ]
    assert scaled_integers([Decimal("1e999"), Decimal("2e999")]) == [1, 2]
    assert scaled_integers([Decimal("100"), Decimal("2500")]) == [1, 25]
    assert scaled_integers([Decimal("0"), Decimal("0.0")]) == [0, 0]


def test_scaling_is_exact_past_the_decimal_context_precision() -> None:
    wide = Decimal("1234567890123456789012345678901.5")
    assert scaled_integers([wide, Decimal("1")]) == [12345678901234567890123456789015, 10]


def test_a_span_at_the_bound_is_accepted_and_one_past_it_refused_without_building_it() -> None:
    assert scaled_integers([Decimal("1e999"), Decimal("1")]) == [10**999, 1]
    with pytest.raises(NormalizerError) as raised:
        scaled_integers([Decimal("1e1000"), Decimal("1")])
    assert raised.value.code == "dataset_format_error"
    assert str(MAX_SPAN_DIGITS) in str(raised.value) and "1001" in str(raised.value)
    with pytest.raises(NormalizerError) as huge:
        scaled_integers([Decimal("1e-999999"), Decimal("1e999999")])
    assert "1999999" in str(huge.value)
