from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.manifest import ObjectDescriptor
from damicore_normalizer.serializer import serialize_cell, serialize_row


@dataclass(frozen=True)
class TableScan:
    objects: tuple[ObjectDescriptor, ...]
    total_bytes: int
    max_serialized_chunk_bytes: int
    row_count: int


class _FilePool:
    def __init__(self, root: Path, limit: int) -> None:
        self._root = root
        self._limit = limit
        self._streams: OrderedDict[str, BinaryIO] = OrderedDict()

    def write(self, name: str, payload: bytes) -> None:
        stream = self._streams.pop(name, None)
        if stream is None:
            if len(self._streams) >= self._limit:
                _, oldest = self._streams.popitem(last=False)
                oldest.close()
            stream = (self._root / name).open("ab")
        self._streams[name] = stream
        stream.write(payload)

    def close(self) -> None:
        for stream in self._streams.values():
            stream.close()
        self._streams.clear()


def validate_header_names(header: Sequence[str]) -> None:
    """Reject a header whose names cannot label anything: empty, or repeated.

    The part of the table contract that holds for every consumer of a header, split or not.
    :func:`validate_table_shape` adds the split-specific minimum on top of it; the partition,
    which has no split, calls this alone.
    """
    if not header or any(name == "" for name in header):
        raise NormalizerError("Header names must be non-empty", code="dataset_format_error")
    if len(set(header)) != len(header):
        raise NormalizerError("Header names must be unique", code="dataset_format_error")


def validate_table_shape(header: Sequence[str], split: Literal["columns", "rows"]) -> None:
    """Reject a header that cannot produce objects, before anything is created.

    Applied by every dataset format, so a spreadsheet and a delimited file are refused for
    the same reasons with the same codes rather than each format inventing its own.
    """
    validate_header_names(header)
    if split == "columns" and len(header) < 2:
        raise NormalizerError(
            "columns split requires at least two columns",
            code="dataset_format_error",
        )


def split_table(
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    split: Literal["columns", "rows"],
    chunk_rows: int,
    max_open_files: int,
    objects_dir: Path | None,
) -> TableScan:
    """Turn a header plus a stream of text rows into canonical objects.

    This is the whole of what "split by column or by row" means, shared by every dataset
    format: a format's reader owns parsing and validating its own file, and owns nothing
    about what an object is. The rows are consumed lazily, so a format that streams keeps
    streaming through here.

    ``objects_dir`` decides whether the bytes are written or only measured. Both paths run
    the same arithmetic, which is what lets preflight predict a run exactly.
    """
    column_hashes = [hashlib.sha256() for _ in header]
    column_sizes = [0 for _ in header]
    row_objects: list[ObjectDescriptor] = []
    max_chunk_bytes = 0
    chunk_bytes = 0
    row_count = 0
    pool = _FilePool(objects_dir, max_open_files) if objects_dir is not None else None

    try:
        for values in rows:
            # Unreachable from either reader today, and kept deliberately: the delimited
            # reader validates every record width before this point and the spreadsheet
            # reader slices each row to the used range, so neither can deliver a ragged row.
            # It is the shared core's own contract, and a third reader would meet it here
            # rather than discovering the invariant by corrupting object bytes.
            if len(values) != len(header):
                raise NormalizerError(
                    f"Record {row_count + 1} has {len(values)} fields but the header declares "
                    f"{len(header)}",
                    code="dataset_format_error",
                )
            row_count += 1
            if split == "rows":
                payload = serialize_row(values)
                object_id = f"row_{row_count:06d}"
                row_objects.append(
                    ObjectDescriptor(
                        object_id=object_id,
                        label=object_id,
                        relative_path=f"objects/{object_id}.jsonl",
                        size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                    )
                )
                if objects_dir is not None:
                    (objects_dir / f"{object_id}.jsonl").write_bytes(payload)
                chunk_bytes += len(payload)
            else:
                for index, value in enumerate(values):
                    payload = serialize_cell(value)
                    column_hashes[index].update(payload)
                    column_sizes[index] += len(payload)
                    chunk_bytes += len(payload)
                    if pool is not None:
                        pool.write(f"column_{index + 1:06d}.jsonl", payload)
            if row_count % chunk_rows == 0:
                max_chunk_bytes = max(max_chunk_bytes, chunk_bytes)
                chunk_bytes = 0
        max_chunk_bytes = max(max_chunk_bytes, chunk_bytes)
    finally:
        if pool is not None:
            pool.close()

    if row_count == 0 or (split == "rows" and row_count < 2):
        raise NormalizerError(
            "Dataset does not contain enough data rows", code="dataset_format_error"
        )

    if split == "columns":
        objects = tuple(
            ObjectDescriptor(
                object_id=f"column_{index + 1:06d}",
                label=label,
                relative_path=f"objects/column_{index + 1:06d}.jsonl",
                size_bytes=column_sizes[index],
                sha256=column_hashes[index].hexdigest(),
            )
            for index, label in enumerate(header)
        )
    else:
        objects = tuple(row_objects)
    return TableScan(
        objects=objects,
        total_bytes=sum(item.size_bytes for item in objects),
        max_serialized_chunk_bytes=max_chunk_bytes,
        row_count=row_count,
    )
