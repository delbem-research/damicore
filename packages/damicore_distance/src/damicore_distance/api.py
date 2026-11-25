from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
import platform
import struct
import tempfile
import time
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Protocol, TypeVar

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ValidationError

from damicore_distance.artifacts import (
    CompressedSizesCheckpoint,
    DistanceShardsCheckpoint,
    LabelsArtifact,
    NormalizationManifest,
)
from damicore_distance.compressor import (
    codec_fingerprint,
    compressed_fingerprint,
    compressed_size,
)
from damicore_distance.config import DistanceConfig, PpmdCompressor
from damicore_distance.errors import DistanceError
from damicore_distance.matrix import DistanceResult
from damicore_distance.ncd import normalized_compression_distance
from damicore_distance.shards import iter_pair_shards

logger = logging.getLogger(__name__)
CheckpointT = TypeVar("CheckpointT", bound=BaseModel)
ResultT = TypeVar("ResultT")


class ProgressCallback(Protocol):
    def __call__(self, completed: int, total: int, message: str) -> None: ...


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
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


def _sha256(path: Path, chunk_bytes: int = 4_194_304) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _load_objects(manifest_path: Path) -> tuple[list[str], list[str], list[Path], list[str]]:
    try:
        manifest = NormalizationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise DistanceError(
            "Invalid normalization manifest",
            code="artifact_validation_error",
        ) from exc
    object_ids: list[str] = []
    labels: list[str] = []
    paths: list[Path] = []
    digests: list[str] = []
    root = manifest_path.parent.resolve()
    for raw in manifest.objects:
        relative = Path(raw.relative_path)
        # Resolved before it is judged, so what is decided here is containment and kind: an
        # entry linking out of the artifact root resolves outside it and fails is_relative_to,
        # and one linking within is read like any other object, its bytes still held to the
        # recorded digest below.
        candidate = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not candidate.is_relative_to(root)
            or not candidate.is_file()
        ):
            raise DistanceError(
                "Normalization object path escapes its artifact root",
                code="artifact_validation_error",
            )
        if candidate.stat().st_size != raw.size_bytes or _sha256(candidate) != raw.sha256:
            raise DistanceError(
                "Normalization object hash or size mismatch",
                code="artifact_validation_error",
            )
        object_ids.append(raw.object_id)
        labels.append(raw.label)
        paths.append(candidate)
        digests.append(raw.sha256)
    if (
        len(object_ids) < 2
        or len(set(object_ids)) != len(object_ids)
        or len(set(labels)) != len(labels)
    ):
        raise DistanceError(
            "Normalization manifest must contain at least two unique objects and labels",
            code="artifact_validation_error",
        )
    return object_ids, labels, paths, digests


# One shard's work: its index, its pairs, and the run-wide inputs a worker process needs --
# each object's path, its SHA-256 and its compressed size.
WorkerArguments = tuple[int, list[tuple[int, int]], list[str], list[str], list[int], DistanceConfig]
# The shard's values, or the first pair whose PPMd model restarted [adr-0025]. Returned rather
# than raised: an exception crosses the pool without its code.
WorkerResult = tuple[int, list[int], list[int], list[float], tuple[int, int] | None]
# One stream's work: the paths it compresses in order and the run's settings -- one path for
# C(x), the largest object twice for C(xx).
SizeArguments = tuple[tuple[str, ...], DistanceConfig]


def _size_worker(arguments: SizeArguments) -> int:
    raw_paths, config = arguments
    return compressed_size(tuple(Path(path) for path in raw_paths), config=config)


def _check_worker(arguments: SizeArguments) -> bool:
    """Whether a stream is coded into the same bytes under half the PPMd model as under all
    of it [adr-0025]."""
    raw_paths, config = arguments
    check = _model_check(config)
    if check is None:
        return True
    paths = tuple(Path(path) for path in raw_paths)
    return (
        compressed_fingerprint(paths, config=config)[1]
        == compressed_fingerprint(paths, config=check)[1]
    )


