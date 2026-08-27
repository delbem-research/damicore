import hashlib
import json
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
import pytest
from damicore_normalizer import NormalizationConfig, NormalizationResult, normalize_csv

import damicore_distance.api as distance_api
from damicore_distance import (
    DistanceConfig,
    DistanceError,
    DistanceMatrixView,
    compute_distance_matrix,
)
from damicore_distance.ncd import normalized_compression_distance

pytestmark = pytest.mark.unit

# Mirrors damicore_distance.api._worker, which this suite wraps to inject a shard failure.
WorkerArguments = tuple[
    int, list[tuple[int, int]], list[str], list[int], Literal["zlib", "gzip"], int, int
]
WorkerResult = tuple[int, list[int], list[int], list[float]]


def _normalized(tmp_path: Path) -> NormalizationResult:
    source = tmp_path / "input.csv"
    source.write_text("a,b,c\naaaa,aaab,zzzz\naaaa,aaab,zzzy\n", encoding="utf-8")
    return normalize_csv(
        source,
        tmp_path / "normalized",
        config=NormalizationConfig(chunk_rows=1),
    )


# The manifest input block is this stage's whole contract with the previous one, and it is a
# union of three variants. This stage measures bytes and never looks past `kind`, which is
# exactly why a variant it cannot parse is a run it refuses outright rather than a wrong
# answer -- and why one row per variant belongs in this package's own suite instead of only
# in the aggregate's end-to-end runs.
@pytest.mark.parametrize(
    ("kind", "encoding", "block"),
    [
        pytest.param(
            "delimited",
            "json-lines/1",
            {"delimiter": ",", "encoding": "utf-8", "split": "columns"},
            id="delimited-dataset",
        ),
        pytest.param(
            "xlsx",
            "json-lines/1",
            {"sheet": "Sheet1", "split": "rows", "cell_text_rule": "v1"},
            id="spreadsheet-dataset",
        ),
        pytest.param("files", "raw-bytes/1", {}, id="file-corpus"),
    ],
)
def test_every_manifest_input_variant_is_accepted(
    tmp_path: Path, kind: str, encoding: str, block: dict[str, object]
) -> None:
    payloads = [b"alpha alpha alpha\n" * 3, b"beta beta beta\n" * 4]
    objects_dir = tmp_path / "normalized" / "objects"
    objects_dir.mkdir(parents=True)
    objects: list[dict[str, object]] = []
    for index, payload in enumerate(payloads, start=1):
        name = f"object_{index:06d}"
        (objects_dir / name).write_bytes(payload)
        objects.append(
            {
                "object_id": name,
                "label": f"label_{index}",
                "relative_path": f"objects/{name}",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    identity = {"kind": kind, "sha256": "a" * 64, "size_bytes": sum(map(len, payloads))}
    located: dict[str, object] = (
        {"root": str(tmp_path), "file_count": 2, "recursive": True, "include_hidden": False}
        if kind == "files"
        else {"path": str(tmp_path / "source")}
    )
    manifest_path = tmp_path / "normalized" / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "object_encoding": encoding,
                "input": {**identity, **located, **block},
                "objects": objects,
            }
        ),
        encoding="utf-8",
    )

    result = compute_distance_matrix(
        manifest_path, tmp_path / kind, config=DistanceConfig(workers=1)
    )
    assert result.object_count == 2


def test_ncd_is_not_clamped_and_zero_denominator_fails() -> None:
    assert normalized_compression_distance(10, 20, 50) == 2.0
    with pytest.raises(DistanceError, match="denominator"):
        normalized_compression_distance(0, 0, 1)


