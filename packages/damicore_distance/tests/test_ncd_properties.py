"""Hypothesis properties for the compressor and the NCD matrix.

The example-based suite in ``test_distance.py`` pins named scenarios. These properties cover
the two things a fixed example cannot: that the streaming, chunked, file-backed compressor
agrees with a direct in-memory one for arbitrary payloads and chunk boundaries, and that the
matrix built from random small object sets satisfies the NCD invariants and the exact ratio
for every pair.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import itertools
import json
import lzma
import random
import subprocess
import sys
import textwrap
import tracemalloc
import zlib
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pyppmd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import damicore_distance.api as distance_api
from damicore_distance import (
    Bzip2Compressor,
    ConcatPairing,
    DeflateCompressor,
    DistanceConfig,
    DistanceError,
    InterleavePairing,
    LzmaCompressor,
    PpmdCompressor,
    compute_distance_matrix,
)
from damicore_distance.api import _pairs_over_window
from damicore_distance.compressor import _paired_chunks, compressed_size
from damicore_distance.config import PPMD_STREAM_RESIDUE_BYTES
from damicore_distance.ncd import normalized_compression_distance

pytestmark = pytest.mark.unit

# Small enough that a property runs in milliseconds, large enough to cross the chunk sizes
# exercised below and to give the deflate window something to match on.
PAYLOAD = st.binary(max_size=96)
CHUNK_BYTES = st.integers(min_value=1, max_value=48)
CompressorName = Literal["zlib", "gzip", "ppmd", "bzip2", "lzma"]
CompressorSettings = DeflateCompressor | PpmdCompressor | Bzip2Compressor | LzmaCompressor
COMPRESSOR: st.SearchStrategy[CompressorName] = st.sampled_from(
    ["zlib", "gzip", "ppmd", "bzip2", "lzma"]
)
LEVEL = st.integers(min_value=0, max_value=9)
Pairing = ConcatPairing | InterleavePairing
PAIRING: st.SearchStrategy[Pairing] = st.one_of(
    st.just(ConcatPairing()),
    st.builds(InterleavePairing, interleave_block_bytes=st.integers(min_value=1, max_value=64)),
)


def _legacy_interleave(first: bytes, second: bytes, block_bytes: int) -> bytes:
    """``interleave`` from the original damicore-python (``src/ncd.py``), ported line for line
    from files to bytes: the oracle the pairing is held to, sharing no code with it."""
    paired = io.BytesIO()
    left, right = io.BytesIO(first), io.BytesIO(second)
    maxsize = max(len(first), len(second))
    for _ in range(0, maxsize, block_bytes):
        x1 = left.read(block_bytes)
        x2 = right.read(block_bytes)
        paired.write(x1 + x2)
    return paired.getvalue()


def _paired(first: bytes, second: bytes, pairing: Pairing) -> bytes:
    """The bytes a pairing hands the compressor, built by the oracle above or by joining."""
    if isinstance(pairing, InterleavePairing):
        return _legacy_interleave(first, second, pairing.interleave_block_bytes)
    return first + second


def _config(compressor: CompressorName, level: int, chunk_bytes: int) -> DistanceConfig:
    """The configuration a caller would write: a level only for the compressors that have one."""
    settings: CompressorSettings
    match compressor:
        case "zlib" | "gzip":
            settings = DeflateCompressor(name=compressor, compression_level=level)
        case "ppmd":
            settings = PpmdCompressor()
        case "bzip2":
            settings = Bzip2Compressor()
        case "lzma":
            settings = LzmaCompressor()
    return DistanceConfig(compressor=settings, compression_chunk_bytes=chunk_bytes)


def _named(compressor: CompressorName) -> DistanceConfig:
    """A compressor at its defaults, run on one worker."""
    return DistanceConfig.model_validate({"compressor": {"name": compressor}, "workers": 1})


def _direct_size(*payloads: bytes, config: DistanceConfig) -> int:
    """Compress payloads in one pass, in memory, as the reference for ``compressed_size``.

    Deliberately shares nothing with the implementation except the codec itself: no file
    handles and no chunking, so a defect in either would show up as a size disagreement.
    PPMd, bzip2 and LZMA go through each library's one-shot entry point rather than the
    streaming encoder the implementation drives.
    """
    joined = b"".join(payloads)
    settings = config.compressor
    match settings:
        case Bzip2Compressor():
            return len(bz2.compress(joined, settings.bzip2_block_size_100k))
        case LzmaCompressor():
            lzma2 = {
                "id": lzma.FILTER_LZMA2,
                "preset": 6,
                "dict_size": settings.lzma_dictionary_bytes,
            }
            return len(lzma.compress(joined, format=lzma.FORMAT_RAW, filters=[lzma2]))
        case PpmdCompressor():
            # 64 bytes a call: at most 4 bytes per range-coder step and order + 2 steps per
            # byte keep every call's code under the 32 KiB block where pyppmd drops bytes,
            # for any order up to 64 (ADR 0027). pyppmd keeps every slice it is handed, which
            # in a test process costs only the payload.
            encoder = pyppmd.Ppmd8Encoder(
                settings.ppmd_order,
                settings.ppmd_memory_bytes,
                pyppmd.PPMD8_RESTORE_METHOD_RESTART,
            )
            coded = sum(
                len(encoder.encode(joined[start : start + 64]))
                for start in range(0, len(joined), 64)
            )
            return coded + len(encoder.flush())
        case DeflateCompressor():
            stream = zlib.compressobj(
                level=settings.compression_level,
                wbits=zlib.MAX_WBITS if settings.name == "zlib" else 31,
            )
            total = 0
            for payload in payloads:
                total += len(stream.compress(payload))
            return total + len(stream.flush())


def _object_file(directory: Path, name: str, payload: bytes) -> Path:
    path = directory / name
    path.write_bytes(payload)
    return path


def _normalization_manifest(directory: Path, payloads: list[bytes]) -> Path:
    """Write objects and a schema-valid normalization manifest for arbitrary payloads.

    Going through ``normalize_csv`` would constrain the payloads to what a CSV can express;
    the distance stage's contract is the manifest, so this builds that contract directly.
    """
    objects_dir = directory / "objects"
    objects_dir.mkdir(parents=True)
    objects: list[dict[str, object]] = []
    for index, payload in enumerate(payloads, start=1):
        object_id = f"column_{index:06d}"
        _object_file(objects_dir, f"{object_id}.jsonl", payload)
        objects.append(
            {
                "object_id": object_id,
                "label": f"label_{index:06d}",
                "relative_path": f"objects/{object_id}.jsonl",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "object_encoding": "json-lines/1",
                "input": {
                    "kind": "delimited",
                    "path": str(directory / "synthetic.csv"),
                    "sha256": hashlib.sha256(b"".join(payloads)).hexdigest(),
                    "size_bytes": sum(len(payload) for payload in payloads),
                    "delimiter": ",",
                    "encoding": "utf-8",
                    "split": "columns",
                },
                "objects": objects,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


@given(payload=PAYLOAD, chunk_bytes=CHUNK_BYTES, compressor=COMPRESSOR, level=LEVEL)
@settings(max_examples=150, deadline=None)
def test_chunked_compression_matches_a_direct_single_shot_stream(
    tmp_path_factory: pytest.TempPathFactory,
    payload: bytes,
    chunk_bytes: int,
    compressor: CompressorName,
    level: int,
) -> None:
    directory = tmp_path_factory.mktemp("chunked")
    path = _object_file(directory, "object.jsonl", payload)
    config = _config(compressor, level, chunk_bytes)
    assert compressed_size((path,), config=config) == _direct_size(payload, config=config)


@given(payload=PAYLOAD, chunk_bytes=CHUNK_BYTES, level=LEVEL)
@settings(max_examples=100, deadline=None)
def test_zlib_sizes_match_the_one_shot_module_function(
    tmp_path_factory: pytest.TempPathFactory,
    payload: bytes,
    chunk_bytes: int,
    level: int,
) -> None:
    """A second reference that shares no code path with ``compressobj``: for the zlib
    compressor, ``zlib.compress`` must produce a stream of exactly the same length."""
    directory = tmp_path_factory.mktemp("one-shot")
    path = _object_file(directory, "object.jsonl", payload)
    config = _config("zlib", level, chunk_bytes)
    assert compressed_size((path,), config=config) == len(zlib.compress(payload, level))


@given(left=PAYLOAD, right=PAYLOAD, chunk_bytes=CHUNK_BYTES, compressor=COMPRESSOR)
@settings(max_examples=150, deadline=None)
def test_pair_compression_equals_compressing_the_joined_bytes(
    tmp_path_factory: pytest.TempPathFactory,
    left: bytes,
    right: bytes,
    chunk_bytes: int,
    compressor: CompressorName,
) -> None:
    """C(xy) feeds x then y through one compressor instance and must equal compressing the
    concatenation, which the implementation never materializes."""
    directory = tmp_path_factory.mktemp("pair")
    left_path = _object_file(directory, "left.jsonl", left)
    right_path = _object_file(directory, "right.jsonl", right)
    config = _config(compressor, 6, chunk_bytes)
    assert compressed_size((left_path, right_path), config=config) == _direct_size(
        left + right, config=config
    )


@given(
    left=PAYLOAD,
    right=PAYLOAD,
    chunk_bytes=CHUNK_BYTES,
    compressor=COMPRESSOR,
    block_bytes=st.integers(min_value=1, max_value=128),
)
@settings(max_examples=200, deadline=None)
def test_interleaved_pair_compression_equals_compressing_the_legacy_interleave(
    tmp_path_factory: pytest.TempPathFactory,
    left: bytes,
    right: bytes,
    chunk_bytes: int,
    compressor: CompressorName,
    block_bytes: int,
) -> None:
    """The interleaved pair is the original implementation's, byte for byte, streamed rather
    than written to a temporary file: blocks of ``block_bytes`` taken in turn, the rest of the
    longer object after the shorter one ends (ADR 0026). Empty objects, a block longer than
    both, and a block of one byte are all in the drawn range."""
    directory = tmp_path_factory.mktemp("interleaved")
    left_path = _object_file(directory, "left.jsonl", left)
    right_path = _object_file(directory, "right.jsonl", right)
    config = _config(compressor, 6, chunk_bytes).model_copy(
        update={"pairing": InterleavePairing(interleave_block_bytes=block_bytes)}
    )
    legacy = _legacy_interleave(left, right, block_bytes)
    # The bytes fed to the compressor, then what it makes of them.
    # Each piece is the stage's reused buffer, valid until the next: copied as it comes.
    assert b"".join(bytes(piece) for piece in _paired_chunks((left_path, right_path), config)) == (
        legacy
    )
    assert compressed_size((left_path, right_path), config=config) == _direct_size(
        legacy, config=config
    )


@given(left=PAYLOAD, right=PAYLOAD, compressor=COMPRESSOR)
@settings(max_examples=60, deadline=None)
def test_concatenation_is_interleaving_with_a_block_that_holds_either_object(
    tmp_path_factory: pytest.TempPathFactory,
    left: bytes,
    right: bytes,
    compressor: CompressorName,
) -> None:
    """One block of each object is the whole of each: the pairings meet there, which is why
    the window gate reads one block of each object in place of the whole object (ADR 0026)."""
    directory = tmp_path_factory.mktemp("meet")
    paths = (
        _object_file(directory, "left.jsonl", left),
        _object_file(directory, "right.jsonl", right),
    )
    concatenated = _named(compressor)
    holding = concatenated.model_copy(
        update={"pairing": InterleavePairing(interleave_block_bytes=max(len(left), len(right), 1))}
    )
    assert compressed_size(paths, config=holding) == compressed_size(paths, config=concatenated)


def test_interleaving_holds_no_block_whatever_its_size(tmp_path: Path) -> None:
    """A block is read into the compressor's buffer a chunk at a time and never held whole, so
    an interleaving worker is charged what a concatenating one is: the memory traced while a
    pair of 4 MiB objects is interleaved in 4 MiB blocks is what concatenating them traces,
    nowhere near one block (ADR 0026)."""
    block = 4 * 1_048_576
    paths = (
        _object_file(tmp_path, "left.jsonl", random.Random(1).randbytes(block)),
        _object_file(tmp_path, "right.jsonl", random.Random(2).randbytes(block)),
    )

    def peak(pairing: Pairing, chunk: int) -> int:
        # A chunk size of its own, so its buffer is allocated, and traced, in this call.
        config = DistanceConfig(
            compressor=DeflateCompressor(), compression_chunk_bytes=chunk, pairing=pairing
        )
        tracemalloc.start()
        try:
            compressed_size(paths, config=config)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    concatenated = peak(ConcatPairing(), 65_537)
    interleaved = peak(InterleavePairing(interleave_block_bytes=block), 65_539)
    assert interleaved <= concatenated + 4_096, (interleaved, concatenated)
    assert interleaved < block // 4, interleaved


def test_the_default_compressor_reproduces_a_known_size(tmp_path: Path) -> None:
    """A size computed once with pyppmd 1.3.1, so a codec build that encodes differently --
    another platform, another patch release -- fails here instead of shifting every NCD."""
    payload = b"".join(b"damicore %d\n" % index for index in range(5_000))
    path = _object_file(tmp_path, "object.jsonl", payload)
    assert len(payload) == 68_890
    assert compressed_size((path,), config=DistanceConfig()) == 5_801


def _column(vocabulary_seed: int, draw_seed: int, size: int) -> bytes:
    """A column as the normalizer writes it: one JSON value per line, drawn independently
    from a vocabulary, so two columns share a distribution and never a run of content."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    shape = random.Random(vocabulary_seed)
    vocabulary = [
        "".join(shape.choice(letters) for _ in range(shape.randint(4, 9))) for _ in range(200)
    ]
    draws = random.Random(draw_seed)
    lines: list[str] = []
    written = 0
    while written < size:
        lines.append(json.dumps(draws.choice(vocabulary)) + "\n")
        written += len(lines[-1])
    return "".join(lines).encode()[:size]


