from __future__ import annotations

import csv
import hashlib
import io
from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.manifest import PartitionEnd, PartitionMethod
from damicore_normalizer.natural_breaks import natural_breaks
from damicore_normalizer.numeric_column import DecimalSeparator, parse_value, scaled_integers


def rank_bounds(
    method: Literal["quantile", "percentile"], parameter: int, row_count: int
) -> tuple[int, ...]:
    """Boundary ranks of a rank-based partition: the one owner of both formulas.

    Exact integer arithmetic. ``quantile`` divides the ranks into ``parameter`` classes whose
    sizes differ by at most one; ``percentile`` cuts the top and bottom ``parameter`` percent
    of rows, its inner bounds coinciding whenever the two floors meet. Jenks is not a formula
    and is not accepted here; its bounds come from :func:`natural_breaks`.
    """
    if method == "quantile":
        return tuple(step * row_count // parameter for step in range(parameter + 1))
    return (0, parameter * row_count // 100, (100 - parameter) * row_count // 100, row_count)


@dataclass(frozen=True)
class FileInterval:
    """One emitted file as a half-open interval of ranks."""

    method: PartitionMethod
    parameter: int
    end: PartitionEnd
    rank_start: int
    rank_end: int


@dataclass(frozen=True)
class Ranking:
    """Pass B's result: each row's rank, and the distinct values with their multiplicities.

    ``rank[i]`` is the position of data row ``i`` in the order (value descending, file
    position ascending); rank 0 is the highest value. ``distinct`` follows the same order, so
    a class boundary after ``distinct[j]`` is the rank ``sum(weights[:j + 1])``.
    """

    rank: array[int]
    distinct: tuple[Decimal, ...]
    weights: tuple[int, ...]


def rank_column(cells: Iterable[str], separator: DecimalSeparator, row_count: int) -> Ranking:
    """Pass B: parse every cell pass A accepted, sort once, and invert the order into ranks.

    ``sorted(..., reverse=True)`` is stable, so equal values keep their file order at no
    cost. Only construction and comparison of ``Decimal`` happen here, both exact. The key
    vector is released on return; what survives is the rank array and the distinct values.
    """
    keys = [parse_value(text, separator) for text in cells]
    if len(keys) != row_count:
        raise NormalizerError("Input changed during partition", code="input_drift")
    order = sorted(range(row_count), key=keys.__getitem__, reverse=True)
    rank = array("q", bytes(8 * row_count))
    distinct: list[Decimal] = []
    weights: list[int] = []
    for position, index in enumerate(order):
        rank[index] = position
        value = keys[index]
        if distinct and distinct[-1] == value:
            weights[-1] += 1
        else:
            distinct.append(value)
            weights.append(1)
    return Ranking(rank=rank, distinct=tuple(distinct), weights=tuple(weights))


def refuse_short_columns(
    quantiles: Sequence[int],
    percentiles: Sequence[int],
    row_count: int,
) -> None:
    """The n-based preconditions, checked once n is known and before anything is written."""
    for parameter in quantiles:
        if row_count < parameter:
            raise NormalizerError(
                f"quantile {parameter} needs at least {parameter} data rows; the column has "
                f"{row_count}. Pass quantiles=() to disable the method or a smaller value",
                code="dataset_format_error",
            )
    for parameter in percentiles:
        if parameter * row_count // 100 < 1:
            needed = -(-100 // parameter)
            raise NormalizerError(
                f"percentile {parameter} needs at least {needed} data rows; the column has "
                f"{row_count}. Pass percentiles=() to disable the method or a larger value",
                code="dataset_format_error",
            )


def file_intervals(
    quantiles: Sequence[int],
    percentiles: Sequence[int],
    jenks_classes: Sequence[int],
    ranking: Ranking,
) -> tuple[FileInterval, ...]:
    """Turn the requested partitions into rank intervals, in manifest order.

    Parameters arrive sorted ascending. Rank-based methods are pure arithmetic on n; Jenks is
    computed once to the largest requested class count, over the distinct values scaled to
    integers, and its d-based preconditions are refused here, still before any file exists.
    """
    row_count = len(ranking.rank)
    intervals: list[FileInterval] = []

    def add(method: PartitionMethod, parameter: int, bounds: Sequence[int]) -> None:
        intervals.append(FileInterval(method, parameter, "high", bounds[0], bounds[1]))
        intervals.append(FileInterval(method, parameter, "low", bounds[-2], bounds[-1]))

    for parameter in quantiles:
        add("quantile", parameter, rank_bounds("quantile", parameter, row_count))
    for parameter in percentiles:
        add("percentile", parameter, rank_bounds("percentile", parameter, row_count))
    if jenks_classes:
        distinct_count = len(ranking.distinct)
        largest = max(jenks_classes)
        if distinct_count < largest:
            raise NormalizerError(
                f"jenks {largest} needs at least {largest} distinct values; the column has "
                f"{distinct_count}. Pass jenks_classes=() to disable the method or a smaller "
                "value",
                code="dataset_format_error",
            )
        breaks = natural_breaks(scaled_integers(ranking.distinct), ranking.weights, largest)
        cumulative = [0]
        for weight in ranking.weights:
            cumulative.append(cumulative[-1] + weight)
        for parameter in jenks_classes:
            add("jenks", parameter, [cumulative[index] for index in breaks[parameter - 2]])
    return tuple(intervals)


class _CsvSink:
    """One emitted file: library-default CSV, hashed as it is written.

    Records are rendered by ``csv.writer`` into a buffer, encoded as UTF-8, and written as
    bytes, so the digest covers exactly the bytes on disk without a second read here; the
    caller re-reads the file afterwards anyway, which is what turns the digest into a check.
    """

    def __init__(self, path: Path) -> None:
        self._stream = path.open("wb")
        self._buffer = io.StringIO()
        self._writer = csv.writer(
            self._buffer,
            delimiter=",",
            quotechar='"',
            doublequote=True,
            escapechar=None,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        self._digest = hashlib.sha256()
        self.size_bytes: int = 0

    def write_row(self, values: Sequence[str]) -> None:
        self._buffer.seek(0)
        self._buffer.truncate()
        self._writer.writerow(values)
        payload = self._buffer.getvalue().encode("utf-8")
        self._digest.update(payload)
        self._stream.write(payload)
        self.size_bytes += len(payload)

    def close(self) -> None:
        self._stream.close()

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def route_rows(
    rows: Iterable[Sequence[str]],
    header: Sequence[str],
    rank: array[int],
    intervals: Sequence[FileInterval],
    output_dir: Path,
) -> tuple[tuple[int, str], ...]:
    """Pass C: stream the table once and append each row to every file whose interval holds it.

    Rows leave in input order, so each emitted file is a pure subset of the input. All files
    stay open for the pass; their number is twice the number of partitions asked for.
    Returns ``(size_bytes, sha256)`` per interval, in the same order.
    """
    sinks: list[_CsvSink] = []
    count = 0
    try:
        # Opened inside the try so that a descriptor limit hit on the Nth file closes the
        # N-1 already open rather than leaving them to the garbage collector.
        for item in intervals:
            sinks.append(
                _CsvSink(output_dir / f"{item.method}_{item.parameter:02d}_{item.end}.csv")
            )
        for sink in sinks:
            sink.write_row(header)
        for count, values in enumerate(rows, start=1):
            if count > len(rank):
                raise NormalizerError("Input changed during partition", code="input_drift")
            position = rank[count - 1]
            for sink, item in zip(sinks, intervals, strict=True):
                if item.rank_start <= position < item.rank_end:
                    sink.write_row(values)
    finally:
        for sink in sinks:
            sink.close()
    if count != len(rank):
        raise NormalizerError("Input changed during partition", code="input_drift")
    return tuple((sink.size_bytes, sink.sha256) for sink in sinks)