def test_serial_parallel_and_resumed_are_bitwise_equal(tmp_path: Path) -> None:
    normalized = _normalized(tmp_path)
    serial = compute_distance_matrix(
        normalized.manifest_path,
        tmp_path / "serial",
        config=DistanceConfig(workers=1, pairs_per_shard=1),
    )
    parallel = compute_distance_matrix(
        normalized.manifest_path,
        tmp_path / "parallel",
        config=DistanceConfig(workers=2, pairs_per_shard=1),
    )
    resumed = compute_distance_matrix(
        normalized.manifest_path,
        tmp_path / "serial",
        config=DistanceConfig(workers=1, pairs_per_shard=1),
    )
    serial_matrix = np.load(serial.matrix_path, allow_pickle=False)
    assert serial_matrix.dtype == np.float64
    assert np.array_equal(serial_matrix, np.load(parallel.matrix_path, allow_pickle=False))  # pyright: ignore[reportUnknownMemberType]
    assert np.array_equal(serial_matrix, np.load(resumed.matrix_path, allow_pickle=False))  # pyright: ignore[reportUnknownMemberType]


def test_diagnostics_and_corruption_detection(tmp_path: Path) -> None:
    normalized = _normalized(tmp_path)
    result = compute_distance_matrix(
        normalized.manifest_path,
        tmp_path / "run",
        config=DistanceConfig(workers=1, save_diagnostics=True),
    )
    assert result.pair_count == 3
    assert (tmp_path / "run/diagnostics/distance.csv").is_file()
    assert (tmp_path / "run/diagnostics/ncd-pairs.csv").is_file()

    manifest = json.loads(normalized.manifest_path.read_text(encoding="utf-8"))
    object_path = normalized.manifest_path.parent / manifest["objects"][0]["relative_path"]
    object_path.write_bytes(object_path.read_bytes() + b"corrupt")
    with pytest.raises(DistanceError, match="hash or size"):
        compute_distance_matrix(normalized.manifest_path, tmp_path / "corrupt")


def test_gzip_progress_view_and_resume_guards(tmp_path: Path) -> None:
    normalized = _normalized(tmp_path)
    calls: list[tuple[int, int, str]] = []
    result = compute_distance_matrix(
        normalized.manifest_path,
        tmp_path / "gzip",
        config=DistanceConfig(compressor="gzip", workers=1, pairs_per_shard=2),
        progress=lambda completed, total, message: calls.append((completed, total, message)),
    )
    assert calls[-1] == (3, 3, "distance")
    view = DistanceMatrixView(result.matrix_path, ["a", "b", "c"])
    assert view[0, 0] == 0.0
    assert view.to_pandas(force=True).shape == (3, 3)
    view.close()
    with pytest.raises(ValueError, match="reload"):
        view.head()
    with pytest.raises(DistanceError, match="resume is disabled"):
        compute_distance_matrix(
            normalized.manifest_path,
            tmp_path / "gzip",
            config=DistanceConfig(compressor="gzip", workers=1, resume=False),
        )


