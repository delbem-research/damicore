import hashlib
import json
import os
from pathlib import Path

import pytest

from damicore import (
    ArtifactValidationError,
    ConfigurationError,
    DamicoreError,
    ExecutionConfig,
    MaterializationError,
    ResourceLimits,
    estimate,
    load_result,
    run,
)

pytestmark = pytest.mark.unit


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "input.csv"
    path.write_text("a,b,c\naaaa,aaab,zzzz\naaaa,aaac,zzzy\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("split", ["columns", "rows"])
def test_complete_pipeline_reuse_save_and_load(tmp_path: Path, split: str) -> None:
    source = _csv(tmp_path)
    preview = estimate(source, split=split, execution=ExecutionConfig(workers=1))
    assert preview.within_limits
    output = tmp_path / f"run-{split}"
    result = run(
        source,
        split=split,
        output_dir=output,
        progress=False,
        keep_normalized=True,
        execution=ExecutionConfig(workers=1, pairs_per_shard=1),
    )
    assert result.report.status == "completed"
    assert list(result.membership.columns) == ["object_id", "label", "cluster"]
    assert result.distance_matrix.head(2).shape == (2, 2)
    saved = result.save(tmp_path / f"saved-{split}")
    result.close()
    with pytest.raises(ValueError, match="reload"):
        _ = result.distance_matrix.shape
    loaded = load_result(saved.run_dir)
    loaded.close()
    reused = run(
        source,
        split=split,
        output_dir=output,
        progress=False,
        keep_normalized=True,
        execution=ExecutionConfig(workers=1, pairs_per_shard=1),
    )
    reused.close()


def test_the_default_shard_size_is_resolved_from_the_pair_count_and_recorded(
    tmp_path: Path,
) -> None:
    """The run records the shard size the distance stage actually used, not the "auto" it
    was asked for, so the configuration hash names the layout of its checkpoints."""
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path), output_dir=output, progress=False, execution=ExecutionConfig(workers=1)
    )
    result.close()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    shards = json.loads(
        (output / "checkpoints" / "distance-shards.json").read_text(encoding="utf-8")
    )
    assert manifest["config"]["pairs_per_shard"] == 1
    assert result.report.pairs_per_shard == 1
    assert shards["shard_count"] == shards["pair_count"] == 3


def test_limits_materialization_and_corruption(tmp_path: Path) -> None:
    source = _csv(tmp_path)
    limited = estimate(
        source,
        execution=ExecutionConfig(
            workers=1,
            limits=ResourceLimits(max_objects=2),
        ),
    )
    assert limited.violations[0] == "max_objects"

    output = tmp_path / "run"
    result = run(
        source,
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1, pandas_materialization_limit_bytes=1),
    )
    with pytest.raises(MaterializationError, match="head"):
        result.distance_matrix.to_pandas()
    result.close()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    # Corrupting the bytes trips the inventory hash check, which runs first. Discriminated so
    # the row cannot silently start passing through a different guard.
    (output / "tree.nwk").write_text("corrupt", encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="hash or size mismatch"):
        load_result(output)


@pytest.mark.parametrize("malicious_path", ["../escape.bin", "/tmp/escape.bin"])
def test_save_rejects_manifest_paths_outside_roots(tmp_path: Path, malicious_path: str) -> None:
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = next(iter(manifest["artifacts"]))
    record = manifest["artifacts"].pop(key)
    record["path"] = malicious_path
    manifest["artifacts"][malicious_path] = record
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    destination = tmp_path / "exports" / "saved"
    with pytest.raises(ArtifactValidationError, match="manifest is invalid"):
        result.save(destination)
    assert not destination.exists()
    result.close()


def test_save_revalidates_artifact_bytes_and_symlinks(tmp_path: Path) -> None:
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    tree_path = output / "tree.nwk"
    original_tree = tree_path.read_bytes()
    tree_path.write_text("tampered;", encoding="utf-8")
    destination = tmp_path / "saved-tampered"
    with pytest.raises(ArtifactValidationError, match="hash or size"):
        result.save(destination)
    assert not (destination / "tree.nwk").exists()
    tree_path.write_bytes(original_tree)

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    external = tmp_path / "external.bin"
    external.write_bytes(b"external")
    link = output / "external-link"
    os.symlink(external, link)
    record = {
        "path": "external-link",
        "size_bytes": external.stat().st_size,
        "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
    }
    manifest["artifacts"]["external-link"] = record
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="escapes"):
        result.save(tmp_path / "saved-link")
    result.close()


def test_load_result_rejects_extra_manifest_fields(tmp_path: Path) -> None:
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    result.close()
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extended = {**manifest, "unexpected": True}
    manifest_path.write_text(json.dumps(extended), encoding="utf-8")
    with pytest.raises(ArtifactValidationError):
        load_result(output)

    manifest["artifacts"].pop("tree.nwk")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactValidationError):
        load_result(output)


