import hashlib
import json
import os
import random
from pathlib import Path
from typing import Any

import pyppmd
import pytest
from damicore_distance.config import PPMD_STREAM_RESIDUE_BYTES

import damicore.api as api
from damicore import (
    ArtifactValidationError,
    ConfigurationError,
    DamicoreError,
    ExecutionConfig,
    MaterializationError,
    ResourceLimitError,
    ResourceLimits,
    estimate,
    load_result,
    run,
)
from damicore.api import _SCALE_GUIDANCE, _WINDOW_GUIDANCE, _guidance

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


@pytest.mark.parametrize("compressor", ["ppmd", "zlib", "gzip", "bzip2", "lzma"])
def test_every_compressor_completes_a_run(tmp_path: Path, compressor: str) -> None:
    """Each documented compressor is executed end to end, not only the default."""
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


@pytest.mark.parametrize(
    ("chosen", "recorded"),
    [
        pytest.param(
            {},
            {
                "compressor": "ppmd",
                "compression_level": None,
                "ppmd_order": 6,
                "ppmd_memory_bytes": 67_108_864,
                "bzip2_block_size_100k": None,
                "lzma_dictionary_bytes": None,
            },
            id="default",
        ),
        pytest.param(
            {"compressor": "zlib"},
            {
                "compressor": "zlib",
                "compression_level": 6,
                "ppmd_order": None,
                "ppmd_memory_bytes": None,
                "bzip2_block_size_100k": None,
                "lzma_dictionary_bytes": None,
            },
            id="zlib",
        ),
        pytest.param(
            {"compressor": "bzip2"},
            {
                "compressor": "bzip2",
                "compression_level": None,
                "ppmd_order": None,
                "ppmd_memory_bytes": None,
                "bzip2_block_size_100k": 9,
                "lzma_dictionary_bytes": None,
            },
            id="bzip2",
        ),
        pytest.param(
            {"compressor": "lzma"},
            {
                "compressor": "lzma",
                "compression_level": None,
                "ppmd_order": None,
                "ppmd_memory_bytes": None,
                "bzip2_block_size_100k": None,
                "lzma_dictionary_bytes": 8_388_608,
            },
            id="lzma",
        ),
    ],
)
def test_the_manifest_records_the_compressor_settings_in_force_and_only_those(
    tmp_path: Path, chosen: dict[str, str], recorded: dict[str, object]
) -> None:
    """A setting the compressor does not read is recorded as `None`, so it neither enters
    the configuration hash nor suggests a choice that had no effect."""
    output = tmp_path / "run"
    run(
        _csv(tmp_path),
        output_dir=output,
        progress=False,
        execution=ExecutionConfig(workers=1),
        **chosen,  # pyright: ignore[reportArgumentType]
    ).close()
    config = json.loads((output / "manifest.json").read_text(encoding="utf-8"))["config"]
    assert {name: config[name] for name in recorded} == recorded


def test_the_report_states_how_far_the_largest_object_is_from_itself(tmp_path: Path) -> None:
    """Computed here from the object bytes through pyppmd's one-shot entry point, so the
    figure is checked against the codec rather than against the stage that reports it."""
    output = tmp_path / "run"
    result = run(
        _csv(tmp_path),
        output_dir=output,
        keep_normalized=True,
        progress=False,
        execution=ExecutionConfig(workers=1),
    )
    result.close()
    objects = sorted((output / "normalization" / "objects").iterdir())
    # The largest object, ties to the higher SHA-256 (ADR 0024), as the stage chooses it.
    largest = max(
        (path.read_bytes() for path in objects),
        key=lambda payload: (len(payload), hashlib.sha256(payload).hexdigest()),
    )

    def size(payload: bytes) -> int:
        return len(pyppmd.compress(payload, max_order=6, mem_size=67_108_864, variant="I"))

    expected = (size(largest + largest) - size(largest)) / size(largest)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["ncd_self_largest"] == result.report.ncd_self_largest == expected