def test_manifest_and_checkpoint_corruption_fail(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    with pytest.raises(DistanceError, match="manifest"):
        compute_distance_matrix(bad, tmp_path / "bad-run")

    normalized = _normalized(tmp_path)
    output = tmp_path / "run"
    compute_distance_matrix(
        normalized.manifest_path,
        output,
        config=DistanceConfig(workers=1, pairs_per_shard=1),
    )
    checkpoint = output / "checkpoints/compressed-sizes.json"
    checkpoint.write_text("broken", encoding="utf-8")
    with pytest.raises(DistanceError, match="checkpoint"):
        compute_distance_matrix(
            normalized.manifest_path,
            output,
            config=DistanceConfig(workers=1, pairs_per_shard=1),
        )


def test_manifest_schema_rejects_extra_fields(tmp_path: Path) -> None:
    normalized = _normalized(tmp_path)
    manifest = json.loads(normalized.manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    normalized.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DistanceError, match="manifest"):
        compute_distance_matrix(normalized.manifest_path, tmp_path / "invalid-schema")


def test_resume_rejects_symmetric_finite_matrix_corruption(tmp_path: Path) -> None:
    normalized = _normalized(tmp_path)
    output = tmp_path / "run"
    result = compute_distance_matrix(
        normalized.manifest_path,
        output,
        config=DistanceConfig(workers=1, pairs_per_shard=1),
    )
    matrix = np.load(result.matrix_path, mmap_mode="r+", allow_pickle=False)
    matrix[0, 1] = matrix[1, 0] = float(matrix[0, 1]) + 0.125
    matrix.flush()
    del matrix
    with pytest.raises(DistanceError, match="digest"):
        compute_distance_matrix(
            normalized.manifest_path,
            output,
            config=DistanceConfig(workers=1, pairs_per_shard=1),
        )


@pytest.mark.parametrize("fail_after", [0, 1, 2])
def test_resume_after_each_shard_boundary_matches_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after: int
) -> None:
    normalized = _normalized(tmp_path)
    config = DistanceConfig(workers=1, pairs_per_shard=1)
    clean = compute_distance_matrix(normalized.manifest_path, tmp_path / "clean", config=config)
    original_worker = distance_api._worker
    calls = 0

    def fail_at_boundary(arguments: WorkerArguments) -> WorkerResult:
        nonlocal calls
        if calls == fail_after:
            raise RuntimeError("injected shard interruption")
        calls += 1
        return original_worker(arguments)

    monkeypatch.setattr(distance_api, "_worker", fail_at_boundary)
    interrupted = tmp_path / f"interrupted-{fail_after}"
    # A worker failure reaches the caller as a typed error carrying the original as its cause:
    # every public failure carries a stable code, whatever went wrong inside.
    with pytest.raises(DistanceError) as raised:
        compute_distance_matrix(normalized.manifest_path, interrupted, config=config)
    assert raised.value.code == "distance_computation_error"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "injected" in str(raised.value.__cause__)
    monkeypatch.setattr(distance_api, "_worker", original_worker)

    resumed = compute_distance_matrix(normalized.manifest_path, interrupted, config=config)
    assert np.array_equal(  # pyright: ignore[reportUnknownMemberType]
        np.load(clean.matrix_path, allow_pickle=False),
        np.load(resumed.matrix_path, allow_pickle=False),
    )


# Each row is one way a matrix can violate the contract the tree builder relies on.
INVALID_MATRICES = [
    pytest.param(np.zeros((2, 2), dtype=np.float32), "shape or dtype", id="wrong-dtype"),
    pytest.param(np.zeros((2, 3), dtype=np.float64), "shape or dtype", id="not-square"),
    pytest.param(np.zeros(2, dtype=np.float64), "shape or dtype", id="not-two-dimensional"),
    pytest.param(
        np.array([[0.0, np.nan], [np.nan, 0.0]], dtype=np.float64), "NaN", id="not-finite"
    ),
    pytest.param(
        np.array([[1.0, 2.0], [2.0, 0.0]], dtype=np.float64), "diagonal", id="nonzero-diagonal"
    ),
    pytest.param(
        np.array([[0.0, 2.0], [3.0, 0.0]], dtype=np.float64), "symmetric", id="asymmetric"
    ),
]


@pytest.mark.parametrize(("matrix", "discriminator"), INVALID_MATRICES)
def test_an_invalid_matrix_is_rejected(matrix: np.ndarray, discriminator: str) -> None:
    with pytest.raises(DistanceError, match=discriminator) as raised:
        distance_api._validated_matrix_statistics(matrix)
    assert raised.value.code == "distance_matrix_validation_error"


def test_workers_below_one_is_rejected_and_auto_resolves_to_a_positive_count() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DistanceConfig(workers=0)
    assert DistanceConfig(workers="auto").effective_workers >= 1
    assert DistanceConfig(workers=3).effective_workers == 3


def test_an_unreadable_object_is_reported_as_a_compression_error(tmp_path: Path) -> None:
    from damicore_distance.compressor import compressed_size

    with pytest.raises(DistanceError, match="Could not compress") as raised:
        compressed_size((tmp_path / "missing.jsonl",), compressor="zlib", level=6, chunk_bytes=1024)
    assert raised.value.code == "compression_error"


