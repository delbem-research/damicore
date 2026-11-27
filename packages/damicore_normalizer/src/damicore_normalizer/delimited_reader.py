"""Read a delimited dataset as text, and only as text.

Any single-character delimiter is accepted, so `.csv`, `.tsv` and `.txt` are one format.
No value is parsed, coerced or inferred: a cell arrives at the serializer as the characters
the file held.

References
----------
.. [adr-0001] Foundational decisions, section "Canonical CSV serialization": "CSV values
   remain text." That is the whole of what this unit takes from the record. The record also
   fixes the form of the bytes an object is made of, which is `serializer.py`'s to honour,
   not this module's -- this one decides which characters reach it, never how they are
   encoded.
"""

from __future__ import annotations

import codecs
import csv
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from damicore_normalizer.config import DelimitedSource
from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.table_split import TableScan, split_table, validate_table_shape

# A single field may be this many characters during the csv.reader passes. csv defaults to
# 131072, which pandas does not impose, so without this a well-formed wide cell would be
# reported as malformed. The value is a bound the C long behind field_size_limit accepts on
# every supported platform, not a contract: the input contract sets no field-size limit.
_MAX_FIELD_CHARS = 2**31 - 1


def _strip_bom(header: list[str], encoding: str) -> list[str]:
    """Drop a leading UTF-8 BOM from the first column name, as pandas' C parser does.

    Python's ``utf-8`` codec keeps the BOM, so without this the first name would carry it and
    the header check in :func:`iter_records` would reject a well-formed file. Decoding through
    ``utf-8-sig`` instead would buffer past the header, which would misreport an undecodable
    data row as a header failure.
    """
    if not header or codecs.lookup(encoding).name != "utf-8":
        return header
    return [header[0].removeprefix("\ufeff"), *header[1:]]


@contextmanager
def _relaxed_field_size_limit() -> Generator[None]:
    """Lift csv's field-size cap for the duration of a pass, or of one record's read.

    The cap is process-global state, so it is restored on exit; the passes it wraps are
    synchronous and do not run user code.
    """
    previous = csv.field_size_limit(_MAX_FIELD_CHARS)
    try:
        yield
    finally:
        csv.field_size_limit(previous)


def read_header(path: Path, source: DelimitedSource) -> list[str]:
    try:
        with (
            _relaxed_field_size_limit(),
            path.open("r", encoding=source.encoding, errors="strict", newline="") as stream,
        ):
            header = _strip_bom(
                next(csv.reader(stream, delimiter=source.delimiter)), source.encoding
            )
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        raise NormalizerError(
            "Could not read a valid delimited header", code="dataset_format_error"
        ) from exc
    return header


