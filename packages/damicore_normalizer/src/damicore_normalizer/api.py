from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import TypeVar, cast

from pydantic import ValidationError

from damicore_normalizer.config import (
    DelimitedSource,
    FileCorpusSource,
    NormalizationConfig,
    SpreadsheetSource,
)
from damicore_normalizer.delimited_reader import (
    iter_records,
    read_header,
    scan_delimited,
    translating_parse_failures,
    validate_record_widths,
)
from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.file_corpus import scan_corpus
from damicore_normalizer.manifest import (
    PARTITION_PARAMETER_RANGES,
    DelimitedDatasetInput,
    DelimitedPartitionInput,
    FileCorpusInput,
    NormalizationManifest,
    NormalizationResult,
    PartitionFile,
    PartitionManifest,
    PartitionMethod,
    PartitionResult,
    SpreadsheetDatasetInput,
    SpreadsheetPartitionInput,
)
from damicore_normalizer.numeric_column import DecimalSeparator, resolve_separator
from damicore_normalizer.partition import (
    file_intervals,
    rank_column,
    refuse_short_columns,
    route_rows,
)
from damicore_normalizer.scan import ScanResult
from damicore_normalizer.spreadsheet_reader import (
    CELL_TEXT_RULE,
    iter_used_rows,
    resolve_and_bound,
    scan_spreadsheet,
    translating_workbook_failures,
)
from damicore_normalizer.table_split import validate_header_names


def _sha256(path: Path, chunk_size: int = 4_194_304) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    # Every caller here creates the directory first, so this is alignment rather than a fix:
    # the three packages carry a same-named writer with the same purpose, and one of them
    # silently requiring a precondition the others supply is a trap for the next caller.
    # ADR 0012 records why the three stay separate copies.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _resolved_paths(source: str | Path | Sequence[str | Path]) -> tuple[Path, ...]:
    """Normalize the caller's source argument into resolved paths, rejecting anything else.

    A public boundary, so the entries are checked rather than assumed: the annotation binds a
    type checker, not a notebook. ``bytes`` is the case that matters, because it satisfies
    ``Sequence`` and would otherwise be taken apart into one integer per byte, each of which
    fails deep inside ``Path`` with a message about ``int``.
    """
    entries: list[object] = (
        [source] if isinstance(source, (str, Path)) else list(cast(Sequence[object], source))
    )
    if not entries:
        raise NormalizerError("No input path was given", code="input_validation_error")
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, (str, Path)):
            raise NormalizerError(
                f"Input path must be a string or a path, not {type(entry).__name__}",
                code="input_validation_error",
            )
        paths.append(Path(entry).resolve())
    return tuple(paths)


def _dataset_path(paths: tuple[Path, ...]) -> Path:
    if len(paths) != 1:
        raise NormalizerError(
            "A dataset source takes exactly one file; pass a files source to cluster several",
            code="input_validation_error",
        )
    path = paths[0]
    if not path.is_file():
        raise NormalizerError(
            f"Input path is not a regular file: {path}",
            code="input_validation_error",
        )
    return path