def test_the_artifact_schemas_reject_uncontained_paths_and_malformed_digests() -> None:
    """Both models are read back from disk, so their validators are the gate a tampered
    artifact has to pass."""
    from pydantic import ValidationError as PydanticValidationError

    from damicore_distance.artifacts import DistanceShardsCheckpoint, NormalizationObject

    with pytest.raises(PydanticValidationError, match="contained POSIX path"):
        NormalizationObject(
            object_id="o", label="l", relative_path="../escape.jsonl", size_bytes=0, sha256="0" * 64
        )
    with pytest.raises(PydanticValidationError, match="lowercase SHA-256"):
        DistanceShardsCheckpoint(
            identity={}, pair_count=1, shard_count=1, completed=(0,), digests={"0": "nope"}
        )


def test_the_matrix_view_exposes_shape_dtype_and_a_bounded_head(tmp_path: Path) -> None:
    """head() and the numpy passthroughs are the bounded ways to inspect a matrix that may be
    far larger than memory, so they must work without materializing it."""
    values = np.array([[0.0, 1.0, 2.0], [1.0, 0.0, 3.0], [2.0, 3.0, 0.0]], dtype=np.float64)
    path = tmp_path / "distance.npy"
    np.save(path, values, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    view = DistanceMatrixView(path, ["a", "b", "c"])

    assert view.shape == (3, 3)
    assert view.dtype == np.float64
    head = view.head(2)
    assert list(head.index) == ["a", "b"]
    assert head.to_numpy().tolist() == [[0.0, 1.0], [1.0, 0.0]]
    view.close()


def test_materializing_a_matrix_past_the_limit_is_refused_unless_forced(tmp_path: Path) -> None:
    """The limit is what stops a caller turning an out-of-core matrix into an in-memory
    DataFrame by accident; force is the explicit opt-in."""
    values = np.zeros((4, 4), dtype=np.float64)
    path = tmp_path / "distance.npy"
    np.save(path, values, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    view = DistanceMatrixView(
        path,
        ["a", "b", "c", "d"],
        materialization_limit_bytes=8,
        materialization_error=lambda message: DistanceError(message, code="materialization_error"),
    )
    with pytest.raises(DistanceError, match="use head"):
        view.to_pandas()
    assert view.to_pandas(force=True).shape == (4, 4)
    view.close()


# The schema already rejects a traversal or absolute relative_path, so these are the shapes
# that pass validation and still must not be read: a path resolving to nothing, and a symlink
# that could redirect the read outside the artifact root after validation.
@pytest.mark.parametrize("kind", ["missing", "symlink"])
def test_an_object_path_that_is_not_a_contained_regular_file_is_rejected(
    tmp_path: Path, kind: str
) -> None:
    normalized = _normalized(tmp_path)
    root = normalized.manifest_path.parent
    payload = json.loads(normalized.manifest_path.read_text(encoding="utf-8"))
    if kind == "missing":
        payload["objects"][0]["relative_path"] = "objects/absent.jsonl"
    else:
        outside = tmp_path / "outside.jsonl"
        outside.write_text("planted", encoding="utf-8")
        link = root / "objects" / "link.jsonl"
        link.symlink_to(outside)
        payload["objects"][0]["relative_path"] = "objects/link.jsonl"
    normalized.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DistanceError, match="escapes its artifact root") as raised:
        compute_distance_matrix(normalized.manifest_path, tmp_path / "out")
    assert raised.value.code == "artifact_validation_error"


def test_a_manifest_with_fewer_than_two_objects_is_rejected(tmp_path: Path) -> None:
    normalized = _normalized(tmp_path)
    payload = json.loads(normalized.manifest_path.read_text(encoding="utf-8"))
    payload["objects"] = payload["objects"][:1]
    normalized.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DistanceError, match="at least two unique") as raised:
        compute_distance_matrix(normalized.manifest_path, tmp_path / "out")
    assert raised.value.code == "artifact_validation_error"