def validate_record_widths(path: Path, source: DelimitedSource, width: int) -> None:
    """Reject any record whose field count disagrees with the mandatory header.

    ``on_bad_lines="error"`` cannot express this rule on its own. When every data row carries
    surplus fields, the C parser reads the leading ones as an index and those cells disappear;
    when only a later row does, the surplus is dropped or reported depending on where the chunk
    boundary falls; a short row is padded with a cell the input never contained. The canonical
    bytes would then depend on ``chunk_rows``, which determinism forbids.

    ``csv.reader`` agrees with pandas on record boundaries and field counts once the two are
    configured to match, so one streaming pass over the raw records makes the check total.
    Two axes had to be aligned deliberately and are handled above: the BOM, which pandas
    strips and this codec does not, and the field-size cap, which pandas does not impose. A
    blank line is a third: ``skip_blank_lines=False`` defines it as a full-width empty row,
    and pandas materializes it as one.

    A quote opened and never closed is the fourth. It runs to the end of the file, so only the
    last record can end inside one, and ``csv.reader`` returns what it holds as a last field
    where pandas refused the file; that record is refused here.
    """
    lines: list[str] = []

    def kept(stream: Iterator[str]) -> Iterator[str]:
        for line in stream:
            lines.append(line)
            yield line

    try:
        with (
            _relaxed_field_size_limit(),
            path.open("r", encoding=source.encoding, errors="strict", newline="") as stream,
        ):
            reader = csv.reader(kept(stream), delimiter=source.delimiter)
            next(reader, None)
            last: list[str] = []
            for number, record in enumerate(reader, start=2):
                if record and len(record) != width:
                    raise NormalizerError(
                        f"Line {number} has {len(record)} fields but the header declares {width}",
                        code="dataset_format_error",
                    )
                last = lines.copy()
                lines.clear()
            if _ends_inside_a_quote(last, source.delimiter):
                raise NormalizerError(
                    "A quote opened in the last record is never closed",
                    code="dataset_format_error",
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise NormalizerError("Delimited parsing failed", code="dataset_format_error") from exc


def _ends_inside_a_quote(record_lines: list[str], delimiter: str) -> bool:
    """Whether the record these lines hold ends inside a quote opened in it.

    A quote read after its last line reads as nothing only there, where it closes the field;
    anywhere else the record has ended and the quote opens one more. Asked of the reading
    itself, so text after a closing quote, which ``strict`` would report first, cannot hide
    it.
    """
    if not record_lines:
        return False
    closed = [*record_lines, '"']
    reading = list(csv.reader(record_lines, delimiter=delimiter))
    return reading == list(csv.reader(closed, delimiter=delimiter))


def iter_records(
    path: Path,
    source: DelimitedSource,
    header: list[str],
    *,
    columns: Sequence[int] | None = None,
) -> Iterator[tuple[str, ...]]:
    """Stream the data rows as text, whole or restricted to ``columns`` by position.

    The seam every consumer of a delimited file meets: :func:`scan_delimited` splits what
    comes out of it and the partition ranks and routes it. It validates nothing structural
    itself; the caller runs :func:`validate_record_widths` first, once, so the width rule
    has one pass rather than one per consumer.

    One record is held at a time, so reading costs a row whatever the cells' lengths. Read
    through pandas in chunks, as it was until 0.7.1, each chunk held a Python string per
    cell, up to 13 times the chunk's serialized size, which preflight did not charge (issue
    #136). The cells are the ones that reader yielded: ``csv.reader`` takes the same quoting,
    decoding and line breaks, and a blank line is a full-width row of empty cells, as pandas
    materialized it under ``skip_blank_lines=False``.
    """
    width = len(header)
    with path.open("r", encoding=source.encoding, errors="strict", newline="") as stream:
        records = _relaxed_records(csv.reader(stream, delimiter=source.delimiter))
        first = next(records, None)
        if first is None or _strip_bom(first, source.encoding) != header:
            raise NormalizerError("Header changed while parsing", code="dataset_format_error")
        for record in records:
            cells = record or [""] * width
            yield tuple(cells) if columns is None else tuple(cells[index] for index in columns)


def _relaxed_records(reader: Iterator[list[str]]) -> Iterator[list[str]]:
    # The field-size cap is lifted for each read only, never across a yield: the consumer
    # runs between records, and another reader may be open on the same process-global cap.
    while True:
        with _relaxed_field_size_limit():
            record = next(reader, None)
        if record is None:
            return
        yield record


@contextmanager
def translating_parse_failures() -> Generator[None]:
    """Turn what the reader raises while streaming records into this package's one refusal.

    Owned here so every consumer of :func:`iter_records` refuses a malformed file with the
    same code and message rather than each translating the parser's exceptions itself.
    """
    try:
        yield
    except NormalizerError:
        raise
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        raise NormalizerError("Delimited parsing failed", code="dataset_format_error") from exc


def scan_delimited(
    path: Path,
    source: DelimitedSource,
    *,
    chunk_rows: int,
    max_open_files: int,
    objects_dir: Path | None = None,
) -> TableScan:
    """Scan one delimited file into canonical objects; optionally persist them."""
    header = read_header(path, source)
    validate_table_shape(header, source.split)

    # Structure is settled before anything is created, so a malformed file never leaves a
    # partially written objects directory behind.
    validate_record_widths(path, source, len(header))

    if objects_dir is not None:
        objects_dir.mkdir(parents=True, exist_ok=False)
    with translating_parse_failures():
        return split_table(
            header,
            iter_records(path, source, header),
            split=source.split,
            chunk_rows=chunk_rows,
            max_open_files=max_open_files,
            objects_dir=objects_dir,
        )
