"""Guards that only a partially finished run, a tampered inventory, or a stage receipt that
disagrees with the bytes on disk can reach: the resume decision table, the cross-artifact
verification, and the manifest totality rules.
"""

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from damicore_clusterizer import ClusterConfig, ClusterizerError, ClusterResult
from damicore_distance import DistanceConfig, DistanceError, DistanceResult
from damicore_distance.api import ProgressCallback
from damicore_normalizer import NormalizationConfig, NormalizationResult, NormalizerError
from damicore_normalizer.config import ObjectSource
from pydantic import ValidationError

import damicore.api as api
from damicore import (
    ArtifactValidationError,
    CheckpointMismatchError,
    ExecutionConfig,
    InputValidationError,
    OutputDirectoryConflictError,
    ResourceEstimate,
    load_result,
    run,
)
from damicore.manifest import RunManifest, artifact_record

pytestmark = pytest.mark.unit


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "input.csv"
    path.write_text("a,b,c\naaaa,aaab,zzzz\naaaa,aaac,zzzy\n", encoding="utf-8")
    return path


def _single_worker() -> ExecutionConfig:
    return ExecutionConfig(workers=1)


# Decoded JSON carries no static shape, and these tests deliberately write payloads the
# models would reject, so the run records are read here as raw JSON rather than as models.
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _reindex(run_dir: Path, name: str) -> None:
    """Restate one inventory record from the bytes now on disk.

    load_result checks the inventory hash before every later guard, so a test that edits an
    artifact without restating its record would only ever exercise the hash check.
    """
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["artifacts"][name] = artifact_record(run_dir / name, run_dir)
    _write_json(manifest_path, manifest)


def _fail_clusterizing(
    tree_path: str | Path,
    output_dir: str | Path,
    *,
    config: ClusterConfig | None = None,
) -> ClusterResult:
    raise ClusterizerError("injected clusterizer failure", code="clusterization_error")


def _incomplete_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: Path) -> Path:
    """Leave a run that stopped at clusterizing, which is the state resume is defined over."""
    source = _csv(tmp_path)
    monkeypatch.setattr(api, "cluster_tree", _fail_clusterizing)
    with pytest.raises(api.ClusterizationError, match="injected clusterizer failure"):
        run(source, output_dir=output, progress=False, execution=_single_worker())
    monkeypatch.undo()
    return source


def test_a_malformed_final_artifact_fails_schema_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification reads the artifacts back from disk rather than trusting the values the
    clusterizer returned, so an artifact whose schema is wrong must be caught even when its
    receipt hash matches the bytes that were written."""
    original = api.cluster_tree

    def cluster_then_corrupt(
        tree_path: str | Path,
        output_dir: str | Path,
        *,
        config: ClusterConfig | None = None,
    ) -> ClusterResult:
        clustered = original(tree_path, output_dir, config=config)
        clustered.clusters_path.write_text(
            json.dumps({"schema_version": 2, "clusters": []}), encoding="utf-8"
        )
        return clustered

    monkeypatch.setattr(api, "cluster_tree", cluster_then_corrupt)
    output = tmp_path / "run"
    with pytest.raises(ArtifactValidationError, match="Final artifact schema validation failed"):
        run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())


def test_cross_verification_reports_which_check_failed(tmp_path: Path) -> None:
    """The checks mapping is the contract: a caller has to be able to tell an object-order
    mismatch from a matrix-shape mismatch without re-deriving it from the message."""
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        keep_normalized=True,
        execution=_single_worker(),
    )
    try:
        communities = result.report.community_count
        assert communities is not None
        normalization = api._load_normalization(output / "normalization" / "manifest.json")
        reordered = normalization.model_copy(
            update={"objects": tuple(reversed(normalization.objects))}
        )
        with pytest.raises(
            ArtifactValidationError, match="Cross-artifact verification failed"
        ) as raised:
            api._verify_cross_artifacts(output, reordered, None, communities)
    finally:
        result.close()
    checks = raised.value.context["checks"]
    assert isinstance(checks, dict)
    # Only the normalization-to-labels link consults the normalization result; the remaining
    # checks compare artifacts against each other and must stay true, which is exactly what
    # makes the mapping useful for locating the disagreement.
    assert checks["normalization_to_labels"] is False
    assert checks["membership"] is True
    assert checks["matrix_shape"] is True
    assert checks["tree_leaves"] is True
    assert checks["cluster_ids"] is True
    assert checks["requested_communities"] is True


def test_a_directory_holding_another_configuration_is_refused(tmp_path: Path) -> None:
    """Only the input hash and the config hash decide whether a directory belongs to this run,
    so a changed compression level must be a conflict and never a silent resume."""
    source = _csv(tmp_path)
    output = tmp_path / "run"
    first = run(
        source,
        output_dir=output,
        compression_level=6,
        progress=False,
        execution=_single_worker(),
    )
    first.close()
    with pytest.raises(OutputDirectoryConflictError, match="belongs to another run"):
        run(
            source,
            output_dir=output,
            compression_level=9,
            progress=False,
            execution=_single_worker(),
        )


def test_an_incomplete_run_is_refused_when_resume_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume=False must refuse the directory rather than restart into it, because restarting
    would overwrite artifacts the caller asked not to touch."""
    output = tmp_path / "run"
    source = _incomplete_run(tmp_path, monkeypatch, output)
    with pytest.raises(OutputDirectoryConflictError, match="Incomplete output resume is disabled"):
        run(
            source,
            output_dir=output,
            progress=False,
            execution=ExecutionConfig(workers=1, resume=False),
        )