def test_a_worker_returning_a_non_finite_shard_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker runs out of process, so its result is untrusted input to the parent."""
    normalized = _normalized(tmp_path)

    def broken_worker(arguments: WorkerArguments) -> WorkerResult:
        shard_index, pairs, *_ = arguments
        return (
            shard_index,
            [i for i, _ in pairs],
            [j for _, j in pairs],
            [float("nan")] * len(pairs),
        )

    monkeypatch.setattr(distance_api, "_worker", broken_worker)
    with pytest.raises(DistanceError, match="invalid shard") as raised:
        compute_distance_matrix(
            normalized.manifest_path, tmp_path / "out", config=DistanceConfig(workers=1)
        )
    assert raised.value.code == "distance_computation_error"


def test_a_failed_checkpoint_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    normalized = _normalized(tmp_path)
    output = tmp_path / "out"

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated failure while committing a checkpoint")

    monkeypatch.setattr(distance_api.os, "replace", failing_replace)
    # Discriminated: os.replace is patched process-wide, so a bare OSError could come from
    # numpy or tempfile without _atomic_json ever being reached.
    with pytest.raises(OSError, match="simulated failure while committing a checkpoint"):
        compute_distance_matrix(normalized.manifest_path, output, config=DistanceConfig(workers=1))
    assert not list((output / "checkpoints").glob(".*.json.*"))


def _completed_run(tmp_path: Path) -> tuple[Path, Path]:
    """One successful run, so the checkpoints under test exist and are internally consistent."""
    normalized = _normalized(tmp_path)
    output = tmp_path / "run"
    compute_distance_matrix(
        normalized.manifest_path,
        output,
        config=DistanceConfig(workers=1, pairs_per_shard=1),
    )
    return normalized.manifest_path, output


# Checkpoint payloads are plain JSON, hence the Any in the mutation signature.
CheckpointMutation = Callable[[dict[str, Any]], object]


def _rewrite(path: Path, mutate: CheckpointMutation) -> None:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


# A resumed run trusts what is already on disk, so every clause that decides "this checkpoint
# describes the same computation" is a place a silently wrong matrix could be accepted.
def test_a_compressed_size_checkpoint_from_another_run_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    _rewrite(
        output / "checkpoints" / "compressed-sizes.json",
        lambda payload: payload["identity"].update(object_ids=["other"]),
    )
    with pytest.raises(DistanceError, match="checkpoint is incompatible") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_a_truncated_compressed_size_checkpoint_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    _rewrite(
        output / "checkpoints" / "compressed-sizes.json",
        lambda payload: payload.update(sizes=payload["sizes"][:1]),
    )
    with pytest.raises(DistanceError, match="checkpoint is incomplete") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_an_existing_matrix_without_resume_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    (output / "checkpoints" / "compressed-sizes.json").unlink()
    with pytest.raises(DistanceError, match="distance.npy exists") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1, resume=False)
        )
    assert raised.value.code == "output_directory_conflict_error"


def test_an_existing_shard_checkpoint_without_resume_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    (output / "checkpoints" / "compressed-sizes.json").unlink()
    (output / "distance.npy").unlink()
    with pytest.raises(DistanceError, match="shard checkpoint exists") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1, resume=False)
        )
    assert raised.value.code == "output_directory_conflict_error"


def test_a_corrupt_existing_matrix_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    (output / "distance.npy").write_bytes(b"not a numpy file")
    with pytest.raises(DistanceError, match="corrupt") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_an_existing_matrix_of_the_wrong_shape_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    np.save(  # pyright: ignore[reportUnknownMemberType]
        output / "distance.npy", np.zeros((5, 5), dtype=np.float64), allow_pickle=False
    )
    with pytest.raises(DistanceError, match="incompatible") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_a_shard_checkpoint_describing_another_computation_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    _rewrite(
        output / "checkpoints" / "distance-shards.json",
        lambda payload: payload.update(pair_count=payload["pair_count"] + 1),
    )
    with pytest.raises(DistanceError, match="shard checkpoint is incompatible") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_a_shard_index_outside_the_shard_count_is_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    _rewrite(
        output / "checkpoints" / "distance-shards.json",
        lambda payload: payload.update(completed=[99], digests={"99": "a" * 64}),
    )
    with pytest.raises(DistanceError, match="Invalid completed shard index") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_shard_digests_that_do_not_match_the_completed_set_are_rejected(tmp_path: Path) -> None:
    manifest, output = _completed_run(tmp_path)
    _rewrite(
        output / "checkpoints" / "distance-shards.json",
        lambda payload: payload.update(completed=[0], digests={"2": "a" * 64}),
    )
    with pytest.raises(DistanceError, match="Invalid completed shard digests") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


def test_a_completed_shard_missing_from_the_matrix_is_rejected(tmp_path: Path) -> None:
    """The checkpoint says the shard is done; distance.npy is the only thing that can
    contradict it, so the two are cross-checked rather than trusted separately."""
    manifest, output = _completed_run(tmp_path)
    matrix = np.load(output / "distance.npy", mmap_mode="r+", allow_pickle=False)
    matrix[0, 1] = np.nan
    matrix[1, 0] = np.nan
    matrix.flush()
    del matrix
    with pytest.raises(DistanceError, match="missing or asymmetric") as raised:
        compute_distance_matrix(
            manifest, output, config=DistanceConfig(workers=1, pairs_per_shard=1)
        )
    assert raised.value.code == "checkpoint_mismatch_error"


@pytest.mark.parametrize("size", [0, -1, -100], ids=["zero", "negative-one", "large-negative"])
def test_a_non_positive_compressed_size_is_rejected(size: int) -> None:
    """A negative size makes max(cx, cy) negative, so ncd's zero-denominator guard does not
    fire and every pair in that row becomes a finite negative distance -- which the shape,
    finiteness, diagonal and symmetry checks all accept. The schema is the only gate."""
    from pydantic import ValidationError as PydanticValidationError

    from damicore_distance.artifacts import CompressedSizesCheckpoint

    with pytest.raises(PydanticValidationError, match="greater than 0"):
        CompressedSizesCheckpoint(identity={}, sizes=(size, 5))


def test_the_memory_map_close_releases_the_handle_numpy_actually_exposes(tmp_path: Path) -> None:
    """`close()` reaches a private numpy attribute, so the assumption is pinned here.

    Releasing the map deterministically is what the view promises, and the only handle numpy
    offers is `memmap._mmap`. The call site tolerates its absence so that `close()` never
    raises from a caller's `finally` -- but tolerating it silently would turn a renamed
    attribute into a leaked map behind a view that still reports itself closed. This test is
    what makes that a failure at the supported numpy range's edge instead.
    """
    path = tmp_path / "distance.npy"
    np.save(path, np.zeros((2, 2), dtype=np.float64), allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    view = DistanceMatrixView(path, ["a", "b"])
    try:
        handle = getattr(view._matrix, "_mmap", None)
        assert handle is not None, "numpy.memmap no longer exposes _mmap; close() cannot release"
        assert not handle.closed
    finally:
        view.close()
    assert handle.closed


def test_the_matrix_view_close_is_idempotent(tmp_path: Path) -> None:
    """close() runs in callers' finally blocks, so a second call must not raise."""
    path = tmp_path / "distance.npy"
    np.save(path, np.zeros((2, 2), dtype=np.float64), allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    view = DistanceMatrixView(path, ["a", "b"])
    view.close()
    view.close()
    with pytest.raises(ValueError, match="closed"):
        _ = view.shape


def test_shards_are_submitted_in_a_bounded_window() -> None:
    """The streaming invariant: work in flight is bounded by the pool, not by the pair count.

    Executor.map submits every shard before yielding anything, and each shard's arguments
    carry a copy of the object paths and sizes, so the queued payload would grow with the
    number of pairs. This asserts the window instead: nothing beyond `limit` is submitted
    until a result has been taken, and results still arrive in submission order.
    """
    submitted: list[int] = []

    class RecordingExecutor:
        def submit(
            self, function: object, argument: distance_api.WorkerArguments
        ) -> Future[distance_api.WorkerResult]:
            submitted.append(argument[0])
            future: Future[distance_api.WorkerResult] = Future()
            future.set_result((argument[0], [], [], []))
            return future

    total = 10
    limit = 3
    no_pairs: list[tuple[int, int]] = []
    no_paths: list[str] = []
    no_sizes: list[int] = []
    shards: list[distance_api.WorkerArguments] = [
        (index, no_pairs, no_paths, no_sizes, "zlib", 6, 1024) for index in range(total)
    ]
    arguments: Iterator[distance_api.WorkerArguments] = iter(shards)
    results = distance_api._bounded_submit(
        cast(ProcessPoolExecutor, RecordingExecutor()), arguments, limit
    )

    # Nothing is submitted until the first result is pulled, and then only enough to fill
    # the window.
    assert submitted == []
    first = next(results)
    assert first[0] == 0
    assert len(submitted) == limit

    consumed = [first[0]]
    for shard_index, *_ in results:
        # Each further result releases at most one more submission.
        assert len(submitted) - len(consumed) <= limit
        consumed.append(shard_index)

    assert consumed == list(range(total))
    assert submitted == list(range(total))


# Every public failure must carry a stable code. A worker dies as BrokenProcessPool
# with no cause attached, and any other worker exception arrives as whatever type it was.
def _raising(exc: BaseException) -> Iterator[distance_api.WorkerResult]:
    def generate() -> Iterator[distance_api.WorkerResult]:
        raise exc
        yield  # pragma: no cover - unreachable, makes this a generator

    return generate()


def test_a_dead_worker_pool_names_the_guard_and_the_serial_alternative() -> None:
    results = distance_api._typed_results(_raising(BrokenProcessPool("gone")))
    with pytest.raises(DistanceError) as raised:
        list(results)
    assert raised.value.code == "distance_computation_error"
    assert '`if __name__ == "__main__":`' in str(raised.value)
    assert "workers=1" in str(raised.value)


def test_an_unexpected_worker_exception_becomes_a_typed_error() -> None:
    results = distance_api._typed_results(_raising(MemoryError("shard too large")))
    with pytest.raises(DistanceError) as raised:
        list(results)
    assert raised.value.code == "distance_computation_error"
    assert isinstance(raised.value.__cause__, MemoryError)


def test_a_typed_worker_error_passes_through_unchanged() -> None:
    """A DistanceError raised inside a worker already carries its own code; relabelling it
    would replace a precise diagnosis, such as a compression failure, with a generic one."""
    original = DistanceError("Could not compress object", code="compression_error")
    results = distance_api._typed_results(_raising(original))
    with pytest.raises(DistanceError) as raised:
        list(results)
    assert raised.value is original


# Setting a sys.modules entry to None makes `import pandas` raise ImportError, which is how
# the absence of the optional extra is reproduced in an environment that has it installed.
@pytest.mark.parametrize("method_name", ["head", "to_pandas"])
def test_a_pandas_view_without_the_extra_names_the_extra_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method_name: str
) -> None:
    """Both methods stay on the public view while pandas stays optional, so their failure
    has to be the package's own typed error rather than ModuleNotFoundError."""
    path = tmp_path / "distance.npy"
    np.save(path, np.zeros((2, 2), dtype=np.float64), allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    view = DistanceMatrixView(path, ["a", "b"])
    monkeypatch.setitem(sys.modules, "pandas", None)
    try:
        with pytest.raises(DistanceError) as raised:
            getattr(view, method_name)()
        assert raised.value.code == "missing_dependency_error"
        assert "damicore-distance[pandas]" in str(raised.value)
        # The NumPy surface must keep working without the extra; only these two need it.
        assert view.shape == (2, 2)
    finally:
        view.close()
