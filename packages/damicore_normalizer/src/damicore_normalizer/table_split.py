"""Split a dataset into objects, by column or by row.

References
----------
.. [adr-0006] The object source is an axis owned by the normalizer. A dataset is split into
   objects and files already are them; this unit is the first half of that axis. What it
   does not take from the record is the second half: a corpus of files is `file_corpus.py`'s,
   and the two must not grow a shared special case, which is what made the axis a record
   rather than a pair of code paths.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from io import FileIO
from pathlib import Path
from typing import Literal

from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.manifest import ObjectDescriptor
from damicore_normalizer.serializer import serialize_cell, serialize_row


@dataclass(frozen=True)
class TableScan:
    objects: tuple[ObjectDescriptor, ...]
    total_bytes: int
    max_serialized_chunk_bytes: int
    row_count: int


# The column bytes held before they are written, across all columns. A split writes the cells
# of a row to every column in turn, so writing each cell as it comes opens every file once per
# row as soon as the files do not all fit `max_open_files` (issue #134). Held to this budget,
# each file is opened once per budget's worth of bytes instead. Measured on tables of 80 and
# 1,000 columns, 4 to 8 MiB normalized as fast as keeping every file open, and 1 MiB was 5%
# slower at 1,000 columns; 8 MiB gives wider tables the larger writes, at a fixed cost.
_WRITE_BUFFER_BYTES = 8 * 1_048_576


class _ColumnWriter:
    """Append each column's bytes to its object file, holding at most a fixed budget of them.

    A table no wider than ``limit`` keeps every file open for the whole split, as before. A
    wider one keeps its first ``limit - 1`` files open and opens each of the rest once per
    flush, one at a time, so no more than ``limit`` object files are ever open at once. A flush
    is due once the next cell would not fit, so each of those opens writes on average close to
    a column's share of it; only rows larger than the budget bring back an open per cell, as
    before this writer. The bytes held never exceed the budget: a larger cell is written as it
    comes.

    The bytes of every column reach its file in the order they came, so the objects do not
    depend on when the buffers are flushed. Beside the budget it holds one buffer per column,
    as `split_table` already holds a digest and a size per column.
    """

    def __init__(self, root: Path, columns: int, limit: int) -> None:
        self._root = root
        self._kept = columns if columns <= limit else limit - 1
        self._buffers = [bytearray() for _ in range(columns)]
        self._held = 0
        self._streams: list[FileIO] = []

    def _path(self, index: int) -> Path:
        return self._root / f"column_{index + 1:06d}.jsonl"

    def write(self, index: int, payload: bytes) -> None:
        # Never past the budget: what does not fit flushes what is held first, and a cell the
        # budget cannot hold is written as it is, after the bytes of its column that came first.
        if self._held + len(payload) > _WRITE_BUFFER_BYTES:
            self.flush()
            if len(payload) > _WRITE_BUFFER_BYTES:
                self._write_out(index, payload)
                return
        self._buffers[index] += payload
        self._held += len(payload)

    def flush(self) -> None:
        for index, buffer in enumerate(self._buffers):
            if buffer:
                self._write_out(index, buffer)
                buffer.clear()
        self._held = 0

    def _open(self, index: int) -> FileIO:
        # Unbuffered: the column buffer already batches the writes, and a buffer per open file
        # would hold up to a block of bytes each beside it -- 8 MiB more for 2,000 kept files --
        # and defer their write, and its errors, to the close.
        return self._path(index).open("ab", buffering=0)

    def _write_out(self, index: int, data: bytes | bytearray) -> None:
        if index < len(self._streams):
            _write_all(self._streams[index], data)
        elif index < self._kept and index == len(self._streams):
            self._streams.append(self._open(index))
            _write_all(self._streams[index], data)
        else:
            stream = self._open(index)
            try:
                _write_all(stream, data)
            except BaseException:
                _close_quietly(stream)
                raise
            stream.close()

    def close(self) -> None:
        """Close every kept file after the last flush. Every byte was written before, so
        closing writes nothing; an error here is still raised, after every file is closed."""
        streams, self._streams = self._streams, []
        with ExitStack() as closing:
            for stream in streams:
                closing.callback(stream.close)

    def abandon(self) -> None:
        """Close every file without reporting what closing them raises, so the error that
        stopped the split is the one that propagates; what is held is dropped."""
        for stream in self._streams:
            _close_quietly(stream)
        self._streams = []
        self._buffers = []
        self._held = 0


def _write_all(stream: FileIO, data: bytes | bytearray) -> None:
    # A raw file may take less than it is handed; what it does not take is handed again.
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:
            raise OSError(f"{stream.name} took no bytes")
        view = view[written:]


def _close_quietly(stream: FileIO) -> None:
    # Only on a path already raising: the object is incomplete either way, and closing it
    # must not replace the error that says why.
    with suppress(OSError):
        stream.close()


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
    writer = (
        _ColumnWriter(objects_dir, len(header), max_open_files)
        if objects_dir is not None and split == "columns"
        else None
    )

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
                    if writer is not None:
                        writer.write(index, payload)
            if row_count % chunk_rows == 0:
                max_chunk_bytes = max(max_chunk_bytes, chunk_bytes)
                chunk_bytes = 0
        max_chunk_bytes = max(max_chunk_bytes, chunk_bytes)
        if writer is not None:
            writer.flush()
    except BaseException:
        if writer is not None:
            writer.abandon()
        raise
    if writer is not None:
        writer.close()

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