def test_an_incomplete_run_from_another_runtime_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume reuses bytes produced by libraries whose behaviour may have changed, so the
    recorded runtime fingerprint has to gate it before any stage receipt is consulted."""
    output = tmp_path / "run"
    source = _incomplete_run(tmp_path, monkeypatch, output)
    manifest_path = output / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["runtime"]["python"] = "0.0.0"
    _write_json(manifest_path, manifest)
    with pytest.raises(CheckpointMismatchError, match="different runtime fingerprint"):
        run(source, output_dir=output, progress=False, execution=_single_worker())


def test_a_partial_normalization_directory_is_rebuilt_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normalization that did not reach its receipt leaves objects nobody vouched for, so
    resume must delete the whole directory instead of normalizing on top of it."""
    source = _csv(tmp_path)
    original = api.materialize_objects

    def normalize_then_fail(
        source: str | Path | Sequence[str | Path],
        output_dir: str | Path,
        *,
        config: NormalizationConfig | None = None,
    ) -> NormalizationResult:
        original(source, output_dir, config=config)
        raise NormalizerError("injected failure after normalization", code="normalization_error")

    monkeypatch.setattr(api, "materialize_objects", normalize_then_fail)
    output = tmp_path / "run"
    # keep_normalized is part of the config hash, so both attempts must pass the same value
    # or the second would be refused as a different run before resume is ever considered.
    with pytest.raises(api.NormalizationError, match="injected failure after normalization"):
        run(
            source,
            output_dir=output,
            progress=False,
            keep_normalized=True,
            execution=_single_worker(),
        )
    monkeypatch.undo()

    stray = output / "normalization" / "stray.tmp"
    stray.write_text("left behind by the interrupted normalization", encoding="utf-8")
    result = run(
        source,
        output_dir=output,
        progress=False,
        keep_normalized=True,
        execution=_single_worker(),
    )
    try:
        assert result.report.status == "completed"
        assert not stray.exists()
    finally:
        result.close()


def test_normalization_bytes_differing_from_preflight_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight sizes every later decision, so normalization producing a different byte count
    means the run was planned for an input it did not read; it is reported as input drift."""
    real_preflight = api.preflight

    def drifting_preflight(
        source: str | Path | Sequence[str | Path],
        *,
        object_source: ObjectSource,
        save_diagnostics: bool,
        execution: ExecutionConfig,
        disk_target: Path,
    ) -> ResourceEstimate:
        preview = real_preflight(
            source,
            object_source=object_source,
            save_diagnostics=save_diagnostics,
            execution=execution,
            disk_target=disk_target,
        )
        return preview.model_copy(update={"normalized_bytes": preview.normalized_bytes + 1})

    monkeypatch.setattr(api, "preflight", drifting_preflight)
    output = tmp_path / "run"
    with pytest.raises(InputValidationError, match="byte counts differ") as raised:
        run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    assert raised.value.code == "input_drift"
    report = _read_json(output / "report.json")
    assert report["status"] == "failed"
    assert report["failed_stage"] == "normalizing"
    assert report["error"] == {
        "error_type": "InputValidationError",
        "code": "input_drift",
        "error_message": "Preflight and normalization byte counts differ",
    }


def test_a_stale_tree_is_deleted_when_distancing_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree describes one distance matrix. If the matrix is recomputed, whatever the previous
    attempt left from tree building must go first, or a run that fails again would leave a tree
    and a workspace belonging to bytes that no longer exist."""
    output = tmp_path / "run"
    source = _incomplete_run(tmp_path, monkeypatch, output)

    manifest_path = output / "manifest.json"
    manifest = _read_json(manifest_path)
    for stage in ("distancing", "tree_building"):
        manifest["stages"][stage]["status"] = "running"
        manifest["stages"][stage]["finished_at"] = None
    _write_json(manifest_path, manifest)
    workspace = output / "tree-work.npy"
    workspace.write_bytes(b"left behind by the previous tree build")

    result = run(source, output_dir=output, progress=False, execution=_single_worker())
    try:
        assert result.report.status == "completed"
        assert not workspace.exists()
    finally:
        result.close()