def scan_source(
    source: str | Path | Sequence[str | Path],
    config: NormalizationConfig,
    *,
    objects_dir: Path | None = None,
) -> ScanResult:
    """Measure the objects a source produces, writing them only when asked to.

    This is the single place the source axis is decided. Preflight calls it with no
    ``objects_dir`` and :func:`materialize_objects` calls it with one, so a projection and a
    real run cannot disagree about object count, bytes, or identifiers: they are the same
    traversal.
    """
    paths = _resolved_paths(source)
    settings = config.source

    if isinstance(settings, FileCorpusSource):
        corpus = scan_corpus(paths, settings, objects_dir=objects_dir)
        return ScanResult(
            objects=corpus.objects,
            total_bytes=corpus.total_bytes,
            max_serialized_chunk_bytes=corpus.largest_file_bytes,
            manifest_input=FileCorpusInput(
                kind="files",
                root=str(corpus.root),
                sha256=corpus.set_digest,
                size_bytes=corpus.total_bytes,
                file_count=len(corpus.objects),
                recursive=settings.recursive,
                include_hidden=settings.include_hidden,
            ),
            object_encoding="raw-bytes/1",
            source_paths=tuple(path for path, _, _ in corpus.stats),
            source_fingerprints=tuple((size, mtime) for _, size, mtime in corpus.stats),
        )

    path = _dataset_path(paths)
    before = path.stat()
    if isinstance(settings, SpreadsheetSource):
        table, sheet = scan_spreadsheet(
            path,
            settings,
            chunk_rows=config.chunk_rows,
            max_open_files=config.max_open_files,
            objects_dir=objects_dir,
        )
        dataset_input = SpreadsheetDatasetInput(
            kind="xlsx",
            path=str(path),
            sha256=_sha256(path),
            size_bytes=before.st_size,
            sheet=sheet,
            split=settings.split,
            cell_text_rule=CELL_TEXT_RULE,
        )
    else:
        delimited: DelimitedSource = settings
        table = scan_delimited(
            path,
            delimited,
            chunk_rows=config.chunk_rows,
            max_open_files=config.max_open_files,
            objects_dir=objects_dir,
        )
        dataset_input = DelimitedDatasetInput(
            kind="delimited",
            path=str(path),
            sha256=_sha256(path),
            size_bytes=before.st_size,
            delimiter=delimited.delimiter,
            encoding=delimited.encoding,
            split=delimited.split,
        )
    return ScanResult(
        objects=table.objects,
        total_bytes=table.total_bytes,
        max_serialized_chunk_bytes=table.max_serialized_chunk_bytes,
        manifest_input=dataset_input,
        object_encoding="json-lines/1",
        source_paths=(path,),
        source_fingerprints=((before.st_size, before.st_mtime_ns),),
    )


def _fingerprints(paths: Sequence[Path]) -> tuple[tuple[int, int], ...]:
    stats = [path.stat() for path in paths]
    return tuple((item.st_size, item.st_mtime_ns) for item in stats)


def _require_unchanged(
    paths: Sequence[Path],
    expected: tuple[tuple[int, int], ...],
    activity: str,
) -> None:
    """Refuse to go on if an input's size or mtime moved since ``expected`` was taken."""
    try:
        current = _fingerprints(paths)
    except OSError as exc:
        raise NormalizerError(f"Input disappeared during {activity}", code="input_drift") from exc
    if current != expected:
        raise NormalizerError(f"Input changed during {activity}", code="input_drift")


def _require_absent_or_empty(destination: Path) -> None:
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise NormalizerError("output_dir must be absent or empty", code="output_conflict_error")


def materialize_objects(
    source: str | Path | Sequence[str | Path],
    output_dir: str | Path,
    *,
    config: NormalizationConfig | None = None,
) -> NormalizationResult:
    """Turn an input source into deterministic versioned object artifacts.

    Writes ``manifest.json`` and one object file under ``objects/`` in ``output_dir``, which
    must be absent or empty. What one object is follows ``config.source``: one column or one
    data row of a dataset, or one adopted file. Every written object is re-read and checked
    against its recorded size and SHA-256, and every input file is re-stat'd afterwards, so
    the manifest is only written once the artifacts and their sources have been shown to
    agree. That manifest is the input :func:`damicore_distance.compute_distance_matrix`
    expects.

    Raises
    ------
    NormalizerError
        An input path is missing, of the wrong kind, or unreadable
        (``input_validation_error``); ``output_dir`` exists and is not empty
        (``output_conflict_error``); a dataset violates the input contract
        (``dataset_format_error``); a corpus violates the corpus rules
        (``corpus_validation_error``); an input changed while it was being read
        (``input_drift``); a written object does not match its digest
        (``artifact_validation_error``).
    """
    settings = config or NormalizationConfig()
    destination = Path(output_dir).resolve()
    _require_absent_or_empty(destination)
    destination.mkdir(parents=True, exist_ok=True)

    scan = scan_source(source, settings, objects_dir=destination / "objects")
    _require_unchanged(scan.source_paths, scan.source_fingerprints, "normalization")
    for item in scan.objects:
        object_path = destination / item.relative_path
        if object_path.stat().st_size != item.size_bytes or _sha256(object_path) != item.sha256:
            raise NormalizerError(
                f"normalized object failed validation: {item.object_id}",
                code="artifact_validation_error",
            )
    manifest_path = destination / "manifest.json"
    manifest = NormalizationManifest(
        schema_version=2,
        object_encoding=scan.object_encoding,
        input=scan.manifest_input,
        objects=scan.objects,
    )
    _atomic_json(manifest_path, manifest.model_dump(mode="json"))
    return NormalizationResult(
        manifest_path=manifest_path,
        object_count=len(scan.objects),
        total_bytes=scan.total_bytes,
        objects=scan.objects,
    )


