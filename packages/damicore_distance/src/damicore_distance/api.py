from __future__ import annotations

import csv
import hashlib
import itertools
import json
import logging
import math
import os
import platform
import struct
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
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
from damicore_distance.compressor import (
    codec_fingerprint,
    compressed_fingerprint,
    compressed_size,
)
from damicore_distance.config import (
    PPMD_MEMORY_LIMIT,
    PPMD_STREAM_RESIDUE_BYTES,
    DistanceConfig,
    PpmdCompressor,
)
from damicore_distance.errors import DistanceError
from damicore_distance.matrix import DistanceResult
from damicore_distance.ncd import normalized_compression_distance
from damicore_distance.shards import iter_pair_shards

logger = logging.getLogger(__name__)
CheckpointT = TypeVar("CheckpointT", bound=BaseModel)
ResultT = TypeVar("ResultT")
ArgumentT = TypeVar("ArgumentT")


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
# The shard's values, and the pairs half the PPMd model coded differently, each with the bytes'
# digest under the whole model, for a second look under twice it [adr-0025].
Flagged = list[tuple[int, int, bytes]]
WorkerResult = tuple[int, list[int], list[int], list[float], Flagged]
# One stream's work: the paths it compresses in order and the run's settings -- one path for
# C(x), the largest object twice for C(xx).
SizeArguments = tuple[tuple[str, ...], DistanceConfig]


def _size_worker(arguments: SizeArguments) -> int:
    raw_paths, config = arguments
    return compressed_size(tuple(Path(path) for path in raw_paths), config=config)


def _check_worker(arguments: SizeArguments) -> bytes | None:
    """``None`` when a stream is coded into the same bytes under half the PPMd model as under
    all of it, else the digest of its bytes under all of it, for a second look [adr-0025]."""
    raw_paths, config = arguments
    check = _model_check(config)
    if check is None:
        return None
    paths = tuple(Path(path) for path in raw_paths)
    coded = compressed_fingerprint(paths, config=config)[1]
    return None if compressed_fingerprint(paths, config=check)[1] == coded else coded


# One stream's second look: its paths, the settings under twice the model, and the digest of
# its bytes under the model [adr-0025], behind a tag the refusal names it by.
EscalationArguments = tuple[int, tuple[str, ...], DistanceConfig, bytes]
# A stream waiting for its second look, as the caller hands it over: tag, paths, digest.
Look = tuple[int, tuple[Path, ...], bytes]
# A flagged pair on disk while the shards are computed: two object indices and the digest, so
# the waiting pairs cost the coordinator no memory however many there are.
_FLAGGED_RECORD = struct.Struct("<II32s")


def _escalation_worker(arguments: EscalationArguments) -> tuple[int, bool]:
    """Whether twice the model codes a stream into the bytes the model did: if so, the model
    did not restart on it, since its memory never mattered."""
    tag, raw_paths, escalation, coded = arguments
    paths = tuple(Path(path) for path in raw_paths)
    return tag, compressed_fingerprint(paths, config=escalation)[1] == coded


def _doubled_model(settings: DistanceConfig) -> DistanceConfig | None:
    """The settings under twice the PPMd model, or ``None`` when no check applies or the codec
    cannot take twice the model, which is then no look at all [adr-0025]."""
    compressor = settings.compressor
    if not isinstance(compressor, PpmdCompressor) or settings.allow_model_restart:
        return None
    doubled = 2 * compressor.ppmd_memory_bytes
    if doubled > PPMD_MEMORY_LIMIT:
        return None
    return settings.model_copy(
        update={"compressor": compressor.model_copy(update={"ppmd_memory_bytes": doubled})}
    )


Refusal = tuple[int, Literal["restarted", "no_look"]]


