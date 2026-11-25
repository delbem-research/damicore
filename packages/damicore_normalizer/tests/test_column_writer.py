"""The column split's writer: how often it opens object files, and what it leaves behind.

Issue #134: with more columns than ``max_open_files`` the writer opened and closed a file for
every cell. These tests hold the writer to the cost of a split, not to its mechanism.
"""

import csv
import json
import os
import sys
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

import damicore_normalizer.table_split as table_split
from damicore_normalizer import NormalizationConfig, normalize_csv

pytestmark = pytest.mark.unit

# Wider than the default `max_open_files` (64), which is where the writer used to thrash.
_COLUMNS = 70
_CELLS = ["", "a", "a,b", 'say "hi"', "line\nbreak", "ação ✓", "  padded  ", "12.50"]

_opened: list[str] | None = None


def _audit(event: str, arguments: tuple[object, ...]) -> None:
    # Every file Python opens raises the "open" audit event, whichever API opened it, so the
    # count does not depend on how the writer reaches the file system.
    if event == "open" and _opened is not None:
        path = arguments[0]
        if isinstance(path, str | Path) and str(path).endswith(".jsonl"):
            _opened.append(str(path))


sys.addaudithook(_audit)


def _wide_csv(path: Path, rows: int) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"c{index}" for index in range(_COLUMNS)])
        for row in range(rows):
            writer.writerow([_CELLS[(row + index) % len(_CELLS)] for index in range(_COLUMNS)])
    return path


def _openings(source: Path, output: Path, config: NormalizationConfig) -> list[str]:
    global _opened
    _opened = []
    try:
        normalize_csv(source, output, config=config)
        return _opened
    finally:
        _opened = None


def test_a_wide_column_split_opens_object_files_independently_of_the_row_count(
    tmp_path: Path,
) -> None:
    """Splitting a table wider than `max_open_files` costs a bounded number of opens per
    object file, not one per cell: four times the rows must not open any file more often."""
    small = _wide_csv(tmp_path / "small.csv", 200)
    large = _wide_csv(tmp_path / "large.csv", 800)
    config = NormalizationConfig()
    assert _COLUMNS > config.max_open_files
    few = _openings(small, tmp_path / "few", config)
    many = _openings(large, tmp_path / "many", config)
    assert len(many) == len(few), (len(few), len(many))
    assert len(many) <= 2 * _COLUMNS


def test_the_writer_never_holds_more_object_files_open_than_max_open_files(
    tmp_path: Path,
) -> None:
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("counting open descriptors needs /proc")
    source = _wide_csv(tmp_path / "wide.csv", 300)
    objects = tmp_path / "out"
    most = [0]

    def holding() -> int:
        count = 0
        for descriptor in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                continue
            count += target.startswith(str(objects)) and target.endswith(".jsonl")
        return count

    global _opened

    class Watch(list[str]):
        def append(self, item: str) -> None:
            most[0] = max(most[0], holding() + 1)
            super().append(item)

    _opened = Watch()
    try:
        normalize_csv(source, objects, config=NormalizationConfig(max_open_files=3))
    finally:
        _opened = None
    assert 0 < most[0] <= 3