def test_a_completed_clusterizing_receipt_is_reused_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When only verification failed, every stage receipt is still valid; resume must rebuild
    the cluster result from the receipt rather than pay for clustering a second time."""
    source = _csv(tmp_path)
    output = tmp_path / "run"

    def refusing_verification(
        run_dir: Path,
        normalization: NormalizationResult,
        requested_communities: int | None,
        actual_communities: int,
    ) -> dict[str, bool]:
        raise ArtifactValidationError("injected verification failure")

    monkeypatch.setattr(api, "_verify_cross_artifacts", refusing_verification)
    with pytest.raises(ArtifactValidationError, match="injected verification failure"):
        run(source, output_dir=output, progress=False, execution=_single_worker())
    monkeypatch.undo()

    metrics = _read_json(output / "manifest.json")["stages"]["clusterizing"]["metrics"]

    result = run(source, output_dir=output, progress=False, execution=_single_worker())
    try:
        assert result.report.status == "completed"
        # The reused values come from the receipt verbatim; a stage that actually ran again
        # would time itself again and never reproduce the recorded seconds exactly.
        assert result.report.timings_seconds["clusterizing"] == metrics["seconds"]
        assert result.report.community_count == metrics["community_count"]
        assert result.report.cluster_count == metrics["cluster_count"]
        assert result.report.modularity == metrics["modularity"]
    finally:
        result.close()


def test_a_stale_membership_is_deleted_when_clusterizing_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clustering that wrote its files and then failed leaves membership.csv and clusters.json
    without a receipt. A later attempt must remove them before it starts, so a run that fails
    again cannot leave unvouched artifacts sitting next to the manifest."""
    source = _csv(tmp_path)
    output = tmp_path / "run"
    original = api.cluster_tree

    def cluster_then_fail(
        tree_path: str | Path,
        output_dir: str | Path,
        *,
        config: ClusterConfig | None = None,
    ) -> ClusterResult:
        original(tree_path, output_dir, config=config)
        raise ClusterizerError("injected failure after clustering", code="clusterization_error")

    monkeypatch.setattr(api, "cluster_tree", cluster_then_fail)
    with pytest.raises(api.ClusterizationError, match="injected failure after clustering"):
        run(source, output_dir=output, progress=False, execution=_single_worker())
    assert (output / "membership.csv").is_file()
    assert (output / "clusters.json").is_file()

    monkeypatch.setattr(api, "cluster_tree", _fail_clusterizing)
    with pytest.raises(api.ClusterizationError, match="injected clusterizer failure"):
        run(source, output_dir=output, progress=False, execution=_single_worker())
    assert not (output / "membership.csv").exists()
    assert not (output / "clusters.json").exists()


def test_a_normalization_receipt_of_an_unknown_schema_is_rejected_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume fingerprint deliberately ignores the stage packages, so a normalization
    manifest written by a different normalizer build reaches resume with a hash that matches
    its receipt and a schema this version cannot read. Loading it is the guard for that case."""
    source = _csv(tmp_path)
    output = tmp_path / "run"

    def failing_distance(
        normalization_manifest: str | Path,
        output_dir: str | Path,
        *,
        config: DistanceConfig | None = None,
        progress: ProgressCallback | None = None,
    ) -> DistanceResult:
        raise DistanceError("injected distance failure", code="distance_computation_error")

    monkeypatch.setattr(api, "compute_distance_matrix", failing_distance)
    with pytest.raises(api.DistanceComputationError, match="injected distance failure"):
        run(source, output_dir=output, progress=False, execution=_single_worker())
    monkeypatch.undo()

    normalization_manifest = output / "normalization" / "manifest.json"
    _write_json(normalization_manifest, {"schema_version": 99})
    manifest_path = output / "manifest.json"
    manifest = _read_json(manifest_path)
    receipt = manifest["stages"]["normalizing"]
    receipt["outputs"] = [
        artifact_record(normalization_manifest, output)
        if record["path"] == "normalization/manifest.json"
        else record
        for record in receipt["outputs"]
    ]
    _write_json(manifest_path, manifest)

    with pytest.raises(ArtifactValidationError, match="Normalization receipt is invalid"):
        run(source, output_dir=output, progress=False, execution=_single_worker())


def test_an_artifact_reached_through_a_symlinked_directory_is_rejected(tmp_path: Path) -> None:
    """Every inventory path is a plain relative path, but a symlinked directory inside the run
    can still make one resolve outside it. load_result must refuse to read such a file."""
    output = tmp_path / "run"
    result = run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    result.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "planted.bin").write_bytes(b"planted outside the run directory")
    os.symlink(outside, output / "linked")
    _reindex(output, "linked/planted.bin")
    with pytest.raises(ArtifactValidationError, match="escapes the run directory"):
        load_result(output)


def test_the_loader_names_the_schema_version_it_requires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to name the version the loader is actually holding out for.

    A message that spells the number out agrees with the constant only until one of them
    changes, and the one a reader checks by eye is the message. Moving the constant is also
    the only way to reach this branch through its version half at all, since the manifest
    model pins the same number and would reject a foreign one before this line runs.
    """
    output = tmp_path / "run"
    result = run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    result.close()

    monkeypatch.setattr(api, "SCHEMA_VERSION", 3)
    with pytest.raises(ArtifactValidationError, match="schema-v3"):
        load_result(output)