def _second_look(
    streams: Iterator[Look], settings: DistanceConfig, executor: _WorkerPool | None, opened: int
) -> Refusal | None:
    """The first stream to refuse and why, or ``None`` when every one passes [adr-0025]: twice
    the PPMd model codes it otherwise than the model (``"restarted"``), or no second look fits
    the working-memory limit beside the PPMd stream residue already held (``"no_look"``).

    A look runs only while no pair is in flight, so it shares the limit with residue alone
    [adr-0027], and with what is held now, not what the run could ever hold. In the calling
    process that is the residue of the ``opened`` streams this call opened before the look and
    of each look before the next. A pool's workers are instead held to a budget of their own
    for the look -- the limit less the looks it runs at once, shared among them -- and one that
    holds more is replaced first, while it is idle; the pool's budget is restored after. The
    streams are consumed as they are submitted, so neither they nor their results are ever all
    in memory.
    """
    first = next(streams, None)
    if first is None:
        return None
    escalation = _doubled_model(settings)
    if escalation is None:
        return first[0], "no_look"
    pending = itertools.chain((first,), streams)
    limit = settings.max_working_memory_bytes
    # A look holds a stream under twice the model and, once it ends, that stream's state.
    look_bytes = escalation.worker_memory_bytes + PPMD_STREAM_RESIDUE_BYTES
    if executor is None:
        for done, (tag, paths, coded) in enumerate(pending):
            if (opened + done) * PPMD_STREAM_RESIDUE_BYTES + look_bytes > limit:
                return tag, "no_look"
            arguments = (tag, tuple(str(path) for path in paths), escalation, coded)
            if not _escalation_worker(arguments)[1]:
                return tag, "restarted"
        return None
    workers = settings.effective_workers
    slots = min(workers, (limit - workers * PPMD_STREAM_RESIDUE_BYTES) // look_bytes)
    if slots <= 0:
        return first[0], "no_look"
    previous = executor.rebudget((limit - slots * look_bytes) // workers)
    try:
        arguments = (
            (tag, tuple(str(path) for path in paths), escalation, coded)
            for tag, paths, coded in pending
        )
        results = _bounded_submit(executor, _escalation_worker, arguments, slots, lambda _: 1)
        return next(((tag, "restarted") for tag, fits in _typed_results(results) if not fits), None)
    finally:
        executor.rebudget(previous)


def _model_check(settings: DistanceConfig) -> DistanceConfig | None:
    """The same settings with half the PPMd model, or ``None`` when no check applies.

    PPMd codes identically whatever its memory until the memory fills, and then restarts its
    model from nothing. A stream coded into the same bytes under half the model and under all
    of it therefore gives, under the whole model, the size any larger one gives: the measured
    value is the objects' own, unless both models restarted and still coded the same bytes
    [adr-0025]. Bytes and not sizes: sizes alone were found to coincide across two restarts.
    Half and not twice, so the check beside the pairs never needs more memory than the run
    already declares; a stream half the model codes otherwise is not refused on that alone,
    but looked at again under twice the model once no pair is in flight (``_second_look``).
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
    flagged: Flagged = []
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
                flagged.append((i, j, coded))
        left.append(i)
        right.append(j)
        values.append(normalized_compression_distance(sizes[i], sizes[j], cxy))
    return shard_index, left, right, values, flagged


class _WorkerPool:
    """Worker processes, each replaced before the PPMd stream residue it holds would pass its
    budget [adr-0027].

    One single-process executor per worker, not one executor with `max_tasks_per_child`:
    under CPython 3.11 and 3.12, 3.12.3 included, such an executor stops replacing retired
    workers while tasks are queued, and hangs. A task goes to a worker that can take it
    without replacement, the least busy of them; a worker is replaced only once the tasks it
    took have finished, and never before its first, whose residue cannot be split. With no
    budget, no worker is ever replaced.
    """

    def __init__(self, workers: int, budget: int | None) -> None:
        self._budget = budget
        self._executors = [self._spawn() for _ in range(workers)]
        self._held = [0] * workers
        self._in_flight: list[deque[Future[Any]]] = [deque() for _ in range(workers)]

    @staticmethod
    def _spawn() -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))

    def submit(
        self, function: Callable[[ArgumentT], ResultT], argument: ArgumentT, *, streams: int
    ) -> Future[ResultT]:
        """Submit a task that opens ``streams`` PPMd streams, replacing its worker first if
        their residue would take it past the budget."""
        residue = streams * PPMD_STREAM_RESIDUE_BYTES if self._budget is not None else 0
        for flight in self._in_flight:
            # One process per executor runs its tasks in order, so they finish in order.
            while flight and flight[0].done():
                flight.popleft()

        def full(index: int) -> bool:
            held = self._held[index]
            return self._budget is not None and held > 0 and held + residue > self._budget

        index = min(range(len(self._executors)), key=lambda i: (full(i), len(self._in_flight[i])))
        if full(index):
            self._executors[index].shutdown(wait=True)
            self._executors[index] = self._spawn()
            self._held[index] = 0
            self._in_flight[index].clear()
        try:
            future = self._executors[index].submit(function, argument)
        except BrokenProcessPool as exc:
            # A worker killed while idle breaks its executor before the next submission, which
            # no result read would ever report: typed here, on every phase's way in.
            raise DistanceError(_BROKEN_POOL_MESSAGE, code="distance_computation_error") from exc
        self._held[index] += residue
        self._in_flight[index].append(future)
        return future

    def map(
        self,
        function: Callable[[ArgumentT], ResultT],
        arguments: Iterable[ArgumentT],
        *,
        streams: int,
    ) -> Iterator[ResultT]:
        futures = [self.submit(function, argument, streams=streams) for argument in arguments]
        return (future.result() for future in futures)

    def shutdown(self, *, cancel_futures: bool) -> None:
        for executor in self._executors:
            executor.shutdown(cancel_futures=cancel_futures)

    def rebudget(self, budget: int | None) -> int | None:
        """Set the residue budget and return the one it replaces, first replacing every worker
        that holds more than the new one, once the tasks it took have finished."""
        previous = self._budget
        for index, held in enumerate(self._held):
            if budget is not None and held > budget:
                self._executors[index].shutdown(wait=True)
                self._executors[index] = self._spawn()
                self._held[index] = 0
                self._in_flight[index].clear()
        self._budget = budget
        return previous


def _bounded_submit(
    executor: _WorkerPool,
    function: Callable[[ArgumentT], ResultT],
    arguments: Iterator[ArgumentT],
    limit: int,
    streams: Callable[[ArgumentT], int],
    *,
    ordered: bool = True,
) -> Iterator[ResultT]:
    """Yield results, keeping at most `limit` tasks submitted and not yet yielded.

    ``Executor.map`` cannot be used here: it materialises its whole argument iterable before
    yielding anything, and every shard's tuple carries a full copy of the object paths and
    the compressed sizes. The pending payload would then grow with the pair count rather
    than staying bounded by the worker count, which is what the streaming invariant of this
    stage requires.

    ``ordered`` yields in submission order, for a caller whose answer depends on it. Without
    it, results come as tasks finish, oldest first among those finished: one slow task then
    holds back none behind it, where in order the window fills with finished results waiting
    for it and no worker is handed anything new (issue #141).
    """
    pending: deque[Future[ResultT]] = deque()
    for argument in arguments:
        pending.append(executor.submit(function, argument, streams=streams(argument)))
        if len(pending) >= limit:
            yield _next_result(pending, ordered=ordered)
    while pending:
        yield _next_result(pending, ordered=ordered)


def _next_result(pending: deque[Future[ResultT]], *, ordered: bool) -> ResultT:
    """Take one result from ``pending``: its oldest, or its oldest finished one."""
    if not ordered:
        wait(pending, return_when=FIRST_COMPLETED)
        finished = next(future for future in pending if future.done())
        pending.remove(finished)
        return finished.result()
    return pending.popleft().result()


# A spawned worker re-imports the caller's __main__ module, so a module-level call to this
# stage re-enters it inside the child, which multiprocessing refuses. The pool then reports
# only that some process died. The child's own explanation does reach stderr, but once per
# worker and above the traceback the caller actually reads, so both real causes are named
# here instead of leaving `BrokenProcessPool` as the whole diagnosis.
_BROKEN_POOL_MESSAGE = (
    "The distance worker pool died. Either this call runs at module level in a script and "
    'needs to be guarded with `if __name__ == "__main__":`, or a worker was killed by the '
    "operating system, typically for running out of memory. Passing workers=1 avoids the "
    "pool while the run's PPMd stream residue fits the working-memory limit (ADR 0027)."
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
                "max_working_memory_bytes",
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
    executor: _WorkerPool | None,
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
        map(_size_worker, arguments)
        if executor is None
        else executor.map(_size_worker, arguments, streams=1)
    )
    sizes = list(_typed_results(results))
    checkpoint = CompressedSizesCheckpoint(identity=identity, sizes=tuple(sizes))
    _atomic_json(checkpoint_path, checkpoint.model_dump(mode="json"))
    return sizes


def _model_capacity_error(
    subject: str, settings: DistanceConfig, *, cause: Literal["restarted", "no_look"]
) -> DistanceError:
    """The refusal, saying what is known about the stream and the move that would admit it
    [adr-0025]."""
    if cause == "restarted":
        found = (
            "fills the PPMd model, which restarted and coded the rest with what it had read "
            "forgotten -- twice the model codes it otherwise -- so the distance would not be the "
            "one the objects have"
        )
        advice = "Raise ppmd_memory_bytes -- each worker is charged that memory -- or set"
    elif _doubled_model(settings) is None:
        found = (
            "fills more than half the PPMd model, past which the model can restart, and twice "
            "the model is past what the codec takes, so nothing can tell whether it did"
        )
        advice = "Set"
    else:
        found = (
            "fills more than half the PPMd model, past which the model can restart, and "
            "max_working_memory_bytes leaves no room to compress it again under twice the "
            "model, which would tell"
        )
        advice = "Raise max_working_memory_bytes or ppmd_memory_bytes, or set"
    return DistanceError(
        f"{subject} {found}. {advice} allow_model_restart to measure anyway. See ADR 0025.",
        code="model_capacity_error",
    )


def _refuse_model_restart(
    paths: list[Path],
    labels: list[str],
    settings: DistanceConfig,
    executor: _WorkerPool | None,
) -> None:
    """Refuse, before any pair, a PPMd run in which an object alone restarts the model, or
    fills more than half of it with no room for a second look [adr-0025].

    Every pair with it holds it, so no pair needs computing to know the run cannot be
    admitted: two compressions per object here, and a third for one half the model codes
    otherwise, spare the pairs.
    """
    if _model_check(settings) is None:
        return
    arguments: list[SizeArguments] = [((str(path),), settings) for path in paths]
    results = (
        map(_check_worker, arguments)
        if executor is None
        else executor.map(_check_worker, arguments, streams=2)
    )
    flagged = [
        (index, coded) for index, coded in enumerate(_typed_results(results)) if coded is not None
    ]
    # In the calling process, its size and two checks per object were opened before the look.
    refused = _second_look(
        ((index, (paths[index],), coded) for index, coded in flagged),
        settings,
        executor,
        3 * len(paths),
    )
    if refused is not None:
        index, cause = refused
        raise _model_capacity_error(f"The object {labels[index]!r}", settings, cause=cause)


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
    more, paired with itself under ``config.pairing``, for ``ncd_self_largest``.

    Each pair is compressed as one stream joined by ``config.pairing`` [adr-0026], the object
    whose SHA-256 is lower first, so the matrix is a function of the objects and not of their
    order in the manifest: reordered, it comes back with its rows and columns reordered alike
    and no value changed [adr-0024].

    Pairs are computed shard by shard straight into the memory map, and each finished shard
    is checkpointed with a digest, so an interrupted run resumes from the shards already
    done. With ``config.resume`` disabled, existing outputs are a conflict instead of a
    starting point. With more than one worker, or one whose PPMd stream residue outgrows the
    working-memory limit [adr-0027], the per-object compressed sizes and the shards run in
    spawned processes that re-import the caller's ``__main__``, so a script-level call must
    sit under ``if __name__ == "__main__":``.

    Parameters
    ----------
    progress
        Called once before work begins -- so a resumed run reports the pairs it inherited --
        and again after each shard is measured, as ``(measured, total, "distance")``; a shard
        holding a pair that waits for the PPMd second look counts as measured before the look.

    Raises
    ------
    DistanceError
        The normalization manifest or an object it names is invalid
        (``artifact_validation_error``); a checkpoint or an existing ``distance.npy`` is
        incompatible, corrupt, or disagrees with the matrix (``checkpoint_mismatch_error``);
        outputs exist but resume is disabled (``output_directory_conflict_error``); the
        compressor failed (``compression_error``); a worker died or returned an invalid shard
        (``distance_computation_error``); the finished matrix breaks an invariant
        (``distance_matrix_validation_error``); under the PPMd model check, an object or a
        pair restarts the model, or fills more than half of it with no second look possible
        (``model_capacity_error``) [adr-0025].
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
    # Workers are replaced before the PPMd stream residue they accumulate outgrows the
    # budget, and a single worker leaves the calling process only when it would [adr-0027].
    _, budget, pooled = settings.stream_residue(len(paths), len(paths) * (len(paths) - 1) // 2)
    executor = _WorkerPool(settings.effective_workers, budget) if pooled else None
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
            map(_size_worker, doubled)
            if executor is None
            else executor.map(_size_worker, doubled, streams=1)
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
            # Taken as they finish, since shards cost as much as their objects are large and a
            # slow one must not stop the rest; nothing below depends on the order (issue #141).
            else _bounded_submit(
                executor,
                _worker,
                arguments,
                settings.effective_workers * 2,
                # Each pair opens one stream, and a second under the model check.
                lambda shard: len(shard[1]) * (1 if _model_check(settings) is None else 2),
                ordered=False,
            )
        )

        def complete(
            written: np.memmap[Any, Any], shard_index: int, shard: list[tuple[int, int]]
        ) -> None:
            completed_shards[shard_index] = _shard_digest(written, shard)
            if lifted:
                unchecked.add(shard_index)
            logger.info("shard_completed", extra={"shard": shard_index, "pairs": len(shard)})
            checkpoint = DistanceShardsCheckpoint(
                identity=identity,
                pair_count=pair_count,
                shard_count=shard_count,
                completed=tuple(sorted(completed_shards)),
                digests={str(index): completed_shards[index] for index in sorted(completed_shards)},
                unchecked=tuple(sorted(unchecked)),
            )
            _atomic_json(shard_checkpoint, checkpoint.model_dump(mode="json"))

        # Shards holding a pair half the model coded otherwise wait, written but not completed,
        # for the second look once no pair is in flight: interrupted before it, they are
        # computed again rather than resumed unchecked. Their pairs wait on disk, so the
        # coordinator holds only where each waiting shard's pairs start and how many there are
        # [adr-0025]. They are read back shard by shard in index order, not in the order shards
        # finished, so the pair a refusal names does not depend on which shard was slower.
        waiting: dict[int, tuple[int, int]] = {}
        with tempfile.TemporaryFile(dir=checkpoints) as flagged_file:
            for shard_index, left, right, values, flagged in _typed_results(results):
                if len(left) != len(values) or not all(math.isfinite(value) for value in values):
                    raise DistanceError(
                        "Worker returned an invalid shard",
                        code="distance_computation_error",
                    )
                for i, j, value in zip(left, right, values, strict=True):
                    matrix[i, j] = value
                    matrix[j, i] = value
                matrix.flush()
                shard = list(zip(left, right, strict=True))
                _validate_completed_shard(matrix, shard)
                if flagged:
                    waiting[shard_index] = (flagged_file.tell(), len(flagged))
                    for i, j, coded in flagged:
                        flagged_file.write(_FLAGGED_RECORD.pack(i, j, coded))
                else:
                    complete(matrix, shard_index, shard)
                completed_pairs += len(values)
                if progress is not None:
                    progress(completed_pairs, pair_count, "distance")
            (doubled_size,) = _typed_results(self_size)
            if waiting:

                def waiting_pairs() -> Iterator[Look]:
                    for shard_index in sorted(waiting):
                        start, count = waiting[shard_index]
                        for record in range(count):
                            flagged_file.seek(start + record * _FLAGGED_RECORD.size)
                            i, j, coded = _FLAGGED_RECORD.unpack(
                                flagged_file.read(_FLAGGED_RECORD.size)
                            )
                            first, second = (i, j) if digests[i] <= digests[j] else (j, i)
                            yield i * len(paths) + j, (paths[first], paths[second]), coded

                # In the calling process: every object's size, checks and at most one look,
                # two checks per pair and the largest object's self-pair, at most.
                opened = 4 * len(paths) + 2 * pair_count + 1
                refused = _second_look(waiting_pairs(), settings, executor, opened)
                if refused is not None:
                    tag, cause = refused
                    i, j = divmod(tag, len(paths))
                    raise _model_capacity_error(
                        f"The pair {labels[i]!r} and {labels[j]!r}", settings, cause=cause
                    )
                for shard_index, shard in iter_pair_shards(len(paths), pairs_per_shard):
                    if shard_index in waiting:
                        complete(matrix, shard_index, shard)
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