def normalize_csv(
    csv_path: str | Path,
    output_dir: str | Path,
    *,
    config: NormalizationConfig | None = None,
) -> NormalizationResult:
    """Normalize one delimited-text file. A thin wrapper over :func:`materialize_objects`.

    Kept for callers written against 0.1. It accepts only a delimited source, which is what
    its name promises; every other source goes through :func:`materialize_objects`.
    """
    settings = config or NormalizationConfig()
    if not isinstance(settings.source, DelimitedSource):
        raise NormalizerError(
            "normalize_csv only accepts a delimited source; use materialize_objects",
            code="input_validation_error",
        )
    return materialize_objects(csv_path, output_dir, config=settings)


# The settings each source kind defines, derived from the source models minus the two that a
# partition has no use for: `kind` names the variant and `split` does not apply. Deriving
# them is what stops a field added to a source model from going unthreaded silently.
_PARTITION_SETTINGS: dict[str, frozenset[str]] = {
    "delimited": frozenset(DelimitedSource.model_fields) - {"kind", "split"},
    "xlsx": frozenset(SpreadsheetSource.model_fields) - {"kind", "split"},
}
_SETTING_DEFAULTS: dict[str, object] = {
    "delimiter": DelimitedSource.model_fields["delimiter"].default,
    "encoding": DelimitedSource.model_fields["encoding"].default,
    "sheet": SpreadsheetSource.model_fields["sheet"].default,
}

_T = TypeVar("_T")


def _guarded(
    items: Iterator[_T],
    guard: Callable[[], AbstractContextManager[None]],
) -> Iterator[_T]:
    """Run a reader's failure translation around advancing ``items`` and nothing else.

    A generator's frame is only active while it is being advanced, so an exception raised
    by the consumer between two items -- while writing a file, say -- never reaches the
    guard. Only what the reader raises is translated, which is the guard's whole contract.
    """
    with guard():
        yield from items


def _validated_parameters(name: str, values: object, method: PartitionMethod) -> tuple[int, ...]:
    """Accept a list or tuple of ints in the method's range, sorted, without repeats."""
    if not isinstance(values, (list, tuple)):
        raise NormalizerError(
            f"{name} must be a list or tuple of integers", code="input_validation_error"
        )
    low, high = PARTITION_PARAMETER_RANGES[method]
    checked: list[int] = []
    for value in cast(Sequence[object], values):
        if not isinstance(value, int) or isinstance(value, bool):
            raise NormalizerError(
                f"{name} must contain only integers, not {type(value).__name__}",
                code="input_validation_error",
            )
        if value < low or (high is not None and value > high):
            bounds = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise NormalizerError(
                f"{name} values must be {bounds}; got {value}", code="input_validation_error"
            )
        checked.append(value)
    if len(set(checked)) != len(checked):
        raise NormalizerError(f"{name} must not repeat a value", code="input_validation_error")
    return tuple(sorted(checked))


def _validated_decimal(decimal: str | None) -> DecimalSeparator | None:
    if decimal is None:
        return None
    if decimal == ".":
        return "."
    if decimal == ",":
        return ","
    raise NormalizerError(
        "decimal must be None, '.' or ','; it is the decimal separator of the target column",
        code="input_validation_error",
    )


def _partition_settings(
    source_kind: str,
    *,
    delimiter: str,
    encoding: str,
    sheet: str | None,
) -> DelimitedSource | SpreadsheetSource:
    """Map the flat arguments onto the reader settings, refusing one that does not apply."""
    if source_kind not in _PARTITION_SETTINGS:
        raise NormalizerError(
            "source_kind must be exactly 'delimited' or 'xlsx'", code="input_validation_error"
        )
    given: dict[str, object] = {"delimiter": delimiter, "encoding": encoding, "sheet": sheet}
    rejected = sorted(
        name
        for name, value in given.items()
        if name not in _PARTITION_SETTINGS[source_kind] and value != _SETTING_DEFAULTS[name]
    )
    if rejected:
        raise NormalizerError(
            f"{', '.join(rejected)} does not apply to a {source_kind} source",
            code="input_validation_error",
        )
    try:
        if source_kind == "xlsx":
            return SpreadsheetSource(sheet=sheet)
        return DelimitedSource(delimiter=delimiter, encoding=encoding)
    except (ValidationError, LookupError) as exc:
        # codecs.lookup raises LookupError for an unknown encoding, which Pydantic does not
        # turn into a ValidationError, so both are the same refusal here.
        raise NormalizerError(
            f"Invalid setting for a {source_kind} source: {exc}", code="input_validation_error"
        ) from exc


