"""Cross-stage artifact contracts and verification invariants."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest
from damicore import (
    ArtifactValidationError,
    CheckpointMismatchError,
    DamicoreError,
    ExecutionConfig,
    ResourceLimits,
    api,
    run,
)
from damicore.pipeline import (
    RESUME_FINGERPRINT_KEYS,
    resume_fingerprint,
    runtime_fingerprint,
)
from damicore_clusterizer import ClusterConfig
from damicore_distance import DistanceConfig
from damicore_normalizer import NormalizationConfig
from damicore_tree_builder import TreeBuildConfig
from openpyxl import Workbook
from pydantic import BaseModel, ValidationError
from synthetic_data import generate_csv

pytestmark = pytest.mark.contract

SERIAL = ExecutionConfig(workers=1)


def _completed_run(directory: Path) -> Path:
    source = generate_csv(directory / "dataset.csv", rows=24, columns=8, clusters=2, seed=42)
    run_dir = directory / "run"
    result = run(
        source,
        output_dir=run_dir,
        keep_normalized=True,
        progress=False,
        execution=SERIAL,
    )
    result.close()
    return run_dir


def test_labels_agree_across_producer_and_consumers(tmp_path: Path) -> None:
    run_dir = _completed_run(tmp_path)
    normalization = json.loads((run_dir / "normalization/manifest.json").read_text("utf-8"))
    labels = json.loads((run_dir / "labels.json").read_text("utf-8"))
    tree = json.loads((run_dir / "tree.json").read_text("utf-8"))
    clusters = json.loads((run_dir / "clusters.json").read_text("utf-8"))

    normalized_ids = [item["object_id"] for item in normalization["objects"]]
    leaf_ids = [node["id"] for node in tree["nodes"] if node["kind"] == "leaf"]
    clustered_ids = [oid for group in clusters["clusters"] for oid in group["object_ids"]]

    assert list(labels["object_ids"]) == normalized_ids
    assert set(leaf_ids) == set(normalized_ids)
    assert set(clustered_ids) == set(normalized_ids)


def test_cross_verification_rejects_inconsistent_membership(tmp_path: Path) -> None:
    run_dir = _completed_run(tmp_path)
    normalization = api._load_normalization(run_dir / "normalization/manifest.json")
    membership_path = run_dir / "membership.csv"
    with membership_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["cluster"] = "999"  # assignment no longer matches clusters.json
    with membership_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["object_id", "label", "cluster"])
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ArtifactValidationError):
        api._verify_cross_artifacts(run_dir, normalization, None, 0)


def test_resume_fingerprint_projects_only_the_compat_subset() -> None:
    full = runtime_fingerprint()
    assert set(resume_fingerprint(full)) == set(RESUME_FINGERPRINT_KEYS)
    assert "platform" not in resume_fingerprint(full)

    environment_noise = {**full, "platform": "other", "pandas": "0.0", "tqdm": "0.0"}
    assert resume_fingerprint(environment_noise) == resume_fingerprint(full)

    incompatible = {**full, "numpy": "0.0.0"}
    assert resume_fingerprint(incompatible) != resume_fingerprint(full)

    # The codec that measured the checkpointed sizes, like zlib, is part of resumability.
    another_codec = {**full, "pyppmd": "0.0.0"}
    assert resume_fingerprint(another_codec) != resume_fingerprint(full)


def test_incomplete_run_resumes_despite_environment_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = generate_csv(tmp_path / "dataset.csv", rows=24, columns=8, clusters=2, seed=42)
    run_dir = tmp_path / "run"

    def fail_cluster(*_args: object, **_kwargs: object) -> object:
        raise api.ClusterizerError("injected", code="clusterization_error")

    monkeypatch.setattr(api, "cluster_tree", fail_cluster)
    with pytest.raises(api.ClusterizationError):
        run(source, output_dir=run_dir, progress=False, execution=SERIAL)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["runtime"]["platform"] = "a-completely-different-platform"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.undo()
    resumed = run(source, output_dir=run_dir, progress=False, execution=SERIAL)
    try:
        assert resumed.report.status == "completed"
    finally:
        resumed.close()


def test_incomplete_run_rejects_incompatible_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = generate_csv(tmp_path / "dataset.csv", rows=24, columns=8, clusters=2, seed=42)
    run_dir = tmp_path / "run"

    def fail_cluster(*_args: object, **_kwargs: object) -> object:
        raise api.ClusterizerError("injected", code="clusterization_error")

    monkeypatch.setattr(api, "cluster_tree", fail_cluster)
    with pytest.raises(api.ClusterizationError):
        run(source, output_dir=run_dir, progress=False, execution=SERIAL)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["runtime"]["numpy"] = "0.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.undo()
    with pytest.raises(CheckpointMismatchError):
        run(source, output_dir=run_dir, progress=False, execution=SERIAL)


# Every public configuration model, so the rule is asserted once over all of them instead of
# being restated per package, and a model exported later is covered the moment it appears here.
PUBLIC_CONFIGS = [
    pytest.param(ExecutionConfig, id="execution"),
    pytest.param(ResourceLimits, id="resource-limits"),
    pytest.param(NormalizationConfig, id="normalization"),
    pytest.param(DistanceConfig, id="distance"),
    pytest.param(TreeBuildConfig, id="tree-build"),
    pytest.param(ClusterConfig, id="cluster"),
]


@pytest.mark.parametrize("model", PUBLIC_CONFIGS)
def test_a_misspelled_configuration_field_is_rejected(model: type[BaseModel]) -> None:
    """A dropped keyword is worse than a rejected one: the run silently uses the default, and
    the artifacts it writes look valid while answering a different question."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model(definitely_not_a_field=1)


