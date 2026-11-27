"""The delimited reader's records: the cells it yields, and the memory it holds to yield them.

The reference for the cells is the pandas call the reader used through 0.7.1, kept here and
nowhere else: object bytes are a run's scientific identity, so a reader that streams rows must
yield exactly the cells that one did, on every input either accepts or refuses (issue #136).
"""

from __future__ import annotations

import csv
import io
import random
import string
import tempfile
import tracemalloc
from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from damicore_normalizer import DelimitedSource, NormalizationConfig, materialize_objects
from damicore_normalizer.delimited_reader import (
    iter_records,
    read_header,
    translating_parse_failures,
    validate_record_widths,
)
from damicore_normalizer.errors import NormalizerError

pytestmark = pytest.mark.unit


def _pandas_records(
    path: Path, source: DelimitedSource, columns: Sequence[int] | None
) -> list[tuple[str, ...]]:
    """The reader as 0.7.1 shipped it, call for call: the oracle the streaming reader is held
    to. Two-row chunks put a chunk boundary between almost every pair of records."""
    chunks = pd.read_csv(
        path,
        sep=source.delimiter,
        encoding=source.encoding,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        skip_blank_lines=False,
        chunksize=2,
        on_bad_lines="error",
        quotechar='"',
        doublequote=True,
        comment=None,
        index_col=False,
        usecols=None if columns is None else list(columns),
    )
    return [
        tuple(str(value) for value in values)
        for chunk in chunks
        for values in chunk.itertuples(index=False, name=None)
    ]


def _widths_as_0_7_1(path: Path, source: DelimitedSource, width: int) -> None:
    """The width pass as 0.7.1 ran it before the pandas call: a lenient csv.reader, no
    end-of-data check."""
    previous = csv.field_size_limit(2**31 - 1)
    try:
        with path.open("r", encoding=source.encoding, errors="strict", newline="") as stream:
            reader = csv.reader(stream, delimiter=source.delimiter)
            next(reader, None)
            for record in reader:
                if record and len(record) != width:
                    raise NormalizerError("width", code="dataset_format_error")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise NormalizerError("parse", code="dataset_format_error") from exc
    finally:
        csv.field_size_limit(previous)


def _outcome(read: Callable[[], object]) -> tuple[str, object]:
    """What a reader made of a file: its records, or that it refused the file."""
    try:
        with translating_parse_failures():
            return "records", read()
    except NormalizerError as error:
        return "refused", error.code


# Cells are drawn from what the CSV grammar treats specially -- every delimiter in play, the
# quote, both line-break characters, spaces and tabs -- plus multi-byte text and the strings
# pandas would read as missing if NA inference were on.
_PIECES = [",", ";", "\t", "|", '"', "\r", "\n", " ", "a", "Z", "0", "é", "😀", "NA", "null"]
_CELL = st.lists(st.sampled_from(_PIECES), max_size=4).map("".join)
_DELIMITER = st.sampled_from([",", ";", "\t", "|"])
_TERMINATOR = st.sampled_from(["\n", "\r\n", "\r"])


@st.composite
def _written_file(draw: st.DrawFn) -> tuple[str, str]:
    """A file csv.writer produced, with blank lines spliced in: every quoting rule exercised
    on cells built to need it."""
    delimiter = draw(_DELIMITER)
    terminator = draw(_TERMINATOR)
    width = draw(st.integers(min_value=1, max_value=4))
    rows = draw(st.lists(st.lists(_CELL, min_size=width, max_size=width), max_size=6))
    quote_all = draw(st.booleans())
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=delimiter,
        lineterminator=terminator,
        quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
    )
    writer.writerow([f"c{index}" for index in range(width)])
    lines: list[str] = [buffer.getvalue()]
    for row in rows:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(row)
        if draw(st.booleans()):
            lines.append(terminator)
        lines.append(buffer.getvalue())
    return delimiter, "".join(lines)


