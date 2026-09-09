from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Literal

from damicore_normalizer.errors import NormalizerError

DecimalSeparator = Literal[".", ","]

# Two literal patterns rather than one built from a placeholder, because an unescaped "."
# would accept "1x5" and then fail inside Decimal. They are applied with fullmatch, because
# "$" also matches before a trailing newline and a quoted cell holding "1\n" would pass.
# The exponent is bounded at six digits so that every match is a value the Decimal
# constructor accepts: without the bound "1e1000000000000000000" matches and Decimal raises
# InvalidOperation, an exception that is not this package's. Six digits reach 1e999999, far
# beyond any number a spreadsheet, a float64, or a scientific export can hold. Whitespace is
# space and tab only, so digits and padding are ASCII on every platform and locale.
_GRAMMARS: dict[DecimalSeparator, re.Pattern[str]] = {
    ".": re.compile(
        r"[ \t]*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]{1,6})?[ \t]*", re.ASCII
    ),
    ",": re.compile(r"[ \t]*[+-]?([0-9]+(,[0-9]*)?|,[0-9]+)([eE][+-]?[0-9]{1,6})?[ \t]*", re.ASCII),
}

_BOTH_SEPARATORS: tuple[DecimalSeparator, ...] = (".", ",")

# What exact rational arithmetic can afford, not a statement about data: a column holding
# both the smallest positive float64 (a subnormal near 5e-324) and the largest (near 2e308)
# at full precision spans 649 digits, so any spreadsheet or scientific export is inside it.
MAX_SPAN_DIGITS = 1000


def is_numeric(text: str, separator: DecimalSeparator) -> bool:
    """Whether a cell satisfies the grammar for ``separator``."""
    return _GRAMMARS[separator].fullmatch(text) is not None


def parse_value(text: str, separator: DecimalSeparator) -> Decimal:
    """Convert a cell :func:`is_numeric` accepted. Construction only, never arithmetic.

    Decimal arithmetic rounds to the context precision of 28 significant digits; construction
    from text, comparison, and ``as_tuple`` are exact regardless of context, and those three
    are the only Decimal operations this package performs.
    """
    stripped = text.strip(" \t")
    return Decimal(stripped.replace(",", ".") if separator == "," else stripped)


def resolve_separator(
    cells: Iterable[str],
    column: str,
    declared: DecimalSeparator | None,
) -> tuple[DecimalSeparator, int]:
    """Decide the decimal separator of a column by falsification, and count its rows.

    A declared separator is a single hypothesis and its first failing cell is refused. With
    none declared, both hypotheses are tested against every cell in one pass: the survivor
    wins; if both survive no cell contains either character, both readings are the same
    number, and "." is chosen for determinism; if neither survives the column is refused
    naming the first failing row of each. No locale, no sampling, no majority vote.

    Raises
    ------
    NormalizerError
        A cell falsifies every live hypothesis (``dataset_format_error``). The message names
        rows and the column, never a cell value.
    """
    hypotheses = _BOTH_SEPARATORS if declared is None else (declared,)
    first_failure: dict[DecimalSeparator, int] = {}
    count = 0
    for count, text in enumerate(cells, start=1):
        for separator in hypotheses:
            if separator not in first_failure and not is_numeric(text, separator):
                first_failure[separator] = count
        if len(first_failure) == len(hypotheses):
            break
    if declared is not None:
        if declared in first_failure:
            raise NormalizerError(
                f"Column {column!r} holds a value that is not a number under decimal "
                f"{declared!r} at data row {first_failure[declared]}",
                code="dataset_format_error",
            )
        return declared, count
    alive: list[DecimalSeparator] = [
        separator for separator in hypotheses if separator not in first_failure
    ]
    if not alive:
        raise NormalizerError(
            f"Column {column!r} is not numeric under decimal '.' (first failing data row "
            f"{first_failure['.']}) nor ',' (first failing data row {first_failure[',']}); "
            "declare decimal= or fix the data",
            code="dataset_format_error",
        )
    return alive[0], count


def scaled_integers(values: Sequence[Decimal]) -> list[int]:
    """Scale a column's values to integers by the column's smallest exponent, exactly.

    Each nonzero value is written as ``c * 10**e`` with ``c`` not divisible by 10, derived from
    ``as_tuple`` in integer space rather than by ``normalize``, which rounds. The scale is
    ``10**-e_min`` for the smallest ``e`` in the column, so the integers are as narrow as the
    data allows and their width is the column's magnitude span, not any value's absolute
    size. The span is computed arithmetically and checked before any integer is built, so
    ``{1e-999999, 1e999999}`` is refused without attempting a two-million-digit integer.

    Raises
    ------
    NormalizerError
        The span exceeds :data:`MAX_SPAN_DIGITS` (``dataset_format_error``).
    """
    parts: list[tuple[int, int, int]] = []
    for value in values:
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int):
            # The grammar admits only finite values; this keeps the function total over the
            # Decimal type rather than over the grammar.
            raise NormalizerError("Value is not finite", code="dataset_format_error")
        trailing = 0
        while trailing < len(digits) and digits[len(digits) - 1 - trailing] == 0:
            trailing += 1
        significant = digits[: len(digits) - trailing]
        if not significant:
            parts.append((0, 0, 0))
            continue
        coefficient = int("".join(str(digit) for digit in significant))
        parts.append((-1 if sign else 1, coefficient, exponent + trailing))

    nonzero = [
        (len(str(coefficient)), exponent) for _, coefficient, exponent in parts if coefficient
    ]
    if not nonzero:
        return [0 for _ in parts]
    minimum_exponent = min(exponent for _, exponent in nonzero)
    span = max(width + exponent - minimum_exponent for width, exponent in nonzero)
    if span > MAX_SPAN_DIGITS:
        raise NormalizerError(
            f"jenks needs the column's magnitude span within {MAX_SPAN_DIGITS} digits; "
            f"it spans {span}",
            code="dataset_format_error",
        )
    return [
        sign * coefficient * 10 ** (exponent - minimum_exponent) if coefficient else 0
        for sign, coefficient, exponent in parts
    ]