def _mutated(payload: bytes, rate: float) -> bytes:
    draws = random.Random(3)
    mutated = bytearray(payload)
    for index in draws.sample(range(len(mutated)), int(len(mutated) * rate)):
        mutated[index] = draws.randint(97, 122)
    return bytes(mutated)


def _copy_separation(directory: Path, compressor: CompressorName, size: int) -> float:
    """Distance to an unrelated column minus distance to the same column with 1 % changed."""
    column = _column(1, 10, size)
    payloads = [column, _mutated(column, 0.01), _column(2, 12, size)]
    manifest_path = _normalization_manifest(directory / f"{compressor}-{size}", payloads)
    result = compute_distance_matrix(
        manifest_path,
        directory / f"{compressor}-{size}-run",
        config=_named(compressor),
    )
    matrix = np.load(result.matrix_path, allow_pickle=False)
    return float(matrix[0, 2]) - float(matrix[0, 1])


def test_lzma_finds_a_copy_at_any_size_its_dictionary_holds(tmp_path: Path) -> None:
    """Why LZMA is offered (ADR 0021): a column and its 1 % mutated copy at 128 KiB are past
    DEFLATE's window, and its default 8 MiB dictionary holds the pair. Measured: 0.906 under
    LZMA, 0.019 under zlib."""
    lzma_separation = _copy_separation(tmp_path, "lzma", 131_072)
    assert lzma_separation > 0.8
    assert lzma_separation > 20 * _copy_separation(tmp_path, "zlib", 131_072)