# The six data artifacts. manifest.json and report.json are excluded on purpose: they carry
# timestamps, stage timings and peak RSS, so they cannot be byte-identical across two runs.
DATA_ARTIFACTS = (
    "distance.npy",
    "labels.json",
    "tree.json",
    "tree.nwk",
    "membership.csv",
    "clusters.json",
)


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param("build_tree", id="fail-before-tree"),
        pytest.param("cluster_tree", id="fail-before-clustering"),
    ],
)
def test_a_resumed_run_publishes_the_same_bytes_as_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Resume is the most expensive guarantee in the pipeline, and its whole value rests on one
    equivalence: finishing an interrupted run lands exactly where an uninterrupted one would
    have. The distance package asserts that for its own matrix; nothing asserted it over the
    artifacts a user actually reads.
    """
    source = generate_csv(tmp_path / "dataset.csv", rows=24, columns=8, clusters=2, seed=42)

    fresh_dir = tmp_path / "fresh"
    fresh = run(source, output_dir=fresh_dir, progress=False, execution=SERIAL)
    fresh.close()

    def interrupted(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected interruption")

    resumed_dir = tmp_path / "resumed"
    monkeypatch.setattr(api, stage, interrupted)
    # The pipeline translates any stage failure into its public error, so that is what an
    # interruption looks like from here.
    with pytest.raises(DamicoreError, match="injected"):
        run(source, output_dir=resumed_dir, progress=False, execution=SERIAL)
    monkeypatch.undo()

    resumed = run(source, output_dir=resumed_dir, progress=False, execution=SERIAL)
    try:
        assert resumed.report.status == "completed"
    finally:
        resumed.close()

    for name in DATA_ARTIFACTS:
        assert (resumed_dir / name).read_bytes() == (fresh_dir / name).read_bytes(), name


def test_the_reported_ncd_range_agrees_with_the_persisted_matrix(
    tmp_path: Path,
) -> None:
    """These three fields were asserted only by name, in the list of report fields, so a wrong
    value was indistinguishable from a right one.

    The comparison is over the whole matrix because that is what the implementation reports.
    Note the consequence: the diagonal is exactly zero by contract, so ncd_min reads 0.0 on
    every run where no pair is negative, and the informative signal is ncd_out_of_range_count.
    """
    run_dir = _completed_run(tmp_path)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    matrix = np.load(run_dir / "distance.npy", allow_pickle=False)

    assert report["ncd_min"] == pytest.approx(float(matrix.min()))
    assert report["ncd_max"] == pytest.approx(float(matrix.max()))
    assert report["ncd_out_of_range_count"] == int(((matrix < 0.0) | (matrix > 1.0)).sum())
    # The max is a real measurement rather than the diagonal: NCD above 1 is legal and this
    # fixture stays under it, so the maximum has to come from an off-diagonal pair.
    assert report["ncd_max"] > 0.0


# Every source, so the guarantees below are asserted over the axis rather than over the one
# source that happened to exist first. A source added later fails here until it is listed.
def _corpus_fixture(root: Path) -> Path:
    root.mkdir(parents=True)
    shared = b"the quick brown fox jumps over the lazy dog\n" * 10
    (root / "a.bin").write_bytes(shared)
    (root / "b.bin").write_bytes(shared + b"tail\n")
    (root / "c.bin").write_bytes(bytes(range(256)) * 5)
    (root / "d.bin").write_bytes(bytes(range(256)) * 5 + b"\x00")
    return root


def _workbook_fixture(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["alpha", "beta", "gamma"])
    for row in range(1, 6):
        sheet.append(
            [
                "shared preamble " * row,
                f"beta {row}" * (row + 1),
                f"unrelated {row * 7}",
            ]
        )
    workbook.save(path)
    return path


SOURCES = [
    pytest.param("delimited", id="delimited"),
    pytest.param("xlsx", id="xlsx"),
    pytest.param("files", id="files"),
]


def _source(kind: str, directory: Path) -> Path:
    if kind == "files":
        return _corpus_fixture(directory / "corpus")
    if kind == "xlsx":
        return _workbook_fixture(directory / "dataset.xlsx")
    return generate_csv(directory / "dataset.csv", rows=24, columns=6, clusters=2, seed=42)


@pytest.mark.parametrize("kind", SOURCES)
def test_labels_agree_across_producer_and_consumers_for_every_source(
    tmp_path: Path, kind: str
) -> None:
    """The normalization manifest names the objects; every later artifact must name the same
    ones. This held for delimited input before the source axis existed, and the point of
    parametrizing it is that a new source cannot quietly break the chain."""
    source = _source(kind, tmp_path)
    run_dir = tmp_path / "run"
    result = run(
        source,
        source_kind=kind,
        output_dir=run_dir,
        keep_normalized=True,
        progress=False,
        execution=SERIAL,
    )
    result.close()

    normalization = json.loads((run_dir / "normalization/manifest.json").read_text("utf-8"))
    labels = json.loads((run_dir / "labels.json").read_text("utf-8"))
    tree = json.loads((run_dir / "tree.json").read_text("utf-8"))
    clusters = json.loads((run_dir / "clusters.json").read_text("utf-8"))

    normalized_ids = [item["object_id"] for item in normalization["objects"]]
    leaf_ids = [node["id"] for node in tree["nodes"] if node["kind"] == "leaf"]
    clustered_ids = [oid for group in clusters["clusters"] for oid in group["object_ids"]]

    assert list(labels["object_ids"]) == normalized_ids
    assert set(leaf_ids) == set(normalized_ids)
    assert set(clustered_ids) == set(normalized_ids)
    assert normalization["schema_version"] == 2
    assert normalization["input"]["kind"] == kind


@pytest.mark.parametrize("kind", SOURCES)
def test_a_resumed_run_publishes_the_same_bytes_for_every_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Resume's whole value is that finishing an interrupted run lands exactly where an
    uninterrupted one would have. A source that copied files in, or read a workbook twice,
    could break that without any other assertion noticing."""
    source = _source(kind, tmp_path)

    fresh_dir = tmp_path / "fresh"
    fresh = run(source, source_kind=kind, output_dir=fresh_dir, progress=False, execution=SERIAL)
    fresh.close()

    def interrupted(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected interruption")

    resumed_dir = tmp_path / "resumed"
    monkeypatch.setattr(api, "cluster_tree", interrupted)
    with pytest.raises(DamicoreError, match="injected"):
        run(
            source,
            source_kind=kind,
            output_dir=resumed_dir,
            progress=False,
            execution=SERIAL,
        )
    monkeypatch.undo()

    resumed = run(
        source,
        source_kind=kind,
        output_dir=resumed_dir,
        progress=False,
        execution=SERIAL,
    )
    try:
        assert resumed.report.status == "completed"
    finally:
        resumed.close()

    for name in DATA_ARTIFACTS:
        assert (resumed_dir / name).read_bytes() == (fresh_dir / name).read_bytes(), name