def test_ppmd_settings_are_part_of_the_run_identity(tmp_path: Path) -> None:
    source = _csv(tmp_path)
    hashes: list[str] = []
    for order in (6, 8):
        output = tmp_path / f"order-{order}"
        run(
            source,
            output_dir=output,
            ppmd_order=order,
            progress=False,
            execution=ExecutionConfig(workers=1),
        ).close()
        hashes.append(json.loads((output / "manifest.json").read_text())["config_hash"])
    assert hashes[0] != hashes[1]


def test_preflight_charges_each_compressor_stream_once_per_worker(tmp_path: Path) -> None:
    """A stream can fill to its stated size on incompressible data, and every worker holds
    one, so the projection charges that size per worker -- the PPMd model as configured,
    DEFLATE's 256 KiB state -- and the default fits the default limit at the four workers
    "auto" resolves to at most."""
    source = _csv(tmp_path)
    workers = ExecutionConfig(workers=4)
    base = estimate(source, ppmd_memory_bytes=1 << 20, execution=workers)
    grown = estimate(source, ppmd_memory_bytes=(1 << 20) + 1_000, execution=workers)
    deflate = estimate(source, compressor="zlib", execution=workers)
    assert grown.estimated_working_memory_bytes - base.estimated_working_memory_bytes == 4_000
    # And PPMd's stream residue (ADR 0027): each object opened four times, each pair three,
    # and the largest object once more, every worker charged the whole run's.
    residue = PPMD_STREAM_RESIDUE_BYTES * (base.object_count * 4 + base.pair_count * 3 + 1)
    assert base.estimated_working_memory_bytes - deflate.estimated_working_memory_bytes == 4 * (
        (1 << 20) - 262_144 + residue
    )
    assert estimate(source, execution=workers).within_limits


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
        pytest.param(
            {"compressor": "bz2"},
            "'ppmd', 'zlib', 'gzip', 'bzip2' or 'lzma'",
            id="unknown-compressor",
        ),
        pytest.param(
            {"lzma_dictionary_bytes": 4_096},
            "lzma_dictionary_bytes does not apply to ppmd",
            id="lzma-setting-for-ppmd",
        ),
        pytest.param(
            {"compression_level": 9},
            "compression_level does not apply to ppmd",
            id="level-for-ppmd",
        ),
        pytest.param(
            {"compressor": "zlib", "ppmd_order": 8},
            "ppmd_order does not apply to zlib",
            id="ppmd-setting-for-zlib",
        ),
        pytest.param({"ppmd_memory_bytes": 1}, "ppmd_memory_bytes", id="ppmd-memory-clamped"),
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


