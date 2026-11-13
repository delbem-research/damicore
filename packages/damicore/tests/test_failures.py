import json
from pathlib import Path

import pytest
from damicore_clusterizer import ClusterizerError, ClusterResult
from damicore_distance import DistanceError
from damicore_normalizer import NormalizerError
from damicore_tree_builder import TreeBuilderError

import damicore.api as api
from damicore import (
    ArtifactValidationError,
    ConfigurationError,
    ExecutionConfig,
    OutputDirectoryConflictError,
    ResourceEstimate,
    ResourceLimitError,
    ResourceLimits,
    estimate,
    load_result,
    run,
)
from damicore.errors import (
    CompressionError,
    DatasetFormatError,
    DistanceMatrixValidationError,
    InputValidationError,
    TreeFormatError,
)
from damicore.progress import distance_progress

pytestmark = pytest.mark.unit


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "input.csv"
    path.write_text("a,b,c\naa,ab,zz\naa,ac,zy\n", encoding="utf-8")
    return path


# Each row rejects one configuration option before the CSV is opened. Naming the option
# rather than splatting a dict keeps every call checked against estimate's signature.
@pytest.mark.parametrize(
    ("split", "delimiter", "encoding", "message"),
    [
        pytest.param("invalid", ",", "utf-8", "split", id="split"),
        pytest.param("columns", "::", "utf-8", "delimiter", id="delimiter"),
        pytest.param("columns", ",", "not-an-encoding", "encoding", id="encoding"),
    ],
)
def test_invalid_configuration_fails_before_csv_read(
    tmp_path: Path, split: str, delimiter: str, encoding: str, message: str
) -> None:
    missing = tmp_path / "missing.csv"
    with pytest.raises(ConfigurationError, match=message):
        estimate(missing, split=split, delimiter=delimiter, encoding=encoding)


def test_too_many_requested_clusters_is_rejected_before_any_stage_runs(tmp_path: Path) -> None:
    """The leaf bound belongs to the argument contract, so it must fail as configuration at
    preflight rather than as a clusterizer failure after normalization, the NCD matrix and
    the tree have already been computed."""
    output = tmp_path / "over-requested"
    with pytest.raises(ConfigurationError, match="num_clusters"):
        run(
            _csv(tmp_path),
            num_clusters=4,
            output_dir=output,
            progress=False,
            execution=ExecutionConfig(workers=1),
        )
    assert not output.exists()


def test_resource_limit_error_explains_the_object_count_distinction(tmp_path: Path) -> None:
    """The exception itself, not only the README, must state that CSV size and object count
    scale differently."""
    with pytest.raises(ResourceLimitError, match="max_objects") as raised:
        run(
            _csv(tmp_path),
            split="rows",
            output_dir=tmp_path / "rows-limited",
            progress=False,
            execution=ExecutionConfig(workers=1, limits=ResourceLimits(max_objects=1)),
        )
    message = str(raised.value)
    assert "quadratic" in message
    # The guidance must name what to reshape for every source, not only the one that tripped
    # the gate: a corpus has no split, so advice phrased purely in splits is unactionable for
    # it, and this assertion is what keeps the message spanning the source axis.
    assert "split='columns'" in message
    assert "split='rows'" in message
    assert "files source" in message
    carried = raised.value.context["estimate"]
    assert isinstance(carried, ResourceEstimate)
    assert carried.violations == ["max_objects"]


def test_run_rejects_limits_and_conflicting_directories(tmp_path: Path) -> None:
    source = _csv(tmp_path)
    with pytest.raises(ResourceLimitError):
        run(
            source,
            output_dir=tmp_path / "limited",
            progress=False,
            execution=ExecutionConfig(workers=1, limits=ResourceLimits(max_objects=2)),
        )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "user.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(OutputDirectoryConflictError):
        run(source, output_dir=occupied, progress=False, execution=ExecutionConfig(workers=1))
    assert (occupied / "user.txt").read_text(encoding="utf-8") == "keep"


def test_failed_stage_is_reported_and_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _csv(tmp_path)
    output = tmp_path / "resume"
    original = api.cluster_tree

    def fail_cluster(*_args: object, **_kwargs: object) -> ClusterResult:
        raise ClusterizerError("injected failure", code="clusterization_error")

    monkeypatch.setattr(api, "cluster_tree", fail_cluster)
    with pytest.raises(api.ClusterizationError):
        run(source, output_dir=output, progress=False, execution=ExecutionConfig(workers=1))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["failed_stage"] == "clusterizing"
    # The diagnostic report states the shard size the run used, not the model's default.
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert report["pairs_per_shard"] == manifest["config"]["pairs_per_shard"] == 1
    # A failure report carries no measurement it did not take from a finished stage.
    assert report["ncd_self_largest"] is None
    recorded = manifest["stages"]["distancing"]["metrics"]["ncd_self_largest"]

    monkeypatch.setattr(api, "cluster_tree", original)
    result = run(source, output_dir=output, progress=False, execution=ExecutionConfig(workers=1))
    assert result.report.status == "completed"
    # The resumed run reuses the distance stage, so the figure comes from its receipt.
    assert result.report.ncd_self_largest == recorded
    result.close()


def test_reuse_flags_and_loader_terminal_state(tmp_path: Path) -> None:
    source = _csv(tmp_path)
    output = tmp_path / "run"
    result = run(source, output_dir=output, progress=False, execution=ExecutionConfig(workers=1))
    with pytest.raises(OutputDirectoryConflictError, match="reuse"):
        run(
            source,
            output_dir=output,
            progress=False,
            execution=ExecutionConfig(workers=1, reuse_completed=False),
        )
    with pytest.raises(OutputDirectoryConflictError):
        result.save(tmp_path)
    result.close()

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="completed"):
        load_result(output)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NormalizerError("dataset", code="dataset_format_error"), DatasetFormatError),
        (NormalizerError("drift", code="input_drift"), InputValidationError),
        (NormalizerError("normal", code="normalization_error"), api.NormalizationError),
        (DistanceError("compress", code="compression_error"), CompressionError),
        (
            DistanceError("matrix", code="distance_matrix_validation_error"),
            DistanceMatrixValidationError,
        ),
        (DistanceError("distance"), api.DistanceComputationError),
        (TreeBuilderError("tree", code="tree_format_error"), TreeFormatError),
        (TreeBuilderError("build"), api.TreeBuildError),
        (
            TreeBuilderError("conflict", code="output_directory_conflict_error"),
            OutputDirectoryConflictError,
        ),
        (
            TreeBuilderError("artifact", code="artifact_validation_error"),
            ArtifactValidationError,
        ),
        (ClusterizerError("tree", code="tree_format_error"), TreeFormatError),
        (ClusterizerError("cluster"), api.ClusterizationError),
        (
            ClusterizerError("conflict", code="output_directory_conflict_error"),
            OutputDirectoryConflictError,
        ),
        (ValueError("unknown"), api.DamicoreError),
    ],
)
def test_stage_error_translation(error: Exception, expected: type[Exception]) -> None:
    assert isinstance(api._translated_stage_error(error), expected)


def test_progress_adapter_enabled_and_disabled() -> None:
    callback, close = distance_progress(False)
    assert callback is None
    close()
    callback, close = distance_progress(True)
    assert callback is not None
    callback(1, 2, "distance")
    close()
