from __future__ import annotations

import codecs
import csv
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

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

    Python's ``utf-8`` codec keeps the BOM, so without this the two readers disagree on the
    first name and the per-chunk header check rejects a well-formed file. Decoding through
    ``utf-8-sig`` instead would buffer past the header, which would misreport an undecodable
    data row as a header failure.
    """
    if not header or codecs.lookup(encoding).name != "utf-8":
        return header
    return [header[0].removeprefix("\ufeff"), *header[1:]]


@contextmanager
def _relaxed_field_size_limit() -> Generator[None]:
    """Lift csv's field-size cap for the duration of a validation pass.

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
    """
    try:
        with (
            _relaxed_field_size_limit(),
            path.open("r", encoding=source.encoding, errors="strict", newline="") as stream,
        ):
            reader = csv.reader(stream, delimiter=source.delimiter)
            next(reader, None)
            for number, record in enumerate(reader, start=2):
                if record and len(record) != width:
                    raise NormalizerError(
                        f"Line {number} has {len(record)} fields but the header declares {width}",
                        code="dataset_format_error",
                    )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise NormalizerError("Delimited parsing failed", code="dataset_format_error") from exc


def iter_records(
    path: Path,
    source: DelimitedSource,
    header: list[str],
    chunk_rows: int,
    *,
    columns: Sequence[int] | None = None,
) -> Iterator[tuple[str, ...]]:
    """Stream the data rows as text, whole or restricted to ``columns`` by position.

    The seam every consumer of a delimited file meets: :func:`scan_delimited` splits what
    comes out of it and the partition ranks and routes it. It validates nothing structural
    itself; the caller runs :func:`validate_record_widths` first, once, so the width rule
    has one pass rather than one per consumer.
    """
    expected = header if columns is None else [header[index] for index in columns]
    chunks = pd.read_csv(
        path,
        sep=source.delimiter,
        encoding=source.encoding,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        skip_blank_lines=False,
        chunksize=chunk_rows,
        on_bad_lines="error",
        quotechar='"',
        doublequote=True,
        comment=None,
        # No inference of any kind, including an index inferred from row width. Records
        # are already known to match the header, so this only pins the parser's contract.
        index_col=False,
        usecols=None if columns is None else list(columns),
    )
    for chunk in chunks:
        if list(chunk.columns) != expected:
            raise NormalizerError("Header changed while parsing", code="dataset_format_error")
        for values in chunk.itertuples(index=False, name=None):
            yield tuple(str(value) for value in values)


@contextmanager
def translating_parse_failures() -> Generator[None]:
    """Turn what pandas raises while streaming records into this package's one refusal.

    Owned here so every consumer of :func:`iter_records` refuses a malformed file with the
    same code and message rather than each translating the parser's exceptions itself.
    ``pd.errors.ParserError`` is a ``ValueError`` subclass, so the tuple covers it.
    """
    try:
        yield
    except NormalizerError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
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
            iter_records(path, source, header, chunk_rows),
            split=source.split,
            chunk_rows=chunk_rows,
            max_open_files=max_open_files,
            objects_dir=objects_dir,
        )
