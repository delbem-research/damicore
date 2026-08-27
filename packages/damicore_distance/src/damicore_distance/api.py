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
import zlib
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ValidationError

from damicore_distance.artifacts import (
    CompressedSizesCheckpoint,
    DistanceShardsCheckpoint,
    LabelsArtifact,
    NormalizationManifest,
)
from damicore_distance.compressor import compressed_size
from damicore_distance.config import DistanceConfig
from damicore_distance.errors import DistanceError
from damicore_distance.matrix import DistanceResult
from damicore_distance.ncd import normalized_compression_distance
from damicore_distance.shards import iter_pair_shards

logger = logging.getLogger(__name__)
CheckpointT = TypeVar("CheckpointT", bound=BaseModel)


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


def _load_objects(manifest_path: Path) -> tuple[list[str], list[str], list[Path]]:
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
    if (
        len(object_ids) < 2
        or len(set(object_ids)) != len(object_ids)
        or len(set(labels)) != len(labels)
    ):
        raise DistanceError(
            "Normalization manifest must contain at least two unique objects and labels",
            code="artifact_validation_error",
        )
    return object_ids, labels, paths


# One shard's work: its index, its pairs, and the run-wide inputs a worker process needs.
WorkerArguments = tuple[
    int, list[tuple[int, int]], list[str], list[int], Literal["zlib", "gzip"], int, int
]
WorkerResult = tuple[int, list[int], list[int], list[float]]


def _worker(arguments: WorkerArguments) -> WorkerResult:
    shard_index, pairs, raw_paths, sizes, compressor, level, chunk_bytes = arguments
    paths = [Path(path) for path in raw_paths]
    left: list[int] = []
    right: list[int] = []
    values: list[float] = []
    for i, j in pairs:
        cxy = compressed_size(
            (paths[i], paths[j]),
            compressor=compressor,
            level=level,
            chunk_bytes=chunk_bytes,
        )
        left.append(i)
        right.append(j)
        values.append(normalized_compression_distance(sizes[i], sizes[j], cxy))
    return shard_index, left, right, values


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


def _typed_results(results: Iterator[WorkerResult]) -> Iterator[WorkerResult]:
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
        "zlib_build": zlib.ZLIB_VERSION,
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
    }


def _checkpoint_identity(
    manifest_path: Path,
    settings: DistanceConfig,
    object_ids: list[str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "normalization_manifest_sha256": _sha256(manifest_path),
        "object_ids": object_ids,
        "config": settings.model_dump(mode="json"),
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
    sizes = [
        compressed_size(
            (path,),
            compressor=settings.compressor,
            level=settings.compression_level,
            chunk_bytes=settings.compression_chunk_bytes,
        )
        for path in paths
    ]
    checkpoint = CompressedSizesCheckpoint(identity=identity, sizes=tuple(sizes))
    _atomic_json(checkpoint_path, checkpoint.model_dump(mode="json"))
    return sizes


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
) -> dict[int, str]:
    if not path.exists():
        return {}
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
    return {index: checkpoint.digests[str(index)] for index in completed}


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
    and ``ncd_out_of_range_count`` on the result.

    Pairs are computed shard by shard straight into the memory map, and each finished shard
    is checkpointed with a digest, so an interrupted run resumes from the shards already
    done. With ``config.resume`` disabled, existing outputs are a conflict instead of a
    starting point. With more than one worker the shards run in spawned processes that
    re-import the caller's ``__main__``, so a script-level call must sit under
    ``if __name__ == "__main__":``.

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
    object_ids, labels, paths = _load_objects(manifest_path)
    identity = _checkpoint_identity(manifest_path, settings, object_ids)
    sizes = _load_or_compute_sizes(
        checkpoints / "compressed-sizes.json",
        identity,
        paths,
        settings,
    )

    pair_count = len(paths) * (len(paths) - 1) // 2
    shard_count = math.ceil(pair_count / settings.pairs_per_shard)
    matrix_path = destination / "distance.npy"
    matrix = _open_matrix(matrix_path, len(paths), resume=settings.resume)
    shard_checkpoint = checkpoints / "distance-shards.json"
    completed_shards = _load_completed_shards(
        shard_checkpoint,
        identity,
        pair_count,
        shard_count,
        resume=settings.resume,
    )
    completed_pairs = 0
    for index, shard in iter_pair_shards(len(paths), settings.pairs_per_shard):
        if index in completed_shards:
            _validate_completed_shard(matrix, shard)
            if _shard_digest(matrix, shard) != completed_shards[index]:
                raise DistanceError(
                    "Completed shard digest does not match distance.npy",
                    code="checkpoint_mismatch_error",
                )
            completed_pairs += len(shard)
    raw_paths = [str(path) for path in paths]
    # Annotated rather than inferred: a tuple display widens the compressor literal to str,
    # which would put the widening back that the parameter type exists to prevent.
    arguments: Iterator[WorkerArguments] = (
        (
            index,
            shard,
            raw_paths,
            sizes,
            settings.compressor,
            settings.compression_level,
            settings.compression_chunk_bytes,
        )
        for index, shard in iter_pair_shards(len(paths), settings.pairs_per_shard)
        if index not in completed_shards
    )
    if progress is not None:
        progress(completed_pairs, pair_count, "distance")
    executor: ProcessPoolExecutor | None = None
    results: Iterator[WorkerResult]
    if settings.effective_workers == 1:
        results = map(_worker, arguments)
    else:
        executor = ProcessPoolExecutor(
            max_workers=settings.effective_workers,
            mp_context=get_context("spawn"),
        )
        # Two shards per worker: enough that a worker never idles waiting for the next
        # submission, small enough that the queued payload stays proportional to the pool.
        results = _bounded_submit(executor, arguments, settings.effective_workers * 2)
    results = _typed_results(results)
    try:
        for shard_index, left, right, values in results:
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
            )
            _atomic_json(
                shard_checkpoint,
                checkpoint.model_dump(mode="json"),
            )
            completed_pairs += len(values)
            if progress is not None:
                progress(completed_pairs, pair_count, "distance")
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
    )