def _column_index(header: Sequence[str], column: str) -> int:
    if column not in header:
        raise NormalizerError(
            f"Column {column!r} is not in the header; available: {', '.join(header)}",
            code="dataset_format_error",
        )
    return list(header).index(column)


@dataclass(frozen=True)
class _PartitionReader:
    """One input behind the reader seam: its header, the target column, and its rows.

    ``cells`` and ``rows`` return a fresh iterator on each call, because passes A and B read
    the column twice and pass C reads the table once, each as its own streaming pass.
    """

    header: list[str]
    cells: Callable[[], Iterator[str]]
    rows: Callable[[], Iterator[tuple[str, ...]]]
    describe: Callable[[str, int], DelimitedPartitionInput | SpreadsheetPartitionInput]


def _partition_reader(
    path: Path,
    settings: DelimitedSource | SpreadsheetSource,
    column: str,
    chunk_rows: int,
) -> _PartitionReader:
    if isinstance(settings, DelimitedSource):
        header = read_header(path, settings)
        validate_header_names(header)
        index = _column_index(header, column)
        validate_record_widths(path, settings, len(header))
        delimited = settings

        def delimited_cells() -> Iterator[str]:
            records = iter_records(path, delimited, header, chunk_rows, columns=[index])
            return _guarded((row[0] for row in records), translating_parse_failures)

        def delimited_rows() -> Iterator[tuple[str, ...]]:
            records = iter_records(path, delimited, header, chunk_rows)
            return _guarded(records, translating_parse_failures)

        def describe_delimited(sha256: str, size_bytes: int) -> DelimitedPartitionInput:
            return DelimitedPartitionInput(
                kind="delimited",
                path=str(path),
                sha256=sha256,
                size_bytes=size_bytes,
                delimiter=delimited.delimiter,
                encoding=delimited.encoding,
            )

        return _PartitionReader(header, delimited_cells, delimited_rows, describe_delimited)

    sheet, bounds = resolve_and_bound(path, settings)
    with translating_workbook_failures():
        header = list(next(iter_used_rows(path, sheet, bounds)))
    validate_header_names(header)
    index = _column_index(header, column)

    def worksheet_cells() -> Iterator[str]:
        rows = islice(iter_used_rows(path, sheet, bounds, columns=[index]), 1, None)
        return _guarded((row[0] for row in rows), translating_workbook_failures)

    def worksheet_rows() -> Iterator[tuple[str, ...]]:
        rows = islice(iter_used_rows(path, sheet, bounds), 1, None)
        return _guarded(rows, translating_workbook_failures)

    def describe_worksheet(sha256: str, size_bytes: int) -> SpreadsheetPartitionInput:
        return SpreadsheetPartitionInput(
            kind="xlsx",
            path=str(path),
            sha256=sha256,
            size_bytes=size_bytes,
            sheet=sheet,
            cell_text_rule=CELL_TEXT_RULE,
        )

    return _PartitionReader(header, worksheet_cells, worksheet_rows, describe_worksheet)