def _expected_columns(source: Path) -> list[bytes]:
    """The column objects ADR 0001 defines, built from the CSV without the normalizer: one
    compact JSON string per cell, UTF-8, LF-terminated, in row order."""
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))[1:]
    return [
        b"".join(
            (json.dumps(row[index], ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            for row in rows
        )
        for index in range(_COLUMNS)
    ]


@pytest.mark.parametrize("chunk_rows", [1, 7, 50_000])
@pytest.mark.parametrize("max_open_files", [1, 3, 4_096])
def test_column_objects_are_the_same_bytes_whatever_the_chunk_and_file_limit(
    tmp_path: Path, chunk_rows: int, max_open_files: int
) -> None:
    source = _wide_csv(tmp_path / "wide.csv", 120)
    result = normalize_csv(
        source,
        tmp_path / "out",
        config=NormalizationConfig(chunk_rows=chunk_rows, max_open_files=max_open_files),
    )
    expected = _expected_columns(source)
    for index, payload in enumerate(expected):
        written = (tmp_path / "out" / "objects" / f"column_{index + 1:06d}.jsonl").read_bytes()
        assert written == payload, index
        assert result.objects[index].size_bytes == len(payload)


def _rows(count: int, *, fail_after: int | None = None) -> Iterator[list[str]]:
    for row in range(count):
        if fail_after is not None and row == fail_after:
            raise RuntimeError("the reader failed")
        yield [_CELLS[(row + index) % len(_CELLS)] * 40 for index in range(_COLUMNS)]


def test_the_writer_holds_a_bounded_amount_of_column_bytes(tmp_path: Path) -> None:
    """The bytes waiting to be written are bounded by a fixed budget, not by the table: three
    budgets' worth of cells pass through it without its memory growing past one."""
    budget = table_split._WRITE_BUFFER_BYTES
    per_row = sum(len(json.dumps(cell * 40)) + 1 for cell in _CELLS) * _COLUMNS // len(_CELLS)
    rows = 3 * budget // per_row
    tracemalloc.start()
    try:
        table_split.split_table(
            [f"c{index}" for index in range(_COLUMNS)],
            _rows(rows),
            split="columns",
            chunk_rows=50_000,
            max_open_files=64,
            objects_dir=tmp_path,
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak <= budget + 2 * 1_048_576, (peak, budget)


def test_a_failed_read_closes_every_object_file_and_raises_its_own_error(tmp_path: Path) -> None:
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("counting open descriptors needs /proc")
    with pytest.raises(RuntimeError, match="the reader failed"):
        table_split.split_table(
            [f"c{index}" for index in range(_COLUMNS)],
            _rows(500, fail_after=250),
            split="columns",
            chunk_rows=50_000,
            max_open_files=64,
            objects_dir=tmp_path,
        )
    left_open = [
        descriptor
        for descriptor in os.listdir("/proc/self/fd")
        if os.path.realpath(f"/proc/self/fd/{descriptor}").startswith(str(tmp_path))
    ]
    assert left_open == []


def test_each_open_of_a_wide_split_writes_a_share_of_the_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the buffer, opens grow with the bytes written, not with the cells: the columns
    that do not keep their file open are opened once per flush, and a flush happens once per
    buffer's worth of bytes. With a buffer of a few rows, that is far below one per cell."""
    monkeypatch.setattr(table_split, "_WRITE_BUFFER_BYTES", 16_384)
    source = _wide_csv(tmp_path / "wide.csv", 600)
    limit = NormalizationConfig().max_open_files
    opens = _openings(source, tmp_path / "out", NormalizationConfig())
    total = sum(path.stat().st_size for path in (tmp_path / "out" / "objects").iterdir())
    flushes = -(-total // 16_384) + 1
    # Every object is also read back once, to check it against its digest.
    bound = (limit - 1) + (_COLUMNS - (limit - 1)) * flushes + _COLUMNS
    assert len(opens) <= bound, (len(opens), bound)
    assert len(opens) < 600 * _COLUMNS // 10


def _unwritable(objects: Path) -> None:
    """Every column's object file is /dev/full, whose writes fail with ENOSPC."""
    objects.mkdir()
    for index in range(_COLUMNS):
        (objects / f"column_{index + 1:06d}.jsonl").symlink_to("/dev/full")


def _open_objects(objects: Path) -> list[str]:
    held: list[str] = []
    for descriptor in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            continue
        if target == "/dev/full":
            held.append(target)
    return held


@pytest.mark.parametrize("rows", [3, 400])
def test_a_failed_write_raises_its_own_error_and_closes_every_object_file(
    tmp_path: Path, rows: int
) -> None:
    """A full disk is reported as the write that failed, never as a second error from closing
    the files after it, and no object file stays open: 3 rows fail as the files are closed at
    the end, 400 fail in the middle of a flush."""
    if not Path("/dev/full").exists() or not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /dev/full and /proc")
    objects = tmp_path / "objects"
    _unwritable(objects)
    with pytest.raises(OSError) as raised:
        table_split.split_table(
            [f"c{index}" for index in range(_COLUMNS)],
            _rows(rows),
            split="columns",
            chunk_rows=50_000,
            max_open_files=64,
            objects_dir=objects,
        )
    assert raised.value.errno == 28  # ENOSPC
    assert raised.value.__context__ is None, raised.value.__context__
    assert _open_objects(objects) == []