def _model_check(settings: DistanceConfig) -> DistanceConfig | None:
    """The same settings with half the PPMd model, or ``None`` when no check applies.

    PPMd codes identically whatever its memory until the memory fills, and then restarts its
    model from nothing. A stream coded into the same bytes under half the model and under all
    of it therefore gives, under the whole model, the size any larger one gives: the measured
    value is the objects' own, unless both models restarted and still coded the same bytes
    [adr-0025]. Bytes and not sizes: sizes alone were found to coincide across two restarts.
    Half and not twice, so the check never needs more memory than the run already declares;
    the price is refusing a stream that fills more than half the model and less than all of
    it, which a larger ``ppmd_memory_bytes`` admits.
    """
    compressor = settings.compressor
    if not isinstance(compressor, PpmdCompressor) or settings.allow_model_restart:
        return None
    halved = compressor.model_copy(update={"ppmd_memory_bytes": compressor.ppmd_memory_bytes // 2})
    return settings.model_copy(update={"compressor": halved})


def _worker(arguments: WorkerArguments) -> WorkerResult:
    shard_index, pairs, raw_paths, digests, sizes, config = arguments
    check = _model_check(config)
    paths = [Path(path) for path in raw_paths]
    left: list[int] = []
    right: list[int] = []
    values: list[float] = []
    for i, j in pairs:
        # No compressor here is symmetric, so the pair goes in the order of its contents'
        # digests, never of the input: a reordered input then measures the same value. Equal
        # digests are equal bytes, where the order cannot matter [adr-0024].
        first, second = (i, j) if digests[i] <= digests[j] else (j, i)
        pair = (paths[first], paths[second])
        if check is None:
            cxy = compressed_size(pair, config=config)
        else:
            cxy, coded = compressed_fingerprint(pair, config=config)
            if compressed_fingerprint(pair, config=check)[1] != coded:
                return shard_index, left, right, values, (i, j)
        left.append(i)
        right.append(j)
        values.append(normalized_compression_distance(sizes[i], sizes[j], cxy))
    return shard_index, left, right, values, None


def _bounded_submit(
    executor: ProcessPoolExecutor,
    arguments: Iterator[WorkerArguments],
    limit: int,
) -> Iterator[WorkerResult]:
    """Yield worker results in submission order, keeping at most `limit` shards in flight.

    ``Executor.map`` cannot be used here: it materialises its whole argument iterable before
    yielding anything, and every shard's tuple carries a full copy of the object paths and
    the compressed sizes. The pending payload would then grow with the pair count rather
    than staying bounded by the worker count, which is what the streaming invariant of this
    stage requires.
    """
    pending: deque[Future[WorkerResult]] = deque()
    for argument in arguments:
        pending.append(executor.submit(_worker, argument))
        if len(pending) >= limit:
            yield pending.popleft().result()
    while pending:
        yield pending.popleft().result()


# A spawned worker re-imports the caller's __main__ module, so a module-level call to this
# stage re-enters it inside the child, which multiprocessing refuses. The pool then reports
# only that some process died. The child's own explanation does reach stderr, but once per
# worker and above the traceback the caller actually reads, so both real causes are named
# here instead of leaving `BrokenProcessPool` as the whole diagnosis.
_BROKEN_POOL_MESSAGE = (
    "The distance worker pool died. Either this call runs at module level in a script and "
    'needs to be guarded with `if __name__ == "__main__":`, or a worker was killed by the '
    "operating system, typically for running out of memory. Passing workers=1 avoids the "
    "pool entirely."
)


def _typed_results(results: Iterator[ResultT]) -> Iterator[ResultT]:
    """Yield worker results, reporting a failed worker as a typed error.

    Both concurrency modes pass through here, so the failure contract does not depend on the
    worker count. Every public failure must carry a stable code, and an exception raised
    inside a worker would otherwise reach the caller as whatever type it happened to be:
    MemoryError on an oversized shard, zlib.error on a corrupt stream.
    """
    iterator = iter(results)
    while True:
        try:
            value = next(iterator)
        except StopIteration:
            return
        except DistanceError:
            raise
        except BrokenProcessPool as exc:
            raise DistanceError(_BROKEN_POOL_MESSAGE, code="distance_computation_error") from exc
        except Exception as exc:
            raise DistanceError(
                "Distance worker failed", code="distance_computation_error"
            ) from exc
        # Yielded outside the guard: a DistanceError raised by the consumer is its own, not a
        # worker failure to be relabelled.
        yield value


def _validated_matrix_statistics(
    matrix: npt.NDArray[np.float64], block_size: int = 512
) -> tuple[float, float, int]:
    """Enforce the matrix invariants and return its range from the same traversal.

    Validation already reads every cell to prove finiteness, so the minimum, the maximum,
    and the count of values outside ``[0, 1]`` are accumulated here rather than by a second
    pass elsewhere. NCD is unclamped, so a value outside the unit interval is reported and
    never repaired; it is the caller's signal that the compressor behaved unusually.

    The statistics cover the whole matrix, diagonal included.
    """
    if matrix.dtype != np.float64 or matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise DistanceError(
            "Distance matrix has invalid shape or dtype",
            code="distance_matrix_validation_error",
        )
    size = matrix.shape[0]
    minimum = float("inf")
    maximum = float("-inf")
    out_of_range = 0
    for start in range(0, size, block_size):
        stop = min(start + block_size, size)
        block = matrix[start:stop]
        if not np.isfinite(block).all():
            raise DistanceError(
                "Distance matrix contains NaN or infinity",
                code="distance_matrix_validation_error",
            )
        minimum = min(minimum, float(np.min(block)))
        maximum = max(maximum, float(np.max(block)))
        # np.count_nonzero's stub is partially unknown under strict mode; the block is typed.
        out_of_range += int(
            np.count_nonzero(  # pyright: ignore[reportUnknownMemberType]
                np.logical_or(block < 0, block > 1)
            )
        )
        for row in range(start, stop):
            if float(matrix[row, row]) != 0.0:
                raise DistanceError(
                    "Distance matrix diagonal must be exactly zero",
                    code="distance_matrix_validation_error",
                )
            # pyright: ignore is on np.array_equal, whose numpy stub is partially
            # unknown under strict mode; the arrays themselves are fully typed.
            symmetric = np.array_equal(  # pyright: ignore[reportUnknownMemberType]
                matrix[row, :], matrix[:, row]
            )
            if not bool(symmetric):
                raise DistanceError(
                    "Distance matrix must be bitwise symmetric",
                    code="distance_matrix_validation_error",
                )
    return minimum, maximum, out_of_range


def _runtime_fingerprint() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        **codec_fingerprint(),
    }


def _checkpoint_identity(
    manifest_path: Path,
    settings: DistanceConfig,
    object_ids: list[str],
) -> dict[str, object]:
    return {
        # 2 since pairs are compressed in digest order [adr-0024]: a shard written in input
        # order holds other values, and resuming it would mix the two in one matrix.
        "schema_version": 2,
        "normalization_manifest_sha256": _sha256(manifest_path),
        "object_ids": object_ids,
        # These settings change cost or control flow, never a stored size or shard value, so a
        # checkpoint stays valid across them; `pairs_per_shard` stays in because it is the
        # layout the shards are indexed by (ADR 0022).
        "config": settings.model_dump(
            mode="json",
            exclude={
                "workers",
                "compression_chunk_bytes",
                "resume",
                "save_diagnostics",
                "allow_model_restart",
            },
        ),
        "runtime": _runtime_fingerprint(),
    }


def _read_checkpoint(path: Path, model: type[CheckpointT]) -> CheckpointT:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise DistanceError(
            f"Could not read checkpoint {path.name}",
            code="checkpoint_mismatch_error",
        ) from exc


def _load_or_compute_sizes(
    checkpoint_path: Path,
    identity: dict[str, object],
    paths: list[Path],
    settings: DistanceConfig,
    executor: ProcessPoolExecutor | None,
) -> list[int]:
    if checkpoint_path.exists():
        if not settings.resume:
            raise DistanceError(
                "Distance checkpoint exists but resume is disabled",
                code="output_directory_conflict_error",
            )
        checkpoint = _read_checkpoint(checkpoint_path, CompressedSizesCheckpoint)
        if checkpoint.identity != identity:
            raise DistanceError(
                "Compressed-size checkpoint is incompatible",
                code="checkpoint_mismatch_error",
            )
        if len(checkpoint.sizes) != len(paths):
            raise DistanceError(
                "Compressed-size checkpoint is incomplete",
                code="checkpoint_mismatch_error",
            )
        return list(checkpoint.sizes)
    arguments: list[SizeArguments] = [((str(path),), settings) for path in paths]
    # Executor.map submits every argument up front, which is bounded here: one short tuple
    # per object, where a shard's carries every path and size (see _bounded_submit).
    results = (
        map(_size_worker, arguments) if executor is None else executor.map(_size_worker, arguments)
    )
    sizes = list(_typed_results(results))
    checkpoint = CompressedSizesCheckpoint(identity=identity, sizes=tuple(sizes))
    _atomic_json(checkpoint_path, checkpoint.model_dump(mode="json"))
    return sizes


def _model_capacity_error(subject: str) -> DistanceError:
    return DistanceError(
        f"{subject} more than half the PPMd model, past which the model can fill, "
        "restart and code the rest with what it had read forgotten, so the distance would not be "
        "the one the objects have. Raise ppmd_memory_bytes -- each worker is charged that "
        "memory -- or set allow_model_restart to measure anyway. See ADR 0025.",
        code="model_capacity_error",
    )


def _refuse_model_restart(
    paths: list[Path],
    labels: list[str],
    settings: DistanceConfig,
    executor: ProcessPoolExecutor | None,
) -> None:
    """Refuse, before any pair, a PPMd run in which an object alone fills more than half the
    model [adr-0025].

    Every pair with it holds it, so no pair needs computing to know the run cannot be
    admitted: two compressions per object here spare the pairs.
    """
    if _model_check(settings) is None:
        return
    arguments: list[SizeArguments] = [((str(path),), settings) for path in paths]
    results = (
        map(_check_worker, arguments)
        if executor is None
        else executor.map(_check_worker, arguments)
    )
    over = [index for index, fits in enumerate(_typed_results(results)) if not fits]
    if over:
        raise _model_capacity_error(
            f"{len(over)} of {len(paths)} objects, among them {labels[over[0]]!r}, each fill"
        )


def _open_matrix(path: Path, size: int, *, resume: bool) -> np.memmap[Any, Any]:
    if path.exists():
        if not resume:
            raise DistanceError(
                "distance.npy exists but resume is disabled",
                code="output_directory_conflict_error",
            )
        try:
            matrix = np.load(path, mmap_mode="r+", allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise DistanceError(
                "Existing distance.npy is corrupt",
                code="checkpoint_mismatch_error",
            ) from exc
        if matrix.shape != (size, size) or matrix.dtype != np.float64:
            raise DistanceError(
                "Existing distance.npy is incompatible",
                code="checkpoint_mismatch_error",
            )
        return matrix
    matrix = np.lib.format.open_memmap(path, mode="w+", dtype=np.float64, shape=(size, size))
    matrix[:] = np.nan
    np.fill_diagonal(matrix, 0.0)
    matrix.flush()
    return matrix


def _load_completed_shards(
    path: Path,
    identity: dict[str, object],
    pair_count: int,
    shard_count: int,
    *,
    resume: bool,
) -> tuple[dict[int, str], set[int]]:
    if not path.exists():
        return {}, set()
    if not resume:
        raise DistanceError(
            "Distance shard checkpoint exists but resume is disabled",
            code="output_directory_conflict_error",
        )
    checkpoint = _read_checkpoint(path, DistanceShardsCheckpoint)
    if (
        checkpoint.identity != identity
        or checkpoint.pair_count != pair_count
        or checkpoint.shard_count != shard_count
    ):
        raise DistanceError(
            "Distance shard checkpoint is incompatible",
            code="checkpoint_mismatch_error",
        )
    completed = set(checkpoint.completed)
    if len(completed) != len(checkpoint.completed) or any(
        index < 0 or index >= shard_count for index in completed
    ):
        raise DistanceError("Invalid completed shard index", code="checkpoint_mismatch_error")
    expected_keys = {str(index) for index in completed}
    # Digest shape is already guaranteed by DistanceShardsCheckpoint's own validator, so
    # only the key set is checked here.
    if set(checkpoint.digests) != expected_keys:
        raise DistanceError("Invalid completed shard digests", code="checkpoint_mismatch_error")
    unchecked = set(checkpoint.unchecked)
    if len(unchecked) != len(checkpoint.unchecked) or not unchecked <= completed:
        raise DistanceError("Invalid unchecked shard index", code="checkpoint_mismatch_error")
    return {index: checkpoint.digests[str(index)] for index in completed}, unchecked


def _validate_completed_shard(
    matrix: np.ndarray[Any, Any],
    shard: list[tuple[int, int]],
) -> None:
    for i, j in shard:
        value = float(matrix[i, j])
        if not math.isfinite(value) or value != float(matrix[j, i]):
            raise DistanceError(
                "Completed shard contains missing or asymmetric values",
                code="checkpoint_mismatch_error",
            )


def _shard_digest(
    matrix: np.ndarray[Any, Any],
    shard: list[tuple[int, int]],
) -> str:
    digest = hashlib.sha256()
    for i, j in shard:
        digest.update(struct.pack(">QQd", i, j, float(matrix[i, j])))
    return digest.hexdigest()


def _write_diagnostics(
    destination: Path,
    matrix: np.ndarray[Any, Any],
    object_ids: list[str],
) -> None:
    diagnostics = destination / "diagnostics"
    diagnostics.mkdir(exist_ok=True)
    with (diagnostics / "distance.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["object_id", *object_ids])
        for index, object_id in enumerate(object_ids):
            writer.writerow([object_id, *(repr(float(value)) for value in matrix[index])])
    with (diagnostics / "ncd-pairs.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["i", "j", "object_id_x", "object_id_y", "ncd"])
        for i in range(len(object_ids)):
            for j in range(i + 1, len(object_ids)):
                writer.writerow([i, j, object_ids[i], object_ids[j], repr(float(matrix[i, j]))])


def _pairs_over_window(object_bytes: list[int], window: int | None) -> int:
    """Count the pairs whose two spans together exceed what the compressor matches across.

    A span is what the pairing makes the compressor hold of one object at once: the object
    when concatenated, one block of it when interleaved [adr-0026].

    Counted from the object sizes rather than during compression: the pair loop runs in
    workers and across shards, so a resumed run would otherwise count only the shards it
    ran. Sorted once, it is two pointers over the sizes, and the count is exact for every
    pair whether or not this run computed it.
    """
    if window is None:
        return 0
    ordered = sorted(object_bytes)
    left, right, over = 0, len(ordered) - 1, 0
    while left < right:
        if ordered[left] + ordered[right] > window:
            over += right - left
            right -= 1
        else:
            left += 1
    return over


def compute_distance_matrix(
    normalization_manifest: str | Path,
    output_dir: str | Path,
    *,
    config: DistanceConfig | None = None,
    progress: ProgressCallback | None = None,
) -> DistanceResult:
    """Compute or resume the exact NCD matrix from validated normalized objects.

    Takes the normalizer's ``manifest.json`` and re-verifies every object's path containment,
    size, and SHA-256 before reading it. Writes ``distance.npy`` and ``labels.json`` into
    ``output_dir``, plus ``checkpoints/compressed-sizes.json`` and
    ``checkpoints/distance-shards.json``; ``config.save_diagnostics`` adds a ``diagnostics/``
    directory. ``distance.npy`` is a float64 square memory map, validated as finite,
    zero-diagonal, and bitwise symmetric before this returns, and it is what
    :func:`damicore_tree_builder.build_tree` consumes together with ``labels.json``. That
    validation pass also measures the matrix range, reported as ``ncd_min``, ``ncd_max``
    and ``ncd_out_of_range_count`` on the result, and the largest object is compressed once
    more after itself for ``ncd_self_largest``.

    Each pair is compressed as one concatenation, the object whose SHA-256 is lower first, so
    the matrix is a function of the objects and not of their order in the manifest: reordered,
    it comes back with its rows and columns reordered alike and no value changed [adr-0024].

    Pairs are computed shard by shard straight into the memory map, and each finished shard
    is checkpointed with a digest, so an interrupted run resumes from the shards already
    done. With ``config.resume`` disabled, existing outputs are a conflict instead of a
    starting point. With more than one worker the per-object compressed sizes and the shards
    run in one pool of spawned processes that re-import the caller's ``__main__``, so a
    script-level call must sit under ``if __name__ == "__main__":``.

    Parameters
    ----------
    progress
        Called once before work begins -- so a resumed run reports the pairs it inherited --
        and again after each completed shard, as ``(completed, total, "distance")``.

    Raises
    ------
    DistanceError
        The normalization manifest or an object it names is invalid
        (``artifact_validation_error``); a checkpoint or an existing ``distance.npy`` is
        incompatible, corrupt, or disagrees with the matrix (``checkpoint_mismatch_error``);
        outputs exist but resume is disabled (``output_directory_conflict_error``); the
        compressor failed (``compression_error``); a worker died or returned an invalid shard
        (``distance_computation_error``); the finished matrix breaks an invariant
        (``distance_matrix_validation_error``).
    """
    started = time.monotonic()
    settings = config or DistanceConfig()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    checkpoints = destination / "checkpoints"
    checkpoints.mkdir(exist_ok=True)
    manifest_path = Path(normalization_manifest).resolve()
    object_ids, labels, paths, digests = _load_objects(manifest_path)
    identity = _checkpoint_identity(manifest_path, settings, object_ids)
    # One pool serves both phases, so neither the per-object sizes nor the pairs run
    # serially in this process while the workers wait.
    executor = (
        None
        if settings.effective_workers == 1
        else ProcessPoolExecutor(
            max_workers=settings.effective_workers,
            mp_context=get_context("spawn"),
        )
    )
    try:
        sizes = _load_or_compute_sizes(
            checkpoints / "compressed-sizes.json",
            identity,
            paths,
            settings,
            executor,
        )

        _refuse_model_restart(paths, labels, settings, executor)

        # Submitted ahead of the shards so a pool runs it beside them; without a pool, `map`
        # defers it until it is read after the last shard.
        # Ties in size go to the higher digest, so the object -- and the value -- is chosen by
        # content and not by input order, like the pairs [adr-0024].
        largest = max(
            range(len(paths)), key=lambda index: (paths[index].stat().st_size, digests[index])
        )
        doubled: list[SizeArguments] = [((str(paths[largest]),) * 2, settings)]
        self_size = (
            map(_size_worker, doubled) if executor is None else executor.map(_size_worker, doubled)
        )

        pair_count = len(paths) * (len(paths) - 1) // 2
        pairs_per_shard = settings.effective_pairs_per_shard(pair_count)
        shard_count = math.ceil(pair_count / pairs_per_shard)
        matrix_path = destination / "distance.npy"
        matrix = _open_matrix(matrix_path, len(paths), resume=settings.resume)
        shard_checkpoint = checkpoints / "distance-shards.json"
        completed_shards, unchecked = _load_completed_shards(
            shard_checkpoint,
            identity,
            pair_count,
            shard_count,
            resume=settings.resume,
        )
        lifted = isinstance(settings.compressor, PpmdCompressor) and settings.allow_model_restart
        if not lifted:
            # Measured with the check lifted, so computed again under it [adr-0025].
            for index in unchecked:
                del completed_shards[index]
            unchecked = set[int]()
        completed_pairs = 0
        for index, shard in iter_pair_shards(len(paths), pairs_per_shard):
            if index in completed_shards:
                _validate_completed_shard(matrix, shard)
                if _shard_digest(matrix, shard) != completed_shards[index]:
                    raise DistanceError(
                        "Completed shard digest does not match distance.npy",
                        code="checkpoint_mismatch_error",
                    )
                completed_pairs += len(shard)
        raw_paths = [str(path) for path in paths]
        arguments: Iterator[WorkerArguments] = (
            (index, shard, raw_paths, digests, sizes, settings)
            for index, shard in iter_pair_shards(len(paths), pairs_per_shard)
            if index not in completed_shards
        )
        if progress is not None:
            progress(completed_pairs, pair_count, "distance")
        results: Iterator[WorkerResult] = (
            map(_worker, arguments)
            if executor is None
            # Two shards per worker: enough that a worker never idles waiting for the next
            # submission, small enough that the queued payload stays proportional to the pool.
            else _bounded_submit(executor, arguments, settings.effective_workers * 2)
        )
        for shard_index, left, right, values, restarted in _typed_results(results):
            if restarted is not None:
                i, j = restarted
                raise _model_capacity_error(f"The pair {labels[i]!r} and {labels[j]!r} fills")
            if len(left) != len(values) or not all(math.isfinite(value) for value in values):
                raise DistanceError(
                    "Worker returned an invalid shard",
                    code="distance_computation_error",
                )
            for i, j, value in zip(left, right, values, strict=True):
                matrix[i, j] = value
                matrix[j, i] = value
            matrix.flush()
            _validate_completed_shard(matrix, list(zip(left, right, strict=True)))
            completed_shards[shard_index] = _shard_digest(
                matrix, list(zip(left, right, strict=True))
            )
            if lifted:
                unchecked.add(shard_index)
            logger.info(
                "shard_completed",
                extra={"shard": shard_index, "pairs": len(values)},
            )
            checkpoint = DistanceShardsCheckpoint(
                identity=identity,
                pair_count=pair_count,
                shard_count=shard_count,
                completed=tuple(sorted(completed_shards)),
                digests={str(index): completed_shards[index] for index in sorted(completed_shards)},
                unchecked=tuple(sorted(unchecked)),
            )
            _atomic_json(
                shard_checkpoint,
                checkpoint.model_dump(mode="json"),
            )
            completed_pairs += len(values)
            if progress is not None:
                progress(completed_pairs, pair_count, "distance")
        (doubled_size,) = _typed_results(self_size)
        ncd_self_largest = normalized_compression_distance(
            sizes[largest], sizes[largest], doubled_size
        )
    finally:
        if executor is not None:
            executor.shutdown(cancel_futures=True)

    ncd_min, ncd_max, out_of_range = _validated_matrix_statistics(matrix)
    labels_path = destination / "labels.json"
    labels_artifact = LabelsArtifact(
        schema_version=1,
        object_ids=tuple(object_ids),
        labels=tuple(labels),
    )
    _atomic_json(labels_path, labels_artifact.model_dump(mode="json"))
    if settings.save_diagnostics:
        _write_diagnostics(destination, matrix, object_ids)
    matrix.flush()
    del matrix
    return DistanceResult(
        matrix_path=matrix_path,
        labels_path=labels_path,
        object_count=len(paths),
        pair_count=pair_count,
        timing=time.monotonic() - started,
        ncd_min=ncd_min,
        ncd_max=ncd_max,
        ncd_out_of_range_count=out_of_range,
        ncd_self_largest=ncd_self_largest,
        pairs_over_window=_pairs_over_window(
            [settings.pairing.span_bytes(path.stat().st_size) for path in paths],
            settings.compressor.window_bytes,
        ),
    )