def partition_dataset(
    source: str | Path,
    output_dir: str | Path,
    *,
    column: str,
    quantiles: Sequence[int] = (2, 4, 8, 16, 32),
    percentiles: Sequence[int] = (5, 10, 20),
    jenks_classes: Sequence[int] = (2, 3, 4, 5),
    decimal: str | None = None,
    source_kind: str = "delimited",
    delimiter: str = ",",
    encoding: str = "utf-8",
    sheet: str | None = None,
    chunk_rows: int = 50_000,
) -> PartitionResult:
    """Split a dataset into high and low subsets by a numeric column, with a manifest.

    For every requested partition -- ``quantile`` q, ``percentile`` p, or ``jenks`` k
    classes -- writes the rows with the highest values and the rows with the lowest values
    to ``{method}_{parameter:02d}_{high|low}.csv`` under ``output_dir``, which must be
    absent or empty, then ``partition.json``. Each file is a pure subset of the input in
    input order, cells unchanged, in the library's default dataset form (``,``, UTF-8, LF),
    so it feeds :func:`materialize_objects` or ``damicore.run`` with no further argument.
    Rows are ordered by (value descending, file position ascending) under ``partition_rule``
    v1, whose numeric grammar, separator resolution, method definitions and tie-breaks are
    specified in ``docs/dataset-partitioning.md`` and decided in ADR 0014.

    ``decimal`` is the target column's decimal separator, ``.`` or ``,``; ``None`` resolves
    it by testing both against every cell and refusing when neither fits. Parameters are
    lists or tuples of ints, sorted here, an empty one disabling its method. ``delimiter``
    and ``encoding`` apply to a delimited source and ``sheet`` to an ``xlsx`` one; a
    non-default setting that does not apply is rejected.

    Every data-dependent refusal happens before any file is created, and a failure after
    that leaves a directory without ``partition.json``, which is the incomplete state. No
    message or artifact carries a cell value.

    Raises
    ------
    NormalizerError
        An argument is invalid (``input_validation_error``); ``output_dir`` exists and is not
        empty (``output_conflict_error``); the header, the column, a cell of it, or a
        precondition of a requested method is violated (``dataset_format_error``); the input
        changed while it was being read (``input_drift``); an emitted file does not match
        its digest (``artifact_validation_error``).
    """
    # A public boundary: the annotation binds a type checker, not a notebook, so the two
    # runtime checks below look redundant to Pyright and are not.
    if not isinstance(cast(object, source), (str, Path)):
        raise NormalizerError(
            "source must be one path, a string or a Path", code="input_validation_error"
        )
    settings = _partition_settings(source_kind, delimiter=delimiter, encoding=encoding, sheet=sheet)
    quantile_set = _validated_parameters("quantiles", quantiles, "quantile")
    percentile_set = _validated_parameters("percentiles", percentiles, "percentile")
    jenks_set = _validated_parameters("jenks_classes", jenks_classes, "jenks")
    if not (quantile_set or percentile_set or jenks_set):
        raise NormalizerError(
            "At least one of quantiles, percentiles and jenks_classes must be non-empty",
            code="input_validation_error",
        )
    declared = _validated_decimal(decimal)
    rows_per_chunk = cast(object, chunk_rows)
    if not isinstance(rows_per_chunk, int) or isinstance(rows_per_chunk, bool) or chunk_rows < 1:
        raise NormalizerError(
            "chunk_rows must be a positive integer", code="input_validation_error"
        )
    path = _dataset_path(_resolved_paths(source))
    destination = Path(output_dir).resolve()
    _require_absent_or_empty(destination)

    fingerprint = _fingerprints((path,))
    digest = _sha256(path)
    reader = _partition_reader(path, settings, column, chunk_rows)

    separator, row_count = resolve_separator(reader.cells(), column, declared)
    _require_unchanged((path,), fingerprint, "partition")
    refuse_short_columns(quantile_set, percentile_set, row_count)

    ranking = rank_column(reader.cells(), separator, row_count)
    _require_unchanged((path,), fingerprint, "partition")
    intervals = file_intervals(quantile_set, percentile_set, jenks_set, ranking)

    destination.mkdir(parents=True, exist_ok=True)
    _require_absent_or_empty(destination)
    written = route_rows(reader.rows(), reader.header, ranking.rank, intervals, destination)
    _require_unchanged((path,), fingerprint, "partition")

    files: list[PartitionFile] = []
    for item, (size_bytes, sha256) in zip(intervals, written, strict=True):
        record = PartitionFile(
            method=item.method,
            parameter=item.parameter,
            end=item.end,
            rank_start=item.rank_start,
            rank_end=item.rank_end,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        target = destination / record.relative_path
        if target.stat().st_size != size_bytes or _sha256(target) != sha256:
            raise NormalizerError(
                f"partition file failed validation: {record.relative_path}",
                code="artifact_validation_error",
            )
        files.append(record)

    manifest = PartitionManifest(
        schema_version=1,
        partition_rule="v1",
        input=reader.describe(digest, fingerprint[0][0]),
        column=column,
        decimal=separator,
        row_count=row_count,
        files=tuple(files),
    )
    manifest_path = destination / "partition.json"
    _atomic_json(manifest_path, manifest.model_dump(mode="json"))
    return PartitionResult(manifest_path=manifest_path, manifest=manifest)