def test_bzip2_finds_a_copy_within_its_block_and_not_past_it(tmp_path: Path) -> None:
    """The 900 kB block is bzip2's window: a pair of 128 KiB columns fits it, a pair of
    1 MiB columns spans blocks that share nothing. Measured: 0.590 and 0.070."""
    assert _copy_separation(tmp_path, "bzip2", 131_072) > 0.3
    assert _copy_separation(tmp_path, "bzip2", 1_048_576) < 0.1


def test_past_the_deflate_window_ppmd_still_separates_columns_by_distribution(
    tmp_path: Path,
) -> None:
    """The defect the default exists to fix. Two columns drawn from one vocabulary and a
    third from another, 128 KiB each: DEFLATE sees 32 KiB, so the pair it compresses holds
    the first column out of view of the second and every distance nears 1. PPMd keeps the
    statistics of what it has read, which is what such columns share. Measured: separation
    0.028 under zlib and 0.238 under PPMd; the bounds below sit well inside both."""
    size = 131_072
    payloads = [_column(1, 10, size), _column(1, 11, size), _column(2, 12, size)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)

    def separation(compressor: Literal["zlib", "ppmd"]) -> float:
        result = compute_distance_matrix(
            manifest_path,
            tmp_path / compressor,
            config=_named(compressor),
        )
        matrix = np.load(result.matrix_path, allow_pickle=False)
        return float(matrix[0, 2]) - float(matrix[0, 1])

    assert separation("ppmd") > 0.15
    assert separation("ppmd") > 4 * separation("zlib")


@given(
    cx=st.integers(min_value=1, max_value=10_000),
    cy=st.integers(min_value=1, max_value=10_000),
    cxy=st.integers(min_value=0, max_value=40_000),
)
def test_ncd_is_the_exact_ratio_and_is_never_clamped(cx: int, cy: int, cxy: int) -> None:
    """The result is not truncated, rounded or bounded to 0..1."""
    value = normalized_compression_distance(cx, cy, cxy)
    assert value == (cxy - min(cx, cy)) / max(cx, cy)
    assert normalized_compression_distance(cy, cx, cxy) == value


@given(payloads=st.lists(PAYLOAD, min_size=2, max_size=4), pairing=PAIRING)
@settings(
    max_examples=40,
    deadline=None,
    # The temporary directory comes from the session-scoped factory, so each example gets a
    # fresh one; only the unused function-scoped fixtures would trip this check.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_random_small_matrices_hold_the_invariants_and_the_exact_pair_values(
    tmp_path_factory: pytest.TempPathFactory, payloads: list[bytes], pairing: Pairing
) -> None:
    directory = tmp_path_factory.mktemp("matrix")
    manifest_path = _normalization_manifest(directory / "normalized", payloads)
    result = compute_distance_matrix(
        manifest_path,
        directory / "run",
        config=DistanceConfig(workers=1, pairs_per_shard=1, pairing=pairing),
    )
    matrix = np.load(result.matrix_path, allow_pickle=False)

    count = len(payloads)
    assert matrix.shape == (count, count)
    assert matrix.dtype == np.float64
    assert np.isfinite(matrix).all()
    assert np.array_equal(matrix, matrix.T)  # pyright: ignore[reportUnknownMemberType]
    assert [float(matrix[index, index]) for index in range(count)] == [0.0] * count

    config = DistanceConfig()
    sizes = [_direct_size(payload, config=config) for payload in payloads]
    for left in range(count):
        for right in range(left + 1, count):
            # The pair is compressed in the order of its contents' digests [adr-0024].
            first, second = sorted(
                (payloads[left], payloads[right]),
                key=lambda payload: hashlib.sha256(payload).digest(),
            )
            joined = _direct_size(_paired(first, second, pairing), config=config)
            expected = normalized_compression_distance(sizes[left], sizes[right], joined)
            assert float(matrix[left, right]) == expected

    # The longest object, ties to the higher digest, compressed after itself: C(xx) against C(x).
    largest = max(
        range(count),
        key=lambda index: (len(payloads[index]), hashlib.sha256(payloads[index]).digest()),
    )
    doubled = _direct_size(_paired(payloads[largest], payloads[largest], pairing), config=config)
    assert result.ncd_self_largest == normalized_compression_distance(
        sizes[largest], sizes[largest], doubled
    )


