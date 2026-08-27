from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from damicore_normalizer.config import (
    DelimitedSource,
    FileCorpusSource,
    NormalizationConfig,
    SpreadsheetSource,
)
from damicore_normalizer.delimited_reader import scan_delimited
from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.file_corpus import scan_corpus
from damicore_normalizer.manifest import (
    DelimitedDatasetInput,
    FileCorpusInput,
    NormalizationManifest,
    NormalizationResult,
    SpreadsheetDatasetInput,
)
from damicore_normalizer.scan import ScanResult
from damicore_normalizer.spreadsheet_reader import CELL_TEXT_RULE, scan_spreadsheet


def _sha256(path: Path, chunk_size: int = 4_194_304) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
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
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise NormalizerError("output_dir must be absent or empty", code="output_conflict_error")
    destination.mkdir(parents=True, exist_ok=True)

    scan = scan_source(source, settings, objects_dir=destination / "objects")
    try:
        current = _fingerprints(scan.source_paths)
    except OSError as exc:
        raise NormalizerError("Input disappeared during normalization", code="input_drift") from exc
    if current != scan.source_fingerprints:
        raise NormalizerError("Input changed during normalization", code="input_drift")
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
