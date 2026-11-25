"""The PPMd stream residue and how the stage contains it (ADR 0027).

pyppmd 1.3.1's `Ppmd8Encoder` frees its model on deallocation but never the `CPpmd8` that
holds it, so every PPMd stream leaves that structure resident in the process that opened it.
These tests hold the charge to the measurement and the containment to the memory limit.
"""

import gc
import random
import tracemalloc
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pyppmd
import pytest

from damicore_distance import (
    ConcatPairing,
    DeflateCompressor,
    DistanceConfig,
    InterleavePairing,
    PpmdCompressor,
)
from damicore_distance import compressor as distance_compressor
from damicore_distance.compressor import compressed_fingerprint, compressed_size
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
    charged, budget, pooled = ppmd.stream_residue(1_000, 499_500)
    assert charged > 0 and budget is not None and pooled


def test_a_pooled_worker_may_hold_what_the_limit_leaves_it() -> None:
    """The budget per worker is what the limit leaves after every worker's stream and chunks,
    shared among them; a worker holds at most that, or one shard where a shard is more, and
    that is what each is charged."""
    limit = 512 * 1_048_576
    settings = DistanceConfig(workers=4, max_working_memory_bytes=limit)
    pairs = 499_500
    charged, budget, pooled = settings.stream_residue(1_000, pairs)
    shard = settings.effective_pairs_per_shard(pairs)
    per_shard = PPMD_STREAM_RESIDUE_BYTES * shard * 2
    leaves = (limit - 4 * settings.worker_memory_bytes - 24 * shard) // 4
    assert pooled
    assert budget == charged == max(leaves, per_shard)
    # At the defaults a shard fits what the limit leaves, so every worker stays inside it.
    assert per_shard <= leaves
    assert 4 * (settings.worker_memory_bytes + charged) + 24 * shard <= limit


def test_one_worker_stays_in_process_while_its_residue_fits_the_limit() -> None:
    """A single worker runs in the calling process, as it always has, whenever everything
    its streams leave fits the limit; past that it gets one process of its own, replaced as
    its residue grows, rather than outgrowing the limit."""
    small = DistanceConfig(workers=1, max_working_memory_bytes=512 * 1_048_576)
    charged, budget, pooled = small.stream_residue(100, 4_950)
    assert (budget, pooled) == (None, False)
    assert charged == PPMD_STREAM_RESIDUE_BYTES * (100 * 4 + 4_950 * 3 + 1)
    large = small.stream_residue(1_000, 499_500)
    assert large[2] and large[1] is not None
    standalone = DistanceConfig(workers=1).stream_residue(1_000, 499_500)
    assert standalone[2] is False  # no limit declared: in process, as before


def _coded(paths: tuple[Path, ...], config: DistanceConfig) -> bytes:
    """The bytes the stage codes a stream into, as `compressed_fingerprint` counts them."""
    feed, finish = distance_compressor._stream(config)
    pieces = distance_compressor._paired_chunks(paths, config)
    return b"".join(bytes(feed(piece)) for piece in pieces) + finish()


def _decoded(coded: bytes, model: PpmdCompressor) -> bytes | str | None:
    """The decoder's reading of a stream, or ``None`` where it finds the stream corrupt."""
    # Exported in pyppmd's `__all__` but defined behind its import fallback, which the type
    # checker does not follow; looked up by name and typed here instead.
    decompress = cast("Callable[..., bytes | str]", vars(pyppmd)["decompress"])
    try:
        return decompress(
            coded, max_order=model.ppmd_order, mem_size=model.ppmd_memory_bytes, variant="I"
        )
    except (ValueError, EOFError):
        return None


# Seeds whose 80,000 random bytes, coded in one call as a 4 MiB chunk coded them, lost a byte.
DROPPING_SEEDS = [10, 14, 22, 35]


@pytest.mark.parametrize("seed", DROPPING_SEEDS)
@pytest.mark.parametrize("chunk_bytes", [7, 4_096, 1 << 22])
def test_a_ppmd_stream_decodes_back_to_its_input(
    tmp_path: Path, seed: int, chunk_bytes: int
) -> None:
    """pyppmd 1.3.1 drops output bytes when a symbol's code crosses the end of the first 32 KiB
    output block of an `encode()` call, so a call coding more than that can lose a byte. The
    oracle shares nothing with the encoder: the decoder must give the input back, whatever
    the operational chunk size (ADR 0027)."""
    payload = random.Random(seed).randbytes(80_000)
    path = tmp_path / "object.bin"
    path.write_bytes(payload)
    model = PpmdCompressor(ppmd_memory_bytes=1 << 22)
    config = DistanceConfig(compressor=model, compression_chunk_bytes=chunk_bytes)
    assert _decoded(_coded((path,), config), model) == payload


@pytest.mark.parametrize(
    "pairing",
    [ConcatPairing(), InterleavePairing(interleave_block_bytes=1_024)],
    ids=["concat", "interleave"],
)
def test_a_ppmd_size_does_not_depend_on_the_chunk_size(
    tmp_path: Path, pairing: ConcatPairing | InterleavePairing
) -> None:
    """The chunk size is operational, so no value may move with it (ADR 0027)."""
    paths: list[Path] = []
    for seed in DROPPING_SEEDS[:2]:
        path = tmp_path / f"object_{seed}.bin"
        path.write_bytes(random.Random(seed).randbytes(80_000))
        paths.append(path)

    def size(chunk_bytes: int) -> int:
        config = DistanceConfig(
            compressor=PpmdCompressor(ppmd_memory_bytes=1 << 22),
            compression_chunk_bytes=chunk_bytes,
            pairing=pairing,
        )
        return compressed_size(tuple(paths), config=config)

    assert size(7) == size(4_096) == size(1 << 22)


def test_concurrent_streams_in_one_process_measure_what_they_measure_alone(
    tmp_path: Path,
) -> None:
    """The reused buffer is per thread: two threads compressing at once in one process get
    the values each gets alone (ADR 0027)."""
    paths: list[Path] = []
    for index in range(4):
        path = tmp_path / f"object_{index}.bin"
        path.write_bytes(random.Random(index).randbytes(50_000))
        paths.append(path)
    configs = [
        DistanceConfig(compressor=compressor, compression_chunk_bytes=64)
        for compressor in (PpmdCompressor(ppmd_memory_bytes=1 << 22), DeflateCompressor())
    ]
    jobs = [(path, config) for path in paths for config in configs] * 3
    alone = [compressed_fingerprint((path,), config=config) for path, config in jobs]

    def fingerprint(job: tuple[Path, DistanceConfig]) -> tuple[int, bytes]:
        return compressed_fingerprint((job[0],), config=job[1])

    with ThreadPoolExecutor(max_workers=4) as threads:
        together = list(threads.map(fingerprint, jobs))
    assert together == alone