def _wide_csv(tmp_path: Path) -> Path:
    """Two columns whose objects are 20 KiB each, so their pair is past DEFLATE's window."""
    path = tmp_path / "wide.csv"
    rows = ["a,b"]
    for index in range(1_400):
        rows.append(f"value-{index:05d},value-{index:05d}x")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_a_pair_past_the_compressor_window_is_reported_and_refused(tmp_path: Path) -> None:
    """The gate of issue 59: preflight names it, `run` raises it, and raising the factor runs.

    `zlib` is asked for a pair it cannot match across, which is the measurement defect
    ADR 0023 records -- not a cost. The override is a limit like any other, and the run it
    allows records the factor it ran under.
    """
    source = _wide_csv(tmp_path)
    execution = ExecutionConfig(workers=1, limits=ResourceLimits())
    preview = estimate(
        source, split="columns", compressor="zlib", compression_level=6, execution=execution
    )
    assert preview.compressor_window_bytes == 32_768
    assert preview.max_pair_bytes is not None
    assert preview.max_pair_bytes > 32_768
    assert preview.violations == ["compressor_window"]
    assert not preview.within_limits

    with pytest.raises(DamicoreError) as refused:
        run(
            source,
            split="columns",
            output_dir=tmp_path / "refused",
            compressor="zlib",
            compression_level=6,
            progress=False,
            execution=execution,
        )
    assert "compressor_window" in str(refused.value)

    lifted = ExecutionConfig(
        workers=1,
        pairs_per_shard=1,
        limits=ResourceLimits(max_pair_window_factor=8.0),
    )
    allowed = estimate(
        source, split="columns", compressor="zlib", compression_level=6, execution=lifted
    )
    assert allowed.violations == []
    result = run(
        source,
        split="columns",
        output_dir=tmp_path / "allowed",
        compressor="zlib",
        compression_level=6,
        progress=False,
        execution=lifted,
    )
    assert result.report.compressor_window_bytes == 32_768
    assert result.report.pairs_over_window == 1
    manifest = json.loads((result.artifacts.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["config"]["limits"]["max_pair_window_factor"] == 8.0
    result.close()


def _restarting_corpus(directory: Path) -> Path:
    """Two files of random bytes that each fill a 1 MiB PPMd model on their own."""
    directory.mkdir()
    for seed in (1, 2):
        (directory / f"object_{seed}.bin").write_bytes(random.Random(seed).randbytes(262_144))
    return directory


def test_a_ppmd_model_an_object_fills_is_refused_and_can_be_lifted(tmp_path: Path) -> None:
    """ADR 0025: under PPMd the stage refuses a run in which an object fills the model, before
    any pair; ResourceLimits.allow_model_restart measures anyway and is recorded."""
    corpus = _restarting_corpus(tmp_path / "corpus")
    with pytest.raises(ResourceLimitError) as refused:
        run(
            corpus,
            source_kind="files",
            ppmd_memory_bytes=1_048_576,
            output_dir=tmp_path / "refused",
            progress=False,
            execution=ExecutionConfig(workers=1),
        )
    assert refused.value.code == "resource_limit_error"
    assert "ppmd_memory_bytes" in str(refused.value)
    assert not (tmp_path / "refused" / "distance.npy").exists()

    result = run(
        corpus,
        source_kind="files",
        ppmd_memory_bytes=1_048_576,
        output_dir=tmp_path / "allowed",
        progress=False,
        execution=ExecutionConfig(workers=1, limits=ResourceLimits(allow_model_restart=True)),
    )
    manifest = json.loads((result.artifacts.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["config"]["limits"]["allow_model_restart"] is True
    assert manifest["stages"]["distancing"]["metrics"]["ppmd_model_checked"] is False
    result.close()


def test_a_refused_run_resumed_with_the_check_lifted_says_it_was_not_applied(
    tmp_path: Path,
) -> None:
    """The refusal leaves the run directory behind, and manifest.json keeps the limits of the
    invocation that created it. The distance receipt is written by the invocation that ran the
    stage, so it is the record of whether the check applied (ADR 0025)."""
    corpus = _restarting_corpus(tmp_path / "corpus")
    output = tmp_path / "run"
    arguments: dict[str, Any] = {
        "source_kind": "files",
        "ppmd_memory_bytes": 1_048_576,
        "output_dir": output,
        "progress": False,
    }
    with pytest.raises(ResourceLimitError):
        run(corpus, execution=ExecutionConfig(workers=1), **arguments)
    lifted = ExecutionConfig(workers=1, limits=ResourceLimits(allow_model_restart=True))
    result = run(corpus, execution=lifted, **arguments)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"]["distancing"]["metrics"]["ppmd_model_checked"] is False
    result.close()

    checked = run(_csv(tmp_path), output_dir=tmp_path / "checked", progress=False)
    manifest = json.loads((tmp_path / "checked" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"]["distancing"]["metrics"]["ppmd_model_checked"] is True
    checked.close()


@pytest.mark.parametrize("completed", [True, False], ids=["reused", "resumed"])
def test_a_call_with_the_check_refuses_a_directory_measured_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: bool
) -> None:
    """A call that applies the PPMd model check never returns a matrix measured without it
    (ADR 0025). A directory whose distance stage completed with the check lifted is refused,
    whether the run completed and would be reused or stopped later and would be resumed."""
    corpus = _restarting_corpus(tmp_path / "corpus")
    output = tmp_path / "run"
    lifted = ExecutionConfig(workers=1, limits=ResourceLimits(allow_model_restart=True))
    arguments: dict[str, Any] = {
        "source_kind": "files",
        "ppmd_memory_bytes": 1_048_576,
        "output_dir": output,
        "progress": False,
    }
    if completed:
        run(corpus, execution=lifted, **arguments).close()
    else:

        def fail_clusterizing(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected clusterizer failure")

        monkeypatch.setattr(api, "cluster_tree", fail_clusterizing)
        with pytest.raises(DamicoreError):
            run(corpus, execution=lifted, **arguments)
        monkeypatch.undo()
    with pytest.raises(ResourceLimitError) as refused:
        run(corpus, execution=ExecutionConfig(workers=1), **arguments)
    assert "allow_model_restart" in str(refused.value)
    # The directory stays usable for the call that measured it.
    run(corpus, execution=lifted, **arguments).close()


def _near_copy_corpus(directory: Path, size: int) -> Path:
    """Two files of ``size`` bytes, the second the first with one byte in a hundred changed."""
    directory.mkdir()
    original = bytes(random.Random(62).choice(b"abcdefgh0123456789\n") for _ in range(size))
    copy = bytearray(original)
    for index in range(0, size, 100):
        copy[index] = ord("z")
    (directory / "a.txt").write_bytes(original)
    (directory / "b.txt").write_bytes(bytes(copy))
    return directory


def test_the_working_memory_limit_decides_the_second_look(tmp_path: Path) -> None:
    """The PPMd check looks again under twice the model only within
    `ResourceLimits.max_working_memory_bytes`: a pair half the model codes otherwise is
    measured under the default limit, and refused, naming the limit, under one that admits the
    run but holds no stream of twice the model (ADR 0025)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    original = random.Random(12_000).randbytes(12_000)
    copy = bytearray(original)
    for index in random.Random(12_000).sample(range(len(copy)), 12):
        copy[index] = random.Random(index).randrange(256)
    (corpus / "a.bin").write_bytes(original)
    (corpus / "b.bin").write_bytes(bytes(copy))
    arguments: dict[str, Any] = {
        "source_kind": "files",
        "ppmd_memory_bytes": 1_048_576,
        "progress": False,
    }
    run(
        corpus, output_dir=tmp_path / "roomy", execution=ExecutionConfig(workers=1), **arguments
    ).close()
    # One worker is charged two 4 MiB chunks and the 1 MiB model; twice the model needs 1 MiB
    # more than that, which this limit does not have.
    tight = ExecutionConfig(
        workers=1, limits=ResourceLimits(max_working_memory_bytes=9 * 1_048_576 + 524_288)
    )
    assert estimate(
        corpus, execution=tight, **{k: v for k, v in arguments.items() if k != "progress"}
    ).within_limits
    with pytest.raises(ResourceLimitError, match="max_working_memory_bytes"):
        run(corpus, output_dir=tmp_path / "tight", execution=tight, **arguments)


def test_preflight_charges_the_disk_pairs_wait_on_for_the_second_look(tmp_path: Path) -> None:
    """Under the PPMd check a pair waiting for its second look waits on disk, 40 bytes, and
    preflight charges it for every pair: nothing else may differ (ADR 0025)."""
    source = _csv(tmp_path)
    checked = estimate(source)
    lifted = estimate(
        source, execution=ExecutionConfig(limits=ResourceLimits(allow_model_restart=True))
    )
    assert checked.estimated_artifact_bytes - lifted.estimated_artifact_bytes == (
        checked.pair_count * 40
    )
    zlib = estimate(source, compressor="zlib")
    assert zlib.estimated_artifact_bytes == lifted.estimated_artifact_bytes


def test_the_pairing_is_recorded_and_named_by_its_rule(tmp_path: Path) -> None:
    """The pairing changes every value, so it is configuration the hash covers and a rule the
    manifest names: `ncd-max/v2` for the concatenation, `ncd-max-interleaved/v1` for
    interleaving, whose block size is recorded beside it and absent otherwise (ADR 0026)."""
    source = _csv(tmp_path)

    def recorded(name: str, **pairing: Any) -> dict[str, Any]:
        output = tmp_path / name
        run(source, output_dir=output, progress=False, **pairing).close()
        return json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    concatenated = recorded("concat")
    interleaved = recorded("interleave", pairing="interleave", interleave_block_bytes=512)
    assert concatenated["config"]["pairing"] == "concat"
    assert concatenated["config"]["interleave_block_bytes"] is None
    assert concatenated["method"]["distance"]["name"] == "ncd-max/v2"
    assert interleaved["config"]["pairing"] == "interleave"
    assert interleaved["config"]["interleave_block_bytes"] == 512
    assert interleaved["method"]["distance"]["name"] == "ncd-max-interleaved/v1"
    assert concatenated["config_hash"] != interleaved["config_hash"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"interleave_block_bytes": 512}, "interleave_block_bytes does not apply to concat"),
        ({"pairing": "zip"}, "pairing must be exactly"),
        ({"pairing": "interleave", "interleave_block_bytes": 0}, "interleave_block_bytes"),
    ],
)
def test_a_pairing_setting_that_does_not_apply_is_refused(
    tmp_path: Path, arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        estimate(_csv(tmp_path), **arguments)


def test_interleaving_admits_a_pair_past_the_window_and_gates_its_block(tmp_path: Path) -> None:
    """Under interleaving the compressor holds one block of each object at once, not both
    objects, so the window gate reads that span: two 40 KiB files are past DEFLATE's 32 KiB
    window concatenated, within it in 1 KiB blocks, and past it again in 32 KiB blocks
    (ADR 0026)."""
    corpus = _near_copy_corpus(tmp_path / "corpus", 40_960)

    def gate(**pairing: Any) -> tuple[int | None, list[str]]:
        preview = estimate(corpus, source_kind="files", compressor="zlib", **pairing)
        return preview.max_pair_bytes, preview.violations

    assert gate() == (81_920, ["compressor_window"])
    assert gate(pairing="interleave", interleave_block_bytes=1_024) == (2_048, [])
    assert gate(pairing="interleave", interleave_block_bytes=32_768) == (
        65_536,
        ["compressor_window"],
    )


def test_preflight_charges_an_interleaving_worker_what_it_charges_a_concatenating_one(
    tmp_path: Path,
) -> None:
    """An interleaving worker reads each block into the buffer it compresses from, a chunk at a
    time, and never holds a block whole, so the block size is no part of its charge: a large
    block must not refuse a run the concatenation admits (ADR 0026, ADR 0027)."""
    source = _csv(tmp_path)
    workers = ExecutionConfig(workers=2, compression_chunk_bytes=1_048_576)
    base = estimate(source, execution=workers).estimated_working_memory_bytes
    for block in (1_024, 4_194_304, 64 * 1_048_576):
        interleaved = estimate(
            source, execution=workers, pairing="interleave", interleave_block_bytes=block
        )
        assert interleaved.estimated_working_memory_bytes == base, block
        assert "max_working_memory_bytes" not in interleaved.violations, block


def test_the_refusal_advises_on_the_gates_it_actually_tripped(tmp_path: Path) -> None:
    """The window is not a cost, so the advice for costs must not be the advice it gets.

    Reshaping the source or raising a limit after reading the estimate does nothing for a pair
    the compressor cannot match across, and the three outcomes -- the window alone, the window
    beside a scale gate, and scale alone -- are what a caller reads in the error.
    """
    assert _guidance(["compressor_window"]) == _WINDOW_GUIDANCE
    both = _guidance(["max_objects", "compressor_window"])
    assert both.startswith(_SCALE_GUIDANCE)
    assert both.endswith(_WINDOW_GUIDANCE)
    assert _guidance(["max_objects"]) == _SCALE_GUIDANCE

    source = _wide_csv(tmp_path)
    with pytest.raises(DamicoreError) as refused:
        run(
            source,
            split="columns",
            output_dir=tmp_path / "advised",
            compressor="zlib",
            compression_level=6,
            progress=False,
            execution=ExecutionConfig(workers=1, limits=ResourceLimits()),
        )
    message = str(refused.value)
    assert _WINDOW_GUIDANCE in message
    assert _SCALE_GUIDANCE not in message


def test_the_default_compressor_declares_no_window_and_is_not_gated(tmp_path: Path) -> None:
    """PPMd has a model, not a window, so the same source it would gate on runs ungated."""
    preview = estimate(_wide_csv(tmp_path), split="columns", execution=ExecutionConfig(workers=1))
    assert preview.compressor_window_bytes is None
    assert "compressor_window" not in preview.violations
