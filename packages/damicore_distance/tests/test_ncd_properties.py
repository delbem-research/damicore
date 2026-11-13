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
import json
import lzma
import random
import zlib
from pathlib import Path
from typing import Literal

import numpy as np
import pyppmd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from damicore_distance import (
    Bzip2Compressor,
    DeflateCompressor,
    DistanceConfig,
    LzmaCompressor,
    PpmdCompressor,
    compute_distance_matrix,
)
from damicore_distance.compressor import compressed_size
from damicore_distance.ncd import normalized_compression_distance

pytestmark = pytest.mark.unit

# Small enough that a property runs in milliseconds, large enough to cross the chunk sizes
# exercised below and to give the deflate window something to match on.
PAYLOAD = st.binary(max_size=96)
CHUNK_BYTES = st.integers(min_value=1, max_value=48)
Compressor = Literal["zlib", "gzip", "ppmd", "bzip2", "lzma"]
CompressorSettings = DeflateCompressor | PpmdCompressor | Bzip2Compressor | LzmaCompressor
COMPRESSOR: st.SearchStrategy[Compressor] = st.sampled_from(
    ["zlib", "gzip", "ppmd", "bzip2", "lzma"]
)
LEVEL = st.integers(min_value=0, max_value=9)


def _config(compressor: Compressor, level: int, chunk_bytes: int) -> DistanceConfig:
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


def _named(compressor: Compressor) -> DistanceConfig:
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
            return len(
                pyppmd.compress(
                    joined,
                    max_order=settings.ppmd_order,
                    mem_size=settings.ppmd_memory_bytes,
                    variant="I",
                )
            )
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
    compressor: Compressor,
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
    compressor: Compressor,
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


def _copy_separation(directory: Path, compressor: Compressor, size: int) -> float:
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


@given(payloads=st.lists(PAYLOAD, min_size=2, max_size=4))
@settings(
    max_examples=30,
    deadline=None,
    # The temporary directory comes from the session-scoped factory, so each example gets a
    # fresh one; only the unused function-scoped fixtures would trip this check.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_random_small_matrices_hold_the_invariants_and_the_exact_pair_values(
    tmp_path_factory: pytest.TempPathFactory, payloads: list[bytes]
) -> None:
    directory = tmp_path_factory.mktemp("matrix")
    manifest_path = _normalization_manifest(directory / "normalized", payloads)
    result = compute_distance_matrix(
        manifest_path,
        directory / "run",
        config=DistanceConfig(workers=1, pairs_per_shard=1),
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
            joined = _direct_size(payloads[left], payloads[right], config=config)
            expected = normalized_compression_distance(sizes[left], sizes[right], joined)
            assert float(matrix[left, right]) == expected

    # The first of the longest objects, compressed after itself: C(xx) against C(x).
    largest = max(range(count), key=lambda index: len(payloads[index]))
    doubled = _direct_size(payloads[largest], payloads[largest], config=config)
    assert result.ncd_self_largest == normalized_compression_distance(
        sizes[largest], sizes[largest], doubled
    )