@st.composite
def _raw_file(draw: st.DrawFn) -> tuple[str, str]:
    """A file assembled from raw pieces, quotes and line breaks where no writer would put
    them: text after a closing quote, a quote in mid-field, a bare CR, a missing final line
    break. Whatever its widths, both readers must agree on it or both refuse it."""
    delimiter = draw(_DELIMITER)
    width = draw(st.integers(min_value=1, max_value=3))
    header = delimiter.join(f"c{index}" for index in range(width)) + "\n"
    body = draw(st.lists(st.sampled_from([*_PIECES, '""', "\r\n"]), max_size=24))
    return delimiter, header + "".join(body)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(file=st.one_of(_written_file(), _raw_file()), select=st.booleans(), seed=st.integers(0, 9))
def test_the_records_are_the_cells_the_pandas_reader_yielded(
    file: tuple[str, str], select: bool, seed: int
) -> None:
    """Every file reads as it did through 0.7.1 -- the width pass that ran then and the pandas
    call it replaced -- whole or one column at a time: the same records, or a refusal with the
    same code. A refusal the width pass now makes is held to the same outcome, so the pass
    cannot start refusing a file 0.7.1 read."""
    delimiter, text = file
    source = DelimitedSource(split="columns", delimiter=delimiter)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "input.csv"
        path.write_bytes(text.encode("utf-8"))
        try:
            header = read_header(path, source)
        except NormalizerError:
            return  # unchanged by this reader, and refused before any record is read
        columns = [random.Random(seed).randrange(len(header))] if select else None

        def then() -> list[tuple[str, ...]]:
            _widths_as_0_7_1(path, source, len(header))
            try:
                return _pandas_records(path, source, columns)
            except pd.errors.ParserError as error:
                # pandas' C tokenizer overruns its buffer on some runs of blank lines ended by
                # a bare CR and refuses a file the input contract admits, while it reads the
                # same blank lines elsewhere; that refusal is its defect, not a rule to keep.
                assume("Buffer overflow caught" not in str(error))
                raise

        def now() -> list[tuple[str, ...]]:
            validate_record_widths(path, source, len(header))
            return list(iter_records(path, source, header, columns=columns))

        expected, actual = _outcome(then), _outcome(now)
    assert actual == expected, text


def test_a_run_of_blank_lines_ended_by_a_bare_cr_reads_as_empty_rows(tmp_path: Path) -> None:
    """A blank line is a full-width row of empty cells whatever ends it, as the input contract
    defines it. The pandas reader refused this file with a buffer overrun of its tokenizer
    while it read the same blank lines before a longer row, so the two readers are held to
    the contract here rather than to each other."""
    path = tmp_path / "input.csv"
    path.write_bytes(b"c0,c1\n\r\r,")
    source = DelimitedSource(split="columns")
    header = read_header(path, source)
    validate_record_widths(path, source, len(header))
    assert list(iter_records(path, source, header)) == [("", ""), ("", ""), ("", "")]


def _short_cells_csv(path: Path, rows: int, width: int) -> Path:
    draws = random.Random(136)
    lines = [",".join(f"c{index}" for index in range(width))]
    for _ in range(rows):
        lines.append(
            ",".join("".join(draws.choices(string.ascii_letters, k=8)) for _ in range(width))
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_normalization_memory_does_not_grow_with_chunk_rows(tmp_path: Path) -> None:
    """The column writer streams, so what normalizing holds is one row and the writer's
    fixed buffer: the same whether `chunk_rows` covers 500 rows or the whole file. A reader
    that builds each chunk as objects holds a Python string per cell of the chunk instead,
    about sixty bytes for a cell of eight characters (issue #136)."""
    source = _short_cells_csv(tmp_path / "short.csv", rows=20_000, width=10)

    def peak(chunk_rows: int) -> int:
        config = NormalizationConfig(source=DelimitedSource(split="columns"), chunk_rows=chunk_rows)
        tracemalloc.start()
        try:
            materialize_objects(source, tmp_path / f"out-{chunk_rows}", config=config)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    small, whole = peak(500), peak(50_000)
    # 200,000 cells held at once would add about 12 MB; a streaming reader adds nothing.
    assert whole <= small + 2 * 1_048_576, (small, whole)
