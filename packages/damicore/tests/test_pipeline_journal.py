import json
from pathlib import Path

import pytest

from damicore import ArtifactValidationError, CheckpointMismatchError
from damicore.pipeline import PipelineJournal

pytestmark = pytest.mark.unit


def _manifest(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest: dict[str, object] = {"status": "created", "stages": {}}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir, manifest


def test_journal_receipts_validate_outputs(tmp_path: Path) -> None:
    run_dir, manifest = _manifest(tmp_path)
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    output = run_dir / "result.txt"
    journal = PipelineJournal(run_dir, manifest)
    started = journal.stage_started("normalizing", [source])
    output.write_text("ok", encoding="utf-8")
    journal.stage_completed("normalizing", started, [output], {"count": 1})
    assert journal.reusable("normalizing")
    output.write_text("bad", encoding="utf-8")
    with pytest.raises(ArtifactValidationError):
        journal.reusable("normalizing")


# Each row is one reason a completed receipt must not be reused. They are separate because a
# reuse decision that silently says "yes" skips a whole stage.
def test_a_receipt_from_a_changed_runtime_is_not_reusable(tmp_path: Path) -> None:
    run_dir, manifest = _manifest(tmp_path)
    journal = PipelineJournal(run_dir, manifest)
    journal.receipts["normalize"] = {
        "status": "completed",
        "runtime": {**journal.runtime, "numpy": "0.0.0"},
        "outputs": [{"path": "out.txt", "size_bytes": 1, "sha256": "0" * 64}],
    }
    with pytest.raises(CheckpointMismatchError, match="Runtime changed for stage"):
        journal.reusable("normalize")


def test_a_receipt_without_outputs_is_not_reusable(tmp_path: Path) -> None:
    run_dir, manifest = _manifest(tmp_path)
    journal = PipelineJournal(run_dir, manifest)
    journal.receipts["normalize"] = {
        "status": "completed",
        "runtime": journal.runtime,
        "outputs": [],
    }
    with pytest.raises(CheckpointMismatchError, match="no output receipt"):
        journal.reusable("normalize")


def test_a_receipt_output_pointing_outside_the_run_directory_is_rejected(tmp_path: Path) -> None:
    """The receipt is read back from disk, so its recorded path is untrusted input: a
    traversal there would make the journal verify -- and reuse -- a file it never wrote."""
    run_dir, manifest = _manifest(tmp_path)
    journal = PipelineJournal(run_dir, manifest)
    journal.receipts["normalize"] = {
        "status": "completed",
        "runtime": journal.runtime,
        "outputs": [{"path": "../escape.txt", "size_bytes": 1, "sha256": "0" * 64}],
    }
    with pytest.raises(ArtifactValidationError, match="escapes the run directory"):
        journal.reusable("normalize")


def test_a_stage_output_outside_the_run_directory_is_rejected(tmp_path: Path) -> None:
    run_dir, manifest = _manifest(tmp_path)
    journal = PipelineJournal(run_dir, manifest)
    outside = tmp_path / "outside.txt"
    outside.write_text("planted", encoding="utf-8")
    started = journal.stage_started("normalize", [])
    with pytest.raises(ArtifactValidationError, match="Receipt path escapes"):
        journal.stage_completed("normalize", started, [outside], {})


def test_a_stage_input_that_is_not_a_regular_file_is_rejected(tmp_path: Path) -> None:
    run_dir, manifest = _manifest(tmp_path)
    journal = PipelineJournal(run_dir, manifest)
    with pytest.raises(ArtifactValidationError, match="not a regular file"):
        journal.stage_started("normalize", [tmp_path / "absent.csv"])


def test_an_uninstalled_sibling_package_is_recorded_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fingerprint must still be produced when a sibling distribution is absent -- the
    stage packages are independently installable, so this is a supported configuration."""
    import damicore.pipeline as pipeline_module

    real_version = pipeline_module.version

    def missing_clusterizer(name: str) -> str:
        if name == "damicore-clusterizer":
            raise pipeline_module.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(pipeline_module, "version", missing_clusterizer)
    assert pipeline_module.runtime_fingerprint()["damicore-clusterizer"] == "unknown"
