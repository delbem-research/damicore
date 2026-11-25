"""The PPMd stream residue and how the stage contains it (ADR 0027).

pyppmd 1.3.1's `Ppmd8Encoder` frees its model on deallocation but never the `CPpmd8` that
holds it, so every PPMd stream leaves that structure resident in the process that opened it.
These tests hold the charge to the measurement and the containment to the memory limit.
"""

import gc
import random
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pyppmd
import pytest

from damicore_distance import (
    ConcatPairing,
    DeflateCompressor,
    DistanceConfig,
    InterleavePairing,
    PpmdCompressor,
)
from damicore_distance.compressor import compressed_size
from damicore_distance.config import PPMD_STREAM_RESIDUE_BYTES

pytestmark = pytest.mark.unit


def _left_per_stream(open_stream: Callable[[], object], count: int) -> float:
    """What each of ``count`` streams leaves allocated, traced byte for byte: the codec's own
    state goes through Python's allocator, so `tracemalloc` sees what it never frees."""
    for _ in range(3):  # the reused buffer and the allocator's first arenas
        open_stream()
    tracemalloc.start()
    try:
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(count):
            open_stream()
        gc.collect()
        return (tracemalloc.get_traced_memory()[0] - before) / count
    finally:
        tracemalloc.stop()


def test_the_charge_per_stream_covers_what_an_encoder_leaves() -> None:
    """Measured, not assumed: an encoder opened, fed and dropped leaves its codec state,
    7,392 bytes here, and no more than the charge. If pyppmd stops leaking, this still holds
    and the charge is merely conservative."""
    payload = random.Random(1).randbytes(1_024)

    def open_and_drop() -> None:
        encoder = pyppmd.Ppmd8Encoder(6, 1 << 20, pyppmd.PPMD8_RESTORE_METHOD_RESTART)
        encoder.encode(payload)
        encoder.flush()

    assert _left_per_stream(open_and_drop, 2_000) <= PPMD_STREAM_RESIDUE_BYTES


@pytest.mark.parametrize("chunk_bytes", [1 << 16, 1 << 22], ids=["whole-chunks", "one-view"])
@pytest.mark.parametrize(
    "pairing",
    [ConcatPairing(), InterleavePairing(interleave_block_bytes=1_024)],
    ids=["concat", "interleave"],
)
def test_a_stream_leaves_the_charge_and_not_its_input(
    tmp_path: Path, pairing: ConcatPairing | InterleavePairing, chunk_bytes: int
) -> None:
    """pyppmd 1.3.1's `encode()` never releases the buffer it is handed, so every object it is
    given stays alive: fed fresh bytes, a stream would leave its whole input, 2 MiB here. The
    stage feeds it one buffer it reuses, so a pair of 1 MiB objects leaves no more than the
    charge, however many pieces the pairing cuts them into, whether or not the last piece is
    a view of the buffer."""
    paths: list[Path] = []
    for index in range(2):
        path = tmp_path / f"object_{index}.bin"
        path.write_bytes(random.Random(index).randbytes(1 << 20))
        paths.append(path)
    config = DistanceConfig(
        compressor=PpmdCompressor(ppmd_memory_bytes=1 << 22),
        compression_chunk_bytes=chunk_bytes,
        pairing=pairing,
    )
    left = _left_per_stream(lambda: compressed_size(tuple(paths), config=config), 10)
    assert left <= PPMD_STREAM_RESIDUE_BYTES


def test_only_ppmd_streams_are_charged() -> None:
    zlib = DistanceConfig(compressor=DeflateCompressor(), workers=4, max_working_memory_bytes=1)
    assert zlib.stream_residue(1_000, 499_500) == (0, None, True)
    ppmd = DistanceConfig(workers=4, max_working_memory_bytes=512 * 1_048_576)
    charged, tasks, pooled = ppmd.stream_residue(1_000, 499_500)
    assert charged > 0 and tasks is not None and pooled


def test_a_pooled_worker_is_replaced_before_its_residue_outgrows_the_limit() -> None:
    """The budget per worker is what the limit leaves after the streams every worker holds;
    a worker runs as many shards as that budget holds, and never fewer than one."""
    limit = 512 * 1_048_576
    settings = DistanceConfig(workers=4, max_working_memory_bytes=limit)
    pairs = 499_500
    charged, tasks, pooled = settings.stream_residue(1_000, pairs)
    shard = settings.effective_pairs_per_shard(pairs)
    per_shard = PPMD_STREAM_RESIDUE_BYTES * shard * 2
    budget = (limit - 4 * settings.worker_memory_bytes - 24 * shard) // 4
    assert pooled and tasks is not None and tasks >= 1
    assert charged == tasks * per_shard
    # As many shards as the budget holds, and not one more: the least replacement that
    # keeps every worker inside the limit.
    assert tasks * per_shard <= budget < (tasks + 1) * per_shard


def test_one_worker_stays_in_process_while_its_residue_fits_the_limit() -> None:
    """A single worker runs in the calling process, as it always has, whenever everything
    its streams leave fits the limit; past that it gets one process of its own, replaced as
    its residue grows, rather than outgrowing the limit."""
    small = DistanceConfig(workers=1, max_working_memory_bytes=512 * 1_048_576)
    charged, tasks, pooled = small.stream_residue(100, 4_950)
    assert (tasks, pooled) == (None, False)
    assert charged == PPMD_STREAM_RESIDUE_BYTES * (100 * 4 + 4_950 * 3 + 1)
    large = small.stream_residue(1_000, 499_500)
    assert large[2] and large[1] is not None
    standalone = DistanceConfig(workers=1).stream_residue(1_000, 499_500)
    assert standalone[2] is False  # no limit declared: in process, as before
