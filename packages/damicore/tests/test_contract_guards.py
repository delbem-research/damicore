"""Guards that back the public contracts: the CLI exit-status table, the configuration
bounds, the preflight input checks, and the manifest's own narrowing helpers."""

import json
import os
from importlib import import_module
from pathlib import Path

import pytest
from damicore_normalizer import NormalizerError

from damicore import (
    ArtifactValidationError,
    CheckpointMismatchError,
    ConfigurationError,
    DamicoreError,
    DatasetFormatError,
    ExecutionConfig,
    InputValidationError,
    OutputDirectoryConflictError,
    ResourceLimitError,
    estimate,
)
from damicore.cli import _exit_code, main
from damicore.manifest import atomic_json, json_mapping, json_sequence

pytestmark = pytest.mark.unit


# Every public failure maps onto a CLI exit status. The table is the
# contract a shell script depends on, so each row pins one status rather than the mapping
# being re-derived from the class hierarchy at review time.
EXIT_STATUS_TABLE = [
    pytest.param(ConfigurationError("bad"), 2, id="configuration-error"),
    pytest.param(InputValidationError("bad"), 2, id="input-validation-error"),
    pytest.param(DatasetFormatError("bad"), 2, id="dataset-format-error-inherits-input"),
    pytest.param(ResourceLimitError("bad"), 3, id="resource-limit-error"),
    pytest.param(ArtifactValidationError("bad"), 4, id="artifact-validation-error"),
    pytest.param(OutputDirectoryConflictError("bad"), 5, id="output-directory-conflict"),
    pytest.param(CheckpointMismatchError("bad"), 5, id="checkpoint-mismatch"),
    pytest.param(DamicoreError("bad"), 4, id="unclassified-falls-back-to-four"),
]


@pytest.mark.parametrize(("error", "status"), EXIT_STATUS_TABLE)
def test_every_public_failure_maps_to_its_documented_exit_status(
    error: DamicoreError, status: int
) -> None:
    assert _exit_code(error) == status


# The table above pins the mapping from error to status, but it constructs every error by
# hand, so it cannot see whether a failure reaches that mapping at all. These rows enter
# through the real argv instead, which is the boundary a user actually crosses.
CLI_REJECTED_FLAGS = [
    pytest.param(["--workers", "0"], id="workers-below-one"),
    pytest.param(["--max-objects", "0"], id="max-objects-zero"),
    pytest.param(["--max-pairs", "-5"], id="max-pairs-negative"),
    pytest.param(["--max-matrix-bytes", "0"], id="max-matrix-bytes-zero"),
    pytest.param(["--max-working-memory-bytes", "-1"], id="max-working-memory-negative"),
]


@pytest.mark.parametrize("flags", CLI_REJECTED_FLAGS)
def test_a_flag_the_config_rejects_leaves_the_cli_as_a_typed_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    source = tmp_path / "input.csv"
    source.write_text("a,b\naa,ab\n", encoding="utf-8")
    assert main(["estimate", str(source), *flags]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "configuration_error"


def test_an_output_path_that_is_not_a_directory_is_a_typed_conflict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The directory is walked with iterdir(), which raises NotADirectoryError on a file:
    neither a public error nor a documented exit status."""
    source = tmp_path / "input.csv"
    source.write_text("a,b\naa,ab\n", encoding="utf-8")
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")
    argv = ["run", str(source), "--no-progress", "--output-dir", str(occupied)]
    assert main(argv) == 5
    assert json.loads(capsys.readouterr().err)["code"] == "output_directory_conflict_error"


def test_an_input_that_cannot_be_read_is_a_typed_input_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """is_file() answers existence and type, never readability. A dropped mount or a revoked
    permission fails at the read instead, and must still reach the caller as the typed public
    failure with the CLI exit code that goes with it."""
    estimate_module = import_module("damicore.estimate")

    def unreadable(*_args: object, **_kwargs: object) -> object:
        raise NormalizerError("Could not read input", code="input_validation_error")

    source = tmp_path / "input.csv"
    source.write_text("a,b\naa,ab\n", encoding="utf-8")
    monkeypatch.setattr(estimate_module, "scan_source", unreadable)
    assert main(["estimate", str(source)]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "input_validation_error"


@pytest.mark.parametrize("field", ["workers", "pairs_per_shard"])
def test_an_execution_count_below_one_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be at least one"):
        ExecutionConfig.model_validate({field: 0})


def test_estimate_rejects_a_path_that_is_not_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(InputValidationError, match="not a regular file"):
        estimate(tmp_path / "absent.csv")


def test_estimate_detects_an_input_that_disappeared_underneath_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight re-stats every input it read. One that is gone by then cannot be compared,
    and reporting that as anything but drift would describe a run that can never happen."""
    # `damicore.estimate` the submodule is shadowed by the re-exported function of the same
    # name, so plain `import damicore.estimate as m` would bind the function instead.
    estimate_module = import_module("damicore.estimate")

    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n2,3\n", encoding="utf-8")

    def vanishing(_paths: object) -> object:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(estimate_module, "_fingerprints", vanishing)
    with pytest.raises(InputValidationError, match="disappeared during preflight") as raised:
        estimate(source)
    assert raised.value.code == "input_drift"


# Both helpers narrow a value decoded from JSON, where any type is possible.
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("a string", [], id="string-is-not-a-sequence"),
        pytest.param({"k": "v"}, [], id="mapping-is-not-a-sequence"),
        pytest.param(None, [], id="null-is-not-a-sequence"),
        pytest.param([1, 2], [1, 2], id="list-passes-through"),
    ],
)
def test_json_sequence_narrows_any_decoded_value(value: object, expected: list[object]) -> None:
    assert json_sequence(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("a string", {}, id="string-is-not-a-mapping"),
        pytest.param([1], {}, id="list-is-not-a-mapping"),
        pytest.param({"k": "v"}, {"k": "v"}, id="mapping-passes-through"),
    ],
)
def test_json_mapping_narrows_any_decoded_value(value: object, expected: dict[str, object]) -> None:
    assert json_mapping(value) == expected


def test_a_failed_manifest_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated failure while committing the manifest")

    monkeypatch.setattr(os, "replace", failing_replace)
    target = tmp_path / "manifest.json"
    with pytest.raises(OSError):
        atomic_json(target, {"a": 1})
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_estimate_detects_an_input_changed_during_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight re-stats the input after scanning: the scan is the long step, so a file
    replaced during it would otherwise be described by an estimate of the old bytes."""
    estimate_module = import_module("damicore.estimate")

    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n2,3\n", encoding="utf-8")
    real_scan = estimate_module.scan_source

    def mutating_scan(*args: object, **kwargs: object) -> object:
        result = real_scan(*args, **kwargs)  # pyright: ignore[reportCallIssue]
        source.write_text("a,b\n1,2\n2,3\n9,9\n", encoding="utf-8")
        return result

    monkeypatch.setattr(estimate_module, "scan_source", mutating_scan)
    with pytest.raises(InputValidationError, match="changed during preflight") as raised:
        estimate(source)
    assert raised.value.code == "input_drift"