def test_an_artifact_symlinked_within_the_run_directory_is_read(tmp_path: Path) -> None:
    """What the loader enforces is containment, not file kind.

    An inventory entry is resolved before it is checked, so a link and the file it points at
    are the same path by the time any guard sees them: inside the run directory both are read,
    and both still have to match the size and digest the inventory recorded. The test above
    covers the case that matters -- resolving *outside* the run directory -- and this one pins
    the complement, so nothing here can grow a guard that cannot fire.
    """
    output = tmp_path / "run"
    result = run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    result.close()
    os.symlink(output / "report.json", output / "report-link.json")
    manifest_path = output / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["artifacts"]["report-link.json"] = artifact_record(output / "report-link.json", output)
    _write_json(manifest_path, manifest)

    loaded = load_result(output)
    loaded.close()


def test_a_report_that_is_not_completed_is_rejected(tmp_path: Path) -> None:
    """The manifest and the report both record the outcome. A result may only load when they
    agree, otherwise a failed run whose manifest was patched would load as a valid result."""
    output = tmp_path / "run"
    result = run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    result.close()
    report_path = output / "report.json"
    report = _read_json(report_path)
    report["status"] = "interrupted"
    _write_json(report_path, report)
    _reindex(output, "report.json")
    with pytest.raises(ArtifactValidationError, match="Result report is not completed"):
        load_result(output)


def test_membership_columns_in_another_order_are_rejected(tmp_path: Path) -> None:
    """membership.csv is returned to the caller as a DataFrame with a documented column order,
    so the order is part of the contract and not merely a presentation detail."""
    output = tmp_path / "run"
    result = run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    result.close()
    membership_path = output / "membership.csv"
    rows = membership_path.read_text(encoding="utf-8").splitlines()
    reordered = ["label,object_id,cluster"]
    for row in rows[1:]:
        object_id, label, cluster = row.split(",")
        reordered.append(f"{label},{object_id},{cluster}")
    membership_path.write_text("\n".join(reordered) + "\n", encoding="utf-8")
    _reindex(output, "membership.csv")
    with pytest.raises(ArtifactValidationError, match="membership.csv columns are invalid"):
        load_result(output)


def _completed_manifest(tmp_path: Path) -> Any:
    """Start from the manifest a real run wrote, so each case below differs from a valid
    manifest only in the field under test and cannot pass or fail for an unrelated reason."""
    output = tmp_path / "run"
    result = run(_csv(tmp_path), output_dir=output, progress=False, execution=_single_worker())
    result.close()
    return _read_json(output / "manifest.json")


def test_an_inventory_key_that_disagrees_with_its_record_is_rejected(tmp_path: Path) -> None:
    """The key and the record's path are two statements of the same fact. Allowing them to
    differ would let a lookup and a copy target two different files."""
    manifest = _completed_manifest(tmp_path)
    manifest["artifacts"]["renamed.nwk"] = manifest["artifacts"].pop("tree.nwk")
    with pytest.raises(ValidationError, match="artifact inventory keys must equal record paths"):
        RunManifest.model_validate_json(json.dumps(manifest))


def test_a_completed_manifest_without_a_completion_time_is_rejected(tmp_path: Path) -> None:
    """`completed` is a total claim: it promises the completion metadata a reader needs. A
    manifest that claims it without a completion time is not a completed run."""
    manifest = _completed_manifest(tmp_path)
    manifest["completed_at"] = None
    with pytest.raises(ValidationError, match="lacks completion metadata or objects"):
        RunManifest.model_validate_json(json.dumps(manifest))