def test_a_newick_artifact_without_its_terminator_is_rejected(tmp_path: Path) -> None:
    """The Newick check is the last one load_result runs, so reaching it means repairing the
    inventory first -- otherwise the hash check fires and the terminator is never inspected."""
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    result.close()
    newick = output / "tree.nwk"
    newick.write_text("(a,b)", encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["tree.nwk"] = {
        "path": "tree.nwk",
        "size_bytes": newick.stat().st_size,
        "sha256": hashlib.sha256(newick.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="does not end with semicolon"):
        load_result(output)


def test_a_relative_path_escaping_the_run_directory_is_rejected() -> None:
    from damicore.result import _contained_relative_path

    for value in ["/absolute/path", "../escape", ""]:
        with pytest.raises(ArtifactValidationError, match="contained relative POSIX path"):
            _contained_relative_path(value)


# Each row tampers with the manifest a completed run wrote, then asks save() to trust it.
# save() copies bytes out of the run directory, so every clause it checks is the difference
# between exporting verified artifacts and exporting whatever the manifest happens to name.
SAVE_REJECTIONS = [
    pytest.param("not-completed", "Only completed artifacts", id="status-not-completed"),
    pytest.param("missing-source", "source does not exist", id="artifact-source-missing"),
    pytest.param("manifest-collision", "duplicate target", id="target-collides-with-manifest"),
]


@pytest.mark.parametrize(("tamper", "discriminator"), SAVE_REJECTIONS)
def test_save_rejects_a_tampered_inventory(tmp_path: Path, tamper: str, discriminator: str) -> None:
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = next(iter(manifest["artifacts"]))
    if tamper == "not-completed":
        manifest["status"] = "interrupted"
    elif tamper == "missing-source":
        record = dict(manifest["artifacts"][key])
        record["path"] = "absent.bin"
        manifest["artifacts"]["absent.bin"] = record
    else:
        record = dict(manifest["artifacts"][key])
        record["path"] = "manifest.json"
        manifest["artifacts"]["manifest.json"] = record
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    destination = tmp_path / "saved"
    with pytest.raises(ArtifactValidationError, match=discriminator):
        result.save(destination)
    result.close()


@pytest.mark.parametrize("compressor", ["zlib", "gzip"])
def test_both_compressors_complete_a_run(tmp_path: Path, compressor: str) -> None:
    """gzip is a documented public option that no test had ever executed."""
    result = run(
        _csv(tmp_path),
        output_dir=tmp_path / compressor,
        compressor=compressor,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    try:
        assert result.report.status == "completed"
        assert result.distance_matrix.shape[0] == len(result.membership)
    finally:
        result.close()


def test_diagnostics_are_written_when_requested(tmp_path: Path) -> None:
    """save_diagnostics is a documented public option that no test had ever executed."""
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        save_diagnostics=True,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    try:
        assert result.artifacts.diagnostics_dir is not None
        assert (output / "diagnostics" / "distance.csv").is_file()
        assert (output / "diagnostics" / "ncd-pairs.csv").is_file()
    finally:
        result.close()


def test_an_unexpected_failure_names_its_cause_in_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untyped failure still has to be diagnosable from report.json alone."""
    import damicore.api as damicore_api

    def exploding_build_tree(*args: object, **kwargs: object) -> object:
        raise MemoryError("workspace allocation refused")

    monkeypatch.setattr(damicore_api, "build_tree", exploding_build_tree)
    output = tmp_path / "run"
    with pytest.raises(DamicoreError, match="MemoryError: workspace allocation refused"):
        run(
            _csv(tmp_path),
            output_dir=output,
            progress=False,
            execution=ExecutionConfig(workers=1),
        )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "MemoryError" in str(report["error"])


# Each row is one configuration value the public API must refuse rather than coerce.
@pytest.mark.parametrize(
    ("kwargs", "discriminator"),
    [
        pytest.param({"compressor": "bz2"}, "zlib' or 'gzip", id="unknown-compressor"),
        pytest.param({"num_clusters": 0}, "num_clusters", id="cluster-count-below-one"),
        pytest.param({"split": "diagonal"}, "split", id="unknown-split"),
    ],
)
def test_an_invalid_option_is_refused_before_any_work(
    tmp_path: Path, kwargs: dict[str, object], discriminator: str
) -> None:
    output = tmp_path / "run"
    with pytest.raises(ConfigurationError, match=discriminator):
        run(_csv(tmp_path), output_dir=output, progress=False, **kwargs)  # pyright: ignore[reportArgumentType]
    assert not (output / "manifest.json").exists()


def test_an_interrupted_run_is_recorded_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A KeyboardInterrupt must still leave a report saying what happened, and must never
    leave the run looking completed."""
    import damicore.api as damicore_api

    def interrupted_build_tree(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(damicore_api, "build_tree", interrupted_build_tree)
    output = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run(
            _csv(tmp_path),
            output_dir=output,
            progress=False,
            execution=ExecutionConfig(workers=1),
        )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "interrupted"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] != "completed"


def test_a_platform_without_rusage_reports_no_peak_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """peak_rss_bytes is optional by contract, so a platform that refuses the measurement
    must produce null rather than failing the run."""
    import resource

    import damicore.api as damicore_api

    def refusing_getrusage(who: int) -> object:
        raise OSError("rusage unavailable")

    monkeypatch.setattr(resource, "getrusage", refusing_getrusage)
    assert damicore_api._peak_rss() is None