@given(
    payloads=st.lists(PAYLOAD, min_size=2, max_size=5),
    compressor=COMPRESSOR,
    pairing=PAIRING,
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_reordering_the_objects_reorders_the_matrix_and_changes_no_value(
    tmp_path_factory: pytest.TempPathFactory,
    payloads: list[bytes],
    compressor: CompressorName,
    pairing: Pairing,
    seed: int,
) -> None:
    """The matrix is a function of the objects, not of where they sit in the input, under
    either pairing.

    No compressor here is symmetric, so a pair compressed in input order measures a value
    that moves when the objects are reordered -- and that was enough to change the partition
    of the same corpus (issue 131). Metamorphic: the same payloads in a shuffled order must
    give the same matrix with its rows and columns shuffled alike, bit for bit, and the same
    ``ncd_self_largest``, whose object is chosen by content when sizes tie.
    """
    order = list(range(len(payloads)))
    random.Random(seed).shuffle(order)
    directory = tmp_path_factory.mktemp("order")

    def measured(
        name: str, objects: list[bytes]
    ) -> tuple[np.ndarray[tuple[int, int], np.dtype[np.float64]], float]:
        manifest_path = _normalization_manifest(directory / name / "normalized", objects)
        config = _named(compressor).model_copy(update={"pairing": pairing})
        result = compute_distance_matrix(manifest_path, directory / name / "run", config=config)
        return np.load(result.matrix_path, allow_pickle=False), result.ncd_self_largest

    reference, reference_self = measured("reference", payloads)
    shuffled, shuffled_self = measured("shuffled", [payloads[index] for index in order])
    assert np.array_equal(shuffled, reference[np.ix_(order, order)])  # pyright: ignore[reportUnknownMemberType]
    assert shuffled_self == reference_self


def test_the_largest_object_is_chosen_by_content_when_sizes_tie(tmp_path: Path) -> None:
    """`ncd_self_largest` compresses one object after itself. When two objects share the
    largest size the choice used to fall to input order, so the reported value moved with it;
    a repetitive object and a random one of equal length read far apart."""
    repetitive = b"damicore " * 400
    noise = random.Random(7).randbytes(len(repetitive))

    def self_largest(name: str, payloads: list[bytes]) -> float:
        manifest_path = _normalization_manifest(tmp_path / name / "normalized", payloads)
        result = compute_distance_matrix(
            manifest_path, tmp_path / name / "run", config=_named("zlib")
        )
        return result.ncd_self_largest

    assert self_largest("forward", [repetitive, noise]) == self_largest(
        "reverse", [noise, repetitive]
    )


# --- PPMd's model capacity, issue 132 ---------------------------------------------------

# A model small enough that the cases below show at tens of KiB, the half the stage checks it
# against, and one sixteen times larger that none of them fills: the oracle for what the small
# one would measure if it never restarted.
SMALL_MODEL = PpmdCompressor(ppmd_memory_bytes=1_048_576)
HALF_MODEL = PpmdCompressor(ppmd_memory_bytes=524_288)
# A working-memory budget that holds a stream of twice the small model, so a stream half the
# model codes differently is measured again under twice it rather than refused (ADR 0025).
ROOMY = 1 << 30
UNFILLED_MODEL = PpmdCompressor(ppmd_memory_bytes=16 * 1_048_576)


def _restarting_object() -> bytes:
    """Random bytes that fill the small model on their own."""
    return random.Random(5).randbytes(262_144)


def _near_duplicate(payload: bytes, seed: int) -> bytes:
    """``payload`` with one byte in a thousand replaced."""
    draws = random.Random(seed)
    mutated = bytearray(payload)
    for index in draws.sample(range(len(mutated)), max(1, len(mutated) // 1_000)):
        mutated[index] = draws.randrange(256)
    return bytes(mutated)


def _matrix(
    manifest_path: Path,
    output: Path,
    compressor: PpmdCompressor,
    *,
    allow_model_restart: bool,
    max_working_memory_bytes: int = ROOMY,
) -> np.ndarray[tuple[int, int], np.dtype[np.float64]]:
    result = compute_distance_matrix(
        manifest_path,
        output,
        config=DistanceConfig(
            compressor=compressor,
            workers=1,
            allow_model_restart=allow_model_restart,
            max_working_memory_bytes=max_working_memory_bytes,
        ),
    )
    return np.load(result.matrix_path, allow_pickle=False)


def test_an_object_that_restarts_the_ppmd_model_alone_is_refused_before_any_pair(
    tmp_path: Path,
) -> None:
    """PPMd restarts its model from nothing when its memory fills, so an object that fills it
    alone has a compressed size, and a distance to everything, measured with part of itself
    forgotten (ADR 0025). That needs no pair to see, so the stage refuses before any pair."""
    payloads = [_restarting_object(), _column(1, 10, 262_144)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    with pytest.raises(DistanceError) as raised:
        compute_distance_matrix(
            manifest_path,
            tmp_path / "run",
            config=DistanceConfig(compressor=SMALL_MODEL, workers=1),
        )
    assert raised.value.code == "model_capacity_error"
    assert "ppmd_memory_bytes" in str(raised.value)
    assert not (tmp_path / "run" / "distance.npy").exists()


def test_a_pair_of_near_duplicates_that_restarts_the_model_is_refused(tmp_path: Path) -> None:
    """The case no per-object check can see (ADR 0025): the memory a pair needs grows with what
    the two objects share, so a near-duplicate pair fills a model each object alone fits in.
    Measured anyway, it reads as far apart; the stage refuses it instead."""
    original = random.Random(16).randbytes(16_000)
    payloads = [original, _near_duplicate(original, 16)]

    def size(compressor: PpmdCompressor, *parts: bytes) -> int:
        return _direct_size(*parts, config=DistanceConfig(compressor=compressor))

    # The scenario, established by the codec alone: each object fits half the model, so a
    # per-object check admits it, and the pair does not fit the whole model, so measuring it
    # would be wrong.
    for payload in payloads:
        assert size(HALF_MODEL, payload) == size(UNFILLED_MODEL, payload)
    assert size(SMALL_MODEL, *payloads) != size(UNFILLED_MODEL, *payloads)

    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    with pytest.raises(DistanceError) as raised:
        compute_distance_matrix(
            manifest_path,
            tmp_path / "run",
            # Room to measure it again under twice the model, which restarts too: refused
            # because the model restarted, not for want of memory.
            config=DistanceConfig(
                compressor=SMALL_MODEL, workers=1, max_working_memory_bytes=ROOMY
            ),
        )
    assert raised.value.code == "model_capacity_error"
    assert "ppmd_memory_bytes" in str(raised.value)
    assert "label_000001" in str(raised.value) and "label_000002" in str(raised.value)


def test_a_restart_that_codes_as_many_bytes_is_still_refused(tmp_path: Path) -> None:
    """Sizes alone are not the check: here the model and its half both restart and code the
    same number of bytes, 1,088, where a model that does not fill codes 1,077. Comparing sizes
    admitted this run with a wrong distance; the bytes the two models code differ, and the
    stage compares those (ADR 0025)."""
    noise = random.Random(2).randbytes(1_001)
    tiny = PpmdCompressor(ppmd_memory_bytes=8_192)

    def size(memory: int) -> int:
        return _direct_size(
            noise, config=DistanceConfig(compressor=PpmdCompressor(ppmd_memory_bytes=memory))
        )

    # The scenario, established by the codec alone.
    assert size(4_096) == size(8_192) != size(1 << 20)

    manifest_path = _normalization_manifest(tmp_path / "normalized", [noise, b"hello"])
    with pytest.raises(DistanceError) as raised:
        compute_distance_matrix(
            manifest_path, tmp_path / "run", config=DistanceConfig(compressor=tiny, workers=1)
        )
    assert raised.value.code == "model_capacity_error"


def test_shards_measured_with_the_check_lifted_are_checked_when_it_applies_again(
    tmp_path: Path,
) -> None:
    """A run measured with the check lifted and resumed without it must not inherit the
    unchecked values: the check applies to the whole matrix the resumed run returns, not to
    the shards it happens to compute itself (ADR 0025)."""
    original = random.Random(16).randbytes(16_000)
    payloads = [original, _near_duplicate(original, 16), _column(1, 10, 4_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    output = tmp_path / "run"

    def measure(*, allow_model_restart: bool) -> None:
        compute_distance_matrix(
            manifest_path,
            output,
            config=DistanceConfig(
                compressor=SMALL_MODEL,
                workers=1,
                pairs_per_shard=1,
                allow_model_restart=allow_model_restart,
            ),
        )

    measure(allow_model_restart=True)
    with pytest.raises(DistanceError) as raised:
        measure(allow_model_restart=False)
    assert raised.value.code == "model_capacity_error"


def _size(compressor: PpmdCompressor, *parts: bytes) -> int:
    return _direct_size(*parts, config=DistanceConfig(compressor=compressor))


def test_a_pair_that_fills_half_the_model_but_not_all_of_it_is_measured(tmp_path: Path) -> None:
    """Half the model codes this pair differently, so it might have restarted the model; under
    twice the model it codes the same bytes as under the model, so it did not, and the value
    is the objects' own. Where memory allows that second look, the run is measured, not
    refused (ADR 0025)."""
    original = random.Random(12_000).randbytes(12_000)
    payloads = [original, _near_duplicate(original, 12_000)]
    # The scenario, established by the codec alone: each object fits half the model, the
    # pair does not, and the whole model holds it.
    for payload in payloads:
        assert _size(HALF_MODEL, payload) == _size(UNFILLED_MODEL, payload)
    assert _size(HALF_MODEL, *sorted(payloads, key=lambda p: hashlib.sha256(p).digest())) != (
        _size(UNFILLED_MODEL, *sorted(payloads, key=lambda p: hashlib.sha256(p).digest()))
    )

    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    measured = _matrix(manifest_path, tmp_path / "checked", SMALL_MODEL, allow_model_restart=False)
    expected = _matrix(
        manifest_path, tmp_path / "unfilled", UNFILLED_MODEL, allow_model_restart=True
    )
    assert measured.tolist() == expected.tolist()


def test_an_object_that_fills_half_the_model_but_not_all_of_it_is_measured(
    tmp_path: Path,
) -> None:
    """The same second look for an object, before any pair (ADR 0025)."""
    noise = random.Random(34_000).randbytes(34_000)
    assert _size(HALF_MODEL, noise) != _size(UNFILLED_MODEL, noise)
    assert _size(SMALL_MODEL, noise) == _size(UNFILLED_MODEL, noise)
    payloads = [noise, _column(1, 10, 2_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    measured = _matrix(manifest_path, tmp_path / "checked", SMALL_MODEL, allow_model_restart=False)
    expected = _matrix(
        manifest_path, tmp_path / "unfilled", UNFILLED_MODEL, allow_model_restart=True
    )
    assert measured.tolist() == expected.tolist()


def test_the_second_look_runs_on_the_pool_and_agrees_with_one_worker(tmp_path: Path) -> None:
    """On a pool the second look is submitted to the workers, as many at once as the budget
    holds; the matrix is the one a single worker measures, bit for bit (ADR 0025)."""
    original = random.Random(12_000).randbytes(12_000)
    payloads = [original, _near_duplicate(original, 12_000), random.Random(7).randbytes(34_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)

    def measured(name: str, workers: int) -> list[list[float]]:
        result = compute_distance_matrix(
            manifest_path,
            tmp_path / name,
            config=DistanceConfig(
                compressor=SMALL_MODEL, workers=workers, max_working_memory_bytes=ROOMY
            ),
        )
        return np.load(result.matrix_path, allow_pickle=False).tolist()

    assert measured("pool", 2) == measured("serial", 1)


def test_the_second_look_and_the_residue_workers_hold_fit_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second look shares ``max_working_memory_bytes`` with the PPMd stream residue the
    workers still hold, which preflight charged against the same limit: the streams it runs at
    once under twice the model, with that residue, must fit the limit (ADR 0025, ADR 0027).
    The residue is counted from the encoders each worker actually opened, not from what the
    stage declares."""
    original = random.Random(12_000).randbytes(12_000)
    payloads = [original, _near_duplicate(original, 12_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    held: dict[int, int] = {}
    running = [-1]

    class InlineExecutor:
        def __init__(self, **_options: Any) -> None:
            self.slot = len(held)
            held[self.slot] = 0

        def submit(self, function: Any, argument: Any) -> Future[Any]:
            running[0] = self.slot
            future: Future[Any] = Future()
            future.set_result(function(argument))
            return future

        def shutdown(self, **_options: Any) -> None:
            held.pop(self.slot, None)

    encoder = pyppmd.Ppmd8Encoder

    def counted(*arguments: Any) -> Any:
        if running[0] in held:
            held[running[0]] += PPMD_STREAM_RESIDUE_BYTES
        return encoder(*arguments)

    looks: list[tuple[int, int]] = []
    bounded = distance_api._bounded_submit

    def recorded(executor: Any, function: Any, arguments: Any, limit: int, streams: Any) -> Any:
        if function is distance_api._escalation_worker:
            first = next(arguments, None)
            if first is None:
                return iter(())
            looks.append((limit, sum(held.values())))
            arguments = itertools.chain([first], arguments)
        return bounded(executor, function, arguments, limit, streams)

    monkeypatch.setattr(distance_api, "ProcessPoolExecutor", InlineExecutor)
    monkeypatch.setattr(pyppmd, "Ppmd8Encoder", counted)
    monkeypatch.setattr(distance_api, "_bounded_submit", recorded)
    probe = DistanceConfig(compressor=SMALL_MODEL, workers=2)
    doubled = PpmdCompressor(ppmd_memory_bytes=2 * SMALL_MODEL.ppmd_memory_bytes)
    look_bytes = probe.model_copy(update={"compressor": doubled}).worker_memory_bytes
    # Room for two streams under twice the model and less than the residue already held.
    limit = 2 * look_bytes + PPMD_STREAM_RESIDUE_BYTES
    result = compute_distance_matrix(
        manifest_path,
        tmp_path / "run",
        config=probe.model_copy(update={"max_working_memory_bytes": limit}),
    )
    assert looks, "the second look never ran"
    for slots, residue in looks:
        assert slots * look_bytes + residue <= limit, (slots, residue, limit)
    expected = _matrix(
        manifest_path, tmp_path / "unfilled", UNFILLED_MODEL, allow_model_restart=True
    )
    assert np.load(result.matrix_path, allow_pickle=False).tolist() == expected.tolist()


def test_a_single_worker_gets_the_second_look_under_the_default_limit(tmp_path: Path) -> None:
    """A caller who declares no limit gets the default one, the limit `damicore.run()` passes,
    and so the second look a declared limit gives: never a refusal that a declared limit would
    have turned into a measurement (ADR 0025)."""
    original = random.Random(12_000).randbytes(12_000)
    payloads = [original, _near_duplicate(original, 12_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    result = compute_distance_matrix(
        manifest_path, tmp_path / "run", config=DistanceConfig(compressor=SMALL_MODEL, workers=1)
    )
    expected = _matrix(
        manifest_path, tmp_path / "unfilled", UNFILLED_MODEL, allow_model_restart=True
    )
    assert np.load(result.matrix_path, allow_pickle=False).tolist() == expected.tolist()


def test_shards_waiting_for_the_second_look_are_not_resumed_unlooked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shard holding a pair half the model coded otherwise is written but not completed
    until the second look passes, so a run interrupted before it computes that shard again
    on resume instead of inheriting it (ADR 0025)."""
    original = random.Random(12_000).randbytes(12_000)
    payloads = [original, _near_duplicate(original, 12_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    config = DistanceConfig(
        compressor=SMALL_MODEL, workers=1, pairs_per_shard=1, max_working_memory_bytes=ROOMY
    )

    def interrupted(*_arguments: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(distance_api, "_second_look", interrupted)
    with pytest.raises(KeyboardInterrupt):
        compute_distance_matrix(manifest_path, tmp_path / "run", config=config)
    checkpoint = json.loads(
        (tmp_path / "run" / "checkpoints" / "distance-shards.json").read_text(encoding="utf-8")
        if (tmp_path / "run" / "checkpoints" / "distance-shards.json").exists()
        else '{"completed": []}'
    )
    assert checkpoint["completed"] == []
    monkeypatch.undo()
    resumed = compute_distance_matrix(manifest_path, tmp_path / "run", config=config)
    expected = _matrix(
        manifest_path, tmp_path / "unfilled", UNFILLED_MODEL, allow_model_restart=True
    )
    assert np.load(resumed.matrix_path, allow_pickle=False).tolist() == expected.tolist()


def test_pairs_waiting_for_the_second_look_cost_the_coordinator_no_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a 4 KiB model every pair of 80-byte random objects needs more than half the model
    and fits the whole of it, so every one of 1,770 pairs waits for the second look. What the
    coordinator itself holds when the look starts must not grow with them: the pairs wait on
    disk (ADR 0025). Measured as the memory allocated from the stage's own module and still
    held, which the codec's own allocations do not enter."""
    payloads = [random.Random(index).randbytes(80) for index in range(60)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    held: list[int] = []
    original = distance_api._second_look

    def measured(*arguments: Any) -> Any:
        snapshot = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(True, distance_api.__file__)]
        )
        held.append(sum(statistic.size for statistic in snapshot.statistics("filename")))
        return original(*arguments)

    monkeypatch.setattr(distance_api, "_second_look", measured)
    tracemalloc.start(1)
    try:
        result = compute_distance_matrix(
            manifest_path,
            tmp_path / "run",
            config=DistanceConfig(
                compressor=PpmdCompressor(ppmd_memory_bytes=4_096),
                workers=1,
                max_working_memory_bytes=ROOMY,
            ),
        )
    finally:
        tracemalloc.stop()
    assert result.matrix_path.exists()
    # Holding the waiting pairs and their digests in lists held 481 KiB here.
    assert held, "the second look never ran"
    assert max(held) < 64 * 1024, held


def test_without_memory_for_a_second_look_the_half_model_refuses(tmp_path: Path) -> None:
    """Twice the model is measured only within the working-memory budget: without it, a
    stream half the model codes differently is refused, as before the second look existed,
    never measured on trust (ADR 0025)."""
    original = random.Random(12_000).randbytes(12_000)
    payloads = [original, _near_duplicate(original, 12_000)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    with pytest.raises(DistanceError) as raised:
        _matrix(
            manifest_path,
            tmp_path / "tight",
            SMALL_MODEL,
            allow_model_restart=False,
            max_working_memory_bytes=4 * 1_048_576,
        )
    assert raised.value.code == "model_capacity_error"
    assert "max_working_memory_bytes" in str(raised.value)


@pytest.mark.parametrize(
    ("memory", "advice", "not_advised"),
    [
        (1_048_576, "max_working_memory_bytes", None),
        # Twice this model is past what pyppmd takes: no limit can make room for the look, so
        # the refusal must not send the caller to raise one.
        (2_147_483_630, "allow_model_restart", "max_working_memory_bytes"),
    ],
)
def test_a_refusal_without_a_second_look_advises_what_can_admit_it(
    memory: int, advice: str, not_advised: str | None
) -> None:
    settings = DistanceConfig(
        compressor=PpmdCompressor(ppmd_memory_bytes=memory), max_working_memory_bytes=1
    )
    message = str(distance_api._model_capacity_error("The object 'x'", settings, cause="no_look"))
    assert message.startswith("The object 'x' fills more than half the PPMd model")
    assert advice in message
    if not_advised is not None:
        assert not_advised not in message


def test_a_caller_who_allows_the_restart_gets_the_measurement(tmp_path: Path) -> None:
    payloads = [_restarting_object(), _column(1, 10, 262_144)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    result = compute_distance_matrix(
        manifest_path,
        tmp_path / "run",
        config=DistanceConfig(compressor=SMALL_MODEL, workers=1, allow_model_restart=True),
    )
    assert result.matrix_path.exists()


def _kind(kind: str, seed: int, size: int) -> bytes:
    draws = random.Random(f"{kind}-{seed}")
    match kind:
        case "vocabulary":
            return _column(seed, seed + 9, size)
        case "numeric":
            return "".join(
                f"{draws.gauss(seed * 7, 10):.6f}\n" for _ in range(size // 8 + 1)
            ).encode()[:size]
        case "integers":
            return "".join(f"{draws.randrange(10**9)}\n" for _ in range(size // 4 + 1)).encode()[
                :size
            ]
        case "text":
            return b" ".join(
                bytes(draws.randrange(97, 123) for _ in range(draws.randrange(1, 9)))
                for _ in range(size // 3 + 1)
            )[:size]
        case "four letters":
            return bytes(draws.randrange(4) + 65 for _ in range(size))
        case "doubles":
            return b"".join(draws.gauss(0, 1).hex().encode() for _ in range(size // 8 + 1))[:size]
        case "mixed":
            return draws.randbytes(size // 2) + _kind("numeric", seed, size - size // 2)
        case _:
            return draws.randbytes(size)


KINDS = ["vocabulary", "numeric", "integers", "text", "four letters", "doubles", "mixed", "random"]


@pytest.mark.parametrize("kind", KINDS)
def test_an_admitted_ppmd_run_measures_what_an_unfilled_model_measures(
    tmp_path: Path, kind: str
) -> None:
    """The guarantee, tested where it can fail: around the sizes at which the small model
    starts to fill, for every kind of content, with a near-duplicate and an unrelated object.

    A run the stage admits must return, bit for bit, the matrix a model none of the objects
    fills returns; a margin per object admitted near-duplicates that broke this (ADR 0025). With
    memory for a second look under twice the model, a run it refuses must hold a stream -- an
    object, or a pair in digest order -- that the model itself codes into other bytes than a
    model none of them fills: it refuses only where the measurement would be wrong. Bytes,
    because two models that both restart can code the same number of them.
    """

    def coded(payload: bytes, compressor: PpmdCompressor) -> bytes:
        encoder = pyppmd.Ppmd8Encoder(
            compressor.ppmd_order,
            compressor.ppmd_memory_bytes,
            pyppmd.PPMD8_RESTORE_METHOD_RESTART,
        )
        return encoder.encode(payload) + encoder.flush()

    def streams(payloads: list[bytes]) -> list[bytes]:
        ordered = sorted(payloads, key=lambda payload: hashlib.sha256(payload).digest())
        return payloads + [x + y for x, y in itertools.combinations(ordered, 2)]

    outcomes: list[bool] = []
    for size in (4_000, 12_000, 24_000, 40_000):
        original = _kind(kind, 1, size)
        payloads = [original, _near_duplicate(original, size), _kind(kind, 2, size)]
        case = tmp_path / str(size)
        manifest_path = _normalization_manifest(case / "normalized", payloads)
        expected = _matrix(
            manifest_path, case / "unfilled", UNFILLED_MODEL, allow_model_restart=True
        )
        try:
            measured = _matrix(
                manifest_path, case / "checked", SMALL_MODEL, allow_model_restart=False
            )
        except DistanceError as refused:
            assert refused.code == "model_capacity_error"
            assert any(
                coded(stream, SMALL_MODEL) != coded(stream, UNFILLED_MODEL)
                for stream in streams(payloads)
            ), (kind, size)
            outcomes.append(False)
        else:
            assert measured.tolist() == expected.tolist(), (kind, size)
            outcomes.append(True)
    if kind == "random":
        # Both sides of the edge are reached, so neither half above is vacuous.
        assert True in outcomes and False in outcomes


# --- The PPMd stream residue (ADR 0027) ------------------------------------------------


def test_no_worker_holds_more_residue_than_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every worker is a process of its own, replaced before a task would take the PPMd
    stream residue it holds past its budget, and never before its first task (ADR 0027)."""
    payloads = [random.Random(index).randbytes(200) for index in range(6)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    built: list[dict[str, Any]] = []
    held: list[int] = []
    largest_task = [0]
    streams: dict[str, Callable[[Any], int]] = {
        "_size_worker": lambda _: 1,
        "_check_worker": lambda _: 2,
        "_escalation_worker": lambda _: 1,
        "_worker": lambda shard: 2 * len(shard[1]),
    }

    class RecordingExecutor:
        def __init__(self, **options: Any) -> None:
            built.append(options)
            held.append(0)
            self.slot = len(held) - 1

        def submit(self, function: Any, argument: Any) -> Future[Any]:
            residue = PPMD_STREAM_RESIDUE_BYTES * streams[function.__name__](argument)
            held[self.slot] += residue
            largest_task[0] = max(largest_task[0], residue)
            future: Future[Any] = Future()
            future.set_result(function(argument))
            return future

        def shutdown(self, **_options: Any) -> None:
            pass

    monkeypatch.setattr(distance_api, "ProcessPoolExecutor", RecordingExecutor)
    # One worker under a limit too small to hold its residue in process: a process of its
    # own, replaced as its residue grows.
    config = DistanceConfig(
        compressor=PpmdCompressor(ppmd_memory_bytes=1 << 20),
        workers=1,
        compression_chunk_bytes=1_024,
        max_working_memory_bytes=(1 << 20) + 2_048 + 24 + 6 * PPMD_STREAM_RESIDUE_BYTES,
    )
    _, budget, pooled = config.stream_residue(6, 15)
    assert pooled and budget is not None
    compute_distance_matrix(manifest_path, tmp_path / "run", config=config)
    assert all(options["max_workers"] == 1 for options in built)
    assert all("max_tasks_per_child" not in options for options in built)
    assert len(built) > 1
    assert max(held) <= max(budget, largest_task[0])


@pytest.mark.parametrize(("workers", "shards"), [(1, 3), (2, 1)])
def test_replacing_workers_on_a_queue_finishes(tmp_path: Path, workers: int, shards: int) -> None:
    """Workers replaced after a few shards, with more tasks queued than workers, must finish.
    CPython's own `max_tasks_per_child` hangs here under 3.11 and 3.12 up to at least 3.12.3:
    a retired worker is not replaced while tasks wait (ADR 0027). Run in a subprocess, so a
    hang fails this test instead of the suite."""
    count = 40
    payloads = [random.Random(index).randbytes(300) for index in range(count)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    pairs = count * (count - 1) // 2
    probe = DistanceConfig(
        compressor=PpmdCompressor(ppmd_memory_bytes=1 << 20),
        workers=workers,
        compression_chunk_bytes=1_024,
    )
    shard = probe.effective_pairs_per_shard(pairs)
    per_task = PPMD_STREAM_RESIDUE_BYTES * shard * 2
    # Room for `shards` shards per worker and not one more.
    limit = workers * (probe.worker_memory_bytes + per_task * shards + per_task // 2)
    limit += shard * 24
    _, budget, pooled = probe.model_copy(update={"max_working_memory_bytes": limit}).stream_residue(
        count, pairs
    )
    assert pooled and budget is not None
    assert per_task * shards <= budget < per_task * (shards + 1)
    script = textwrap.dedent(
        f"""
        from damicore_distance import DistanceConfig, PpmdCompressor, compute_distance_matrix
        if __name__ == "__main__":
            config = DistanceConfig(
                compressor=PpmdCompressor(ppmd_memory_bytes=1 << 20),
                workers={workers},
                compression_chunk_bytes=1_024,
                max_working_memory_bytes={limit},
            )
            compute_distance_matrix(
                {str(manifest_path)!r}, {str(tmp_path / "run")!r}, config=config
            )
        """
    )
    (tmp_path / "replaced.py").write_text(script, encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, str(tmp_path / "replaced.py")],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr[-2_000:]
    assert (tmp_path / "run" / "distance.npy").exists()


def test_replacing_workers_changes_no_value(tmp_path: Path) -> None:
    """Replacement is operational: a pool whose workers are replaced after every shard
    measures the matrix a single in-process worker measures, bit for bit."""
    payloads = [random.Random(index).randbytes(300) for index in range(5)]
    manifest_path = _normalization_manifest(tmp_path / "normalized", payloads)
    model = PpmdCompressor(ppmd_memory_bytes=1 << 20)
    serial = compute_distance_matrix(
        manifest_path, tmp_path / "serial", config=DistanceConfig(compressor=model, workers=1)
    )
    replaced = compute_distance_matrix(
        manifest_path,
        tmp_path / "replaced",
        config=DistanceConfig(
            compressor=model,
            workers=2,
            pairs_per_shard=1,
            compression_chunk_bytes=1_024,
            max_working_memory_bytes=2 * ((1 << 20) + 2_048) + 2 * PPMD_STREAM_RESIDUE_BYTES,
        ),
    )
    assert (
        np.load(replaced.matrix_path, allow_pickle=False).tolist()
        == np.load(serial.matrix_path, allow_pickle=False).tolist()
    )


# --- The compressor window, issue 59 ----------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "window"),
    [
        pytest.param(DeflateCompressor(), 32_768, id="zlib"),
        pytest.param(DeflateCompressor(name="gzip"), 32_768, id="gzip"),
        pytest.param(Bzip2Compressor(), 900_000, id="bzip2-default-block"),
        pytest.param(Bzip2Compressor(bzip2_block_size_100k=1), 100_000, id="bzip2-smallest-block"),
        pytest.param(LzmaCompressor(), 8_388_608, id="lzma-default-dictionary"),
        pytest.param(LzmaCompressor(lzma_dictionary_bytes=4_096), 4_096, id="lzma-smallest"),
        pytest.param(PpmdCompressor(), None, id="ppmd-declares-none"),
    ],
)
def test_each_compressor_declares_what_it_can_match_across(
    settings: CompressorSettings, window: int | None
) -> None:
    """The window is the compressor's own, and PPMd's is `None` rather than a number.

    A model is not a window, and PPMd's `None` is the measured outcome rather than a gap:
    the model's size does not decide what it finds across a pair, so there is no size to
    gate on. The measurement is in [adr-0023].
    """
    assert settings.window_bytes == window


def _near_copies(line_count: int) -> tuple[bytes, bytes]:
    """A column of ``line_count`` values and a copy with one byte in a hundred changed."""
    rng = random.Random(59)
    vocabulary = [f"value-{index:05d}" for index in range(2_000)]
    lines = [json.dumps(rng.choice(vocabulary)) for _ in range(line_count)]
    left = ("\n".join(lines) + "\n").encode("utf-8")
    perturbed = bytearray(left)
    for index in range(0, len(perturbed), 100):
        perturbed[index] = (perturbed[index] % 26) + 65
    return left, bytes(perturbed)


def test_a_pair_the_window_cannot_hold_measures_as_unrelated(tmp_path: Path) -> None:
    """The defect the gate exists for, shown rather than asserted.

    Two objects that differ in 1 % of their bytes are 40 KiB each, so their concatenation is
    past DEFLATE's 32 KiB window and the first is out of view while the second is
    compressed. Under `zlib` they measure as nearly unrelated; under an LZMA dictionary that
    holds the pair, the same two objects measure as the near-copies they are.
    """
    left, right = _near_copies(2_600)
    assert len(left) > 32_768

    def ncd(settings: CompressorSettings) -> float:
        config = DistanceConfig(compressor=settings)
        paths: list[Path] = []
        for name, payload in (("left", left), ("right", right)):
            path = tmp_path / f"{name}-{settings.name}"
            path.write_bytes(payload)
            paths.append(path)
        sizes = [compressed_size([path], config=config) for path in paths]
        together = compressed_size(paths, config=config)
        return normalized_compression_distance(sizes[0], sizes[1], together)

    past_the_window = ncd(DeflateCompressor())
    inside_it = ncd(LzmaCompressor())
    assert past_the_window > 0.5
    assert inside_it < 0.25
    # The same pair, measured by two compressors: the difference is the window, not the data.
    assert past_the_window > 2 * inside_it


def test_interleaving_holds_a_pair_the_window_cannot_until_a_block_outgrows_it(
    tmp_path: Path,
) -> None:
    """Issue 62, the case interleaving exists for. Two 100 KiB near-copies under DEFLATE's
    32 KiB window: concatenated, the first is out of view while the second is coded, and the
    pair measures as unrelated and is counted over the window. In 1 KiB blocks each block of
    one lies a block behind the same block of the other, in view, and the pair measures as the
    near-copies it is, counted over nothing. In 40 KiB blocks the block again outgrows the
    window: unrelated again, and counted, which is what the gate reads (ADR 0026)."""
    left, right = _near_copies(7_500)
    assert len(left) > 3 * 32_768
    manifest_path = _normalization_manifest(tmp_path / "normalized", [left, right])

    def measured(name: str, pairing: Pairing) -> tuple[float, int | None]:
        result = compute_distance_matrix(
            manifest_path,
            tmp_path / name,
            config=DistanceConfig(compressor=DeflateCompressor(), workers=1, pairing=pairing),
        )
        matrix = np.load(result.matrix_path, allow_pickle=False)
        return float(matrix[0, 1]), result.pairs_over_window

    concatenated, concatenated_over = measured("concat", ConcatPairing())
    fine, fine_over = measured("fine", InterleavePairing(interleave_block_bytes=1_024))
    coarse, coarse_over = measured("coarse", InterleavePairing(interleave_block_bytes=40_960))
    assert (concatenated_over, fine_over, coarse_over) == (1, 0, 1)
    # Measured: 0.983 concatenated, 0.312 in 1 KiB blocks, 0.848 in 40 KiB blocks.
    assert concatenated > 2 * fine
    assert coarse > 2 * fine


@given(
    sizes=st.lists(st.integers(min_value=1, max_value=4_096), min_size=2, max_size=12),
    window=st.integers(min_value=1, max_value=8_192),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_over_window_count_matches_counting_every_pair(sizes: list[int], window: int) -> None:
    """The two-pointer count against the definition it implements.

    The loop is O(n) where the definition is O(n^2), and the cases that separate them are
    equal sizes, every pair over the window and none over it, which a range of random size
    lists and windows reaches.
    """
    expected = sum(1 for left, right in itertools.combinations(sizes, 2) if left + right > window)
    assert _pairs_over_window(sizes, window) == expected


def test_no_declared_window_means_no_pair_is_counted() -> None:
    """`None` is not a window of zero: with nothing declared, nothing can be over it."""
    assert _pairs_over_window([10_000, 20_000], None) == 0
