from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from damicore_clusterizer import (
    ClusterConfig,
    ClusterizerError,
    ClusterResult,
    cluster_tree,
)
from damicore_distance import (
    DistanceConfig,
    DistanceError,
    DistanceMatrixView,
    DistanceResult,
    compute_distance_matrix,
)
from damicore_normalizer import (
    DelimitedSource,
    FileCorpusSource,
    NormalizationConfig,
    NormalizationManifest,
    NormalizationResult,
    NormalizerError,
    ObjectDescriptor,
    SpreadsheetSource,
    materialize_objects,
)
from damicore_normalizer.config import ObjectSource
from damicore_tree_builder import (
    Tree,
    TreeBuildConfig,
    TreeBuilderError,
    TreeBuildResult,
    build_tree,
)
from pydantic import ValidationError

from damicore.config import ExecutionConfig
from damicore.errors import (
    ArtifactValidationError,
    CheckpointMismatchError,
    ClusterizationError,
    CompressionError,
    ConfigurationError,
    DamicoreError,
    DatasetFormatError,
    DistanceComputationError,
    DistanceMatrixValidationError,
    InputValidationError,
    MaterializationError,
    NormalizationError,
    OutputDirectoryConflictError,
    ResourceLimitError,
    TreeBuildError,
    TreeFormatError,
)
from damicore.estimate import ResourceEstimate, preflight
from damicore.manifest import (
    ClustersArtifact,
    LabelsArtifact,
    RunManifest,
    artifact_record,
    atomic_json,
    json_mapping,
    sha256_file,
)
from damicore.pipeline import (
    PipelineJournal,
    resume_fingerprint,
    runtime_fingerprint,
    utc_now,
)
from damicore.progress import distance_progress
from damicore.result import DamicoreResult, RunReport, artifact_paths

SCHEMA_VERSION = 2
# Read from the installed distribution rather than restated here. This value is stamped
# into every manifest as run provenance, so a third copy of the version string could put
# a number in an artifact that no distribution ever carried. It raises when damicore is
# not installed, which is the honest outcome: there is no correct value to fall back to.
VERSION = metadata.version("damicore")
logger = logging.getLogger(__name__)

# ResourceLimitError carries the distinction between input size and object count, because
# that is what tells a caller whether the run is reshapable at all. The guidance names how
# each source produces objects rather than assuming a split: a corpus has none, so advice
# phrased purely in splits would be unactionable for the source that most easily trips the
# object cap.
_SCALE_GUIDANCE = (
    "NCD is quadratic and Neighbor Joining cubic in the object count, so what decides "
    "feasibility is how many objects the source produces, not how many bytes it holds. A "
    "multi-gigabyte dataset stays feasible while that count is moderate, which is the usual "
    "case for split='columns'; split='rows' over the same file creates one object per row. A "
    "files source creates one object per file, so its object count is the size of the corpus. "
    "Streaming and memory maps bound RAM but not that work, which is why this is rejected "
    "here rather than later. Reshape the source, reduce the input, or raise individual "
    "ResourceLimits after reviewing estimate(); free disk is never bypassed."
)


def _execution(execution: ExecutionConfig | None) -> ExecutionConfig:
    try:
        return execution or ExecutionConfig()
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc


# The public API takes these as `str` so a caller gets ConfigurationError rather than a
# TypeError, but every stage config declares them as literals. Validating by *returning* the
# literal makes the narrowing part of the contract, so no call site needs a suppression and
# the rejection message has one definition.
def _validated_split(split: str) -> Literal["columns", "rows"]:
    if split == "columns":
        return "columns"
    if split == "rows":
        return "rows"
    raise ConfigurationError("split must be exactly 'columns' or 'rows'")


def _validated_compressor(compressor: str) -> Literal["zlib", "gzip"]:
    if compressor == "zlib":
        return "zlib"
    if compressor == "gzip":
        return "gzip"
    raise ConfigurationError("compressor must be exactly 'zlib' or 'gzip'")


# Which arguments belong to which source, derived from the models that define them rather
# than restated. The public signature is flat because a notebook is the primary caller, so
# something has to map those arguments back onto the discriminated union; reading the union
# for the answer keeps one declaration instead of two that can disagree.
#
# An argument that does not apply is rejected rather than ignored: silently dropping
# `delimiter` for a workbook would let a caller believe a setting took effect, and the
# artifacts would look valid while answering a question nobody asked.
_SOURCE_ARGUMENTS: dict[str, frozenset[str]] = {
    "delimited": frozenset(DelimitedSource.model_fields) - {"kind"},
    "xlsx": frozenset(SpreadsheetSource.model_fields) - {"kind"},
    "files": frozenset(FileCorpusSource.model_fields) - {"kind"},
}


def _object_source(
    source_kind: str,
    *,
    split: str | None,
    delimiter: str | None,
    encoding: str | None,
    sheet: str | None,
    recursive: bool | None,
    include_hidden: bool | None,
) -> ObjectSource:
    """Build the stage-level source description, rejecting settings it cannot honour."""
    if source_kind not in _SOURCE_ARGUMENTS:
        raise ConfigurationError("source_kind must be exactly 'delimited', 'xlsx' or 'files'")
    supplied: dict[str, object] = {
        "split": split,
        "delimiter": delimiter,
        "encoding": encoding,
        "sheet": sheet,
        "recursive": recursive,
        "include_hidden": include_hidden,
    }
    accepted = _SOURCE_ARGUMENTS[source_kind]
    rejected = sorted(
        name for name, value in supplied.items() if value is not None and name not in accepted
    )
    if rejected:
        raise ConfigurationError(f"{', '.join(rejected)} does not apply to a {source_kind} source")
    try:
        if source_kind == "files":
            return FileCorpusSource(
                recursive=True if recursive is None else recursive,
                include_hidden=False if include_hidden is None else include_hidden,
            )
        checked_split = _validated_split("columns" if split is None else split)
        if source_kind == "xlsx":
            return SpreadsheetSource(split=checked_split, sheet=sheet)
        return DelimitedSource(
            split=checked_split,
            delimiter="," if delimiter is None else delimiter,
            encoding="utf-8" if encoding is None else encoding,
        )
    except (LookupError, ValidationError) as exc:
        raise ConfigurationError(str(exc)) from exc


def _normalization_config(
    object_source: ObjectSource,
    execution: ExecutionConfig,
) -> NormalizationConfig:
    try:
        return NormalizationConfig(source=object_source, chunk_rows=execution.csv_chunk_rows)
    except (LookupError, ValidationError) as exc:
        raise ConfigurationError(str(exc)) from exc


def estimate(
    source: str | Path | Sequence[str | Path],
    *,
    source_kind: str = "delimited",
    split: str | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    sheet: str | None = None,
    recursive: bool | None = None,
    include_hidden: bool | None = None,
    keep_normalized: bool = False,
    save_diagnostics: bool = False,
    execution: ExecutionConfig | None = None,
) -> ResourceEstimate:
    """Inspect exact resource requirements without creating run artifacts.

    The whole input is hashed and scanned, but nothing is written: no run directory, no
    materialized objects. Exceeding a limit is not a failure here, which is what makes this
    the cheap way to decide whether a run is worth starting.

    Parameters
    ----------
    source
        A dataset file, a directory of files, or a sequence of file paths. Which of those is
        accepted follows ``source_kind``.
    source_kind
        Exactly ``"delimited"``, ``"xlsx"``, or ``"files"``. It decides where objects come
        from, and therefore which of the remaining arguments apply; supplying one that does
        not apply is a ``ConfigurationError`` rather than a silently ignored setting.
    split
        Exactly ``"columns"`` or ``"rows"``, defaulting to ``"columns"``. It decides what one
        object is for a dataset, and therefore the object count that every resource gate is
        quadratic or cubic in. It does not apply to ``"files"``, where each file is an object.
    keep_normalized
        Accepted for parity with :func:`run` and deliberately ignored: object bytes exist on
        disk while a run is in progress either way, so they are always counted.
    execution
        ``None`` uses the defaults. Free disk is measured against ``./damicore-results``,
        since no output directory has been chosen yet; a run writing elsewhere may see a
        different amount of free space.

    Returns
    -------
    ResourceEstimate
        Returned whether or not the run fits. ``within_limits`` is ``False`` exactly when
        ``violations`` is non-empty, and ``violations`` names each failed gate.

    Raises
    ------
    ConfigurationError
        ``source_kind`` or ``split`` is not one of its accepted values, an argument does not
        apply to this source, or ``delimiter``, ``encoding`` or ``execution`` is rejected by
        validation.
    InputValidationError
        An input path is not a readable regular file, a corpus breaks the corpus rules, or an
        input changed while it was being hashed and scanned (code ``input_drift``).
    DatasetFormatError
        The dataset violates the input contract.
    """
    settings = _execution(execution)
    object_source = _object_source(
        source_kind,
        split=split,
        delimiter=delimiter,
        encoding=encoding,
        sheet=sheet,
        recursive=recursive,
        include_hidden=include_hidden,
    )
    _normalization_config(object_source, settings)
    del keep_normalized
    return preflight(
        source,
        object_source=object_source,
        save_diagnostics=save_diagnostics,
        execution=settings,
        disk_target=Path.cwd() / "damicore-results",
    )


def _config_payload(
    *,
    object_source: ObjectSource,
    compressor: str,
    compression_level: int,
    num_clusters: int | None,
    keep_normalized: bool,
    save_diagnostics: bool,
    execution: ExecutionConfig,
) -> dict[str, object]:
    # Flattened from the source model rather than restated: the union owns which settings
    # exist for which kind, and a setting that does not apply records `None` instead of a
    # default that was never in force. That keeps the configuration hash honest, so two runs
    # differing only in source cannot collide onto the same run directory.
    described = object_source.model_dump(mode="json")
    return {
        "source_kind": described["kind"],
        "split": described.get("split"),
        "delimiter": described.get("delimiter"),
        "encoding": described.get("encoding"),
        "sheet": described.get("sheet"),
        "recursive": described.get("recursive"),
        "include_hidden": described.get("include_hidden"),
        "compressor": compressor,
        "compression_level": compression_level,
        "num_clusters": num_clusters,
        "keep_normalized": keep_normalized,
        "save_diagnostics": save_diagnostics,
        "workers": execution.effective_workers,
        "csv_chunk_rows": execution.csv_chunk_rows,
        "compression_chunk_bytes": execution.compression_chunk_bytes,
        "pairs_per_shard": execution.pairs_per_shard,
        "pandas_materialization_limit_bytes": execution.pandas_materialization_limit_bytes,
        "limits": execution.limits.model_dump(mode="json"),
    }


def _identity(input_hash: str, config: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    config_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    run_hash = hashlib.sha256(f"{input_hash}{config_hash}{SCHEMA_VERSION}".encode()).hexdigest()
    return config_hash, run_hash[:16]


def _load_normalization(path: Path) -> NormalizationResult:
    try:
        manifest = NormalizationManifest.model_validate_json(path.read_text(encoding="utf-8"))
        objects = tuple(ObjectDescriptor.model_validate(item) for item in manifest.objects)
    except (OSError, ValidationError) as exc:
        raise ArtifactValidationError("Normalization receipt is invalid") from exc
    return NormalizationResult(
        manifest_path=path,
        object_count=len(objects),
        total_bytes=sum(item.size_bytes for item in objects),
        objects=objects,
    )


def _verify_cross_artifacts(
    run_dir: Path,
    normalization: NormalizationResult,
    requested_communities: int | None,
    actual_communities: int,
) -> dict[str, bool]:
    try:
        labels = LabelsArtifact.model_validate_json(
            (run_dir / "labels.json").read_text(encoding="utf-8")
        )
        tree = Tree.model_validate_json((run_dir / "tree.json").read_text(encoding="utf-8"))
        clusters = ClustersArtifact.model_validate_json(
            (run_dir / "clusters.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise ArtifactValidationError("Final artifact schema validation failed") from exc
    expected_ids = [item.object_id for item in normalization.objects]
    expected_labels = [item.label for item in normalization.objects]
    object_ids = list(labels.object_ids)
    label_values = list(labels.labels)
    matrix = np.load(run_dir / "distance.npy", mmap_mode="r", allow_pickle=False)
    leaf_ids = [node.id for node in tree.nodes if node.kind == "leaf"]
    with (run_dir / "membership.csv").open(encoding="utf-8", newline="") as stream:
        membership = list(csv.DictReader(stream))
    membership_ids = [row["object_id"] for row in membership]
    membership_labels = [row["label"] for row in membership]
    membership_assignment = {row["object_id"]: int(row["cluster"]) for row in membership}
    cluster_items = clusters.clusters
    clustered_ids = [object_id for cluster in cluster_items for object_id in cluster.object_ids]
    cluster_numbers = [cluster.cluster for cluster in cluster_items]
    clusters_assignment = {
        object_id: cluster.cluster for cluster in cluster_items for object_id in cluster.object_ids
    }
    checks = {
        "normalization_to_labels": object_ids == expected_ids and label_values == expected_labels,
        "matrix_shape": matrix.shape == (len(object_ids), len(object_ids)),
        "tree_leaves": set(leaf_ids) == set(object_ids) and len(leaf_ids) == len(object_ids),
        "membership": membership_ids == object_ids
        and membership_labels == label_values
        and len(set(membership_ids)) == len(object_ids),
        "clusters": set(clustered_ids) == set(object_ids) and len(clustered_ids) == len(object_ids),
        "membership_clusters": membership_assignment == clusters_assignment
        and set(membership_assignment.values()) == set(cluster_numbers),
        "cluster_ids": cluster_numbers == list(range(len(cluster_numbers))),
        "requested_communities": requested_communities is None
        or requested_communities == actual_communities,
    }
    if not all(checks.values()):
        raise ArtifactValidationError("Cross-artifact verification failed", checks=checks)
    return checks


def _peak_rss() -> int | None:
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError):
        return None
    return value if sys.platform == "darwin" else value * 1024


def _stage_seconds(journal: PipelineJournal, stage: str) -> float:
    metrics = json_mapping(json_mapping(journal.receipts.get(stage)).get("metrics"))
    seconds = metrics.get("seconds", 0.0)
    return float(seconds) if isinstance(seconds, (int, float)) else 0.0


def _write_failure(
    journal: PipelineJournal,
    preview: ResourceEstimate,
    stage: str,
    error: BaseException,
    *,
    interrupted: bool,
) -> None:
    status = "interrupted" if interrupted else "failed"
    code = getattr(error, "code", type(error).__name__)
    error_detail: dict[str, object] = {
        "error_type": type(error).__name__,
        "code": str(code),
        "error_message": str(error),
    }
    report = RunReport(
        status=status,
        failed_stage=stage,
        object_count=preview.object_count,
        pair_count=preview.pair_count,
        effective_workers=preview.effective_workers,
        matrix_bytes=preview.matrix_bytes,
        required_free_disk_bytes=preview.required_free_disk_bytes,
        peak_rss_bytes=_peak_rss(),
        timings_seconds={
            name: _stage_seconds(journal, name)
            for name in ("normalizing", "distancing", "tree_building", "clusterizing")
        },
        error=error_detail,
    )
    atomic_json(journal.run_dir / "report.json", report.model_dump(mode="json"))
    journal.manifest.update(
        {
            "status": status,
            "updated_at": utc_now(),
            "failed_stage": stage,
            "stages": journal.receipts,
        }
    )
    atomic_json(journal.manifest_path, journal.manifest)
    logger.error(
        "run_interrupted" if interrupted else "run_failed",
        extra={"stage": stage, "error_type": type(error).__name__},
    )


# How a stage failure becomes a public failure: the stage's own base error selects the row,
# its code selects the class, and an unmapped code falls back to the stage's generic class.
# The four stage bases all accept a `code`, which `type[Exception]` would not express.
StageErrorType = (
    type[NormalizerError] | type[DistanceError] | type[TreeBuilderError] | type[ClusterizerError]
)

_STAGE_TRANSLATIONS: tuple[
    tuple[StageErrorType, dict[str, type[DamicoreError]], type[DamicoreError]], ...
] = (
    (
        NormalizerError,
        {
            "output_conflict_error": OutputDirectoryConflictError,
            "artifact_validation_error": ArtifactValidationError,
            "dataset_format_error": DatasetFormatError,
            "corpus_validation_error": InputValidationError,
            "input_validation_error": InputValidationError,
            "input_drift": InputValidationError,
        },
        NormalizationError,
    ),
    (
        DistanceError,
        {
            "checkpoint_mismatch_error": CheckpointMismatchError,
            "output_directory_conflict_error": OutputDirectoryConflictError,
            "artifact_validation_error": ArtifactValidationError,
            "compression_error": CompressionError,
            "distance_matrix_validation_error": DistanceMatrixValidationError,
        },
        DistanceComputationError,
    ),
    (
        TreeBuilderError,
        {
            "output_directory_conflict_error": OutputDirectoryConflictError,
            "artifact_validation_error": ArtifactValidationError,
            "tree_format_error": TreeFormatError,
        },
        TreeBuildError,
    ),
    (
        ClusterizerError,
        {
            "output_directory_conflict_error": OutputDirectoryConflictError,
            "tree_format_error": TreeFormatError,
        },
        ClusterizationError,
    ),
)

# A public code is the class name in snake_case, which DamicoreError already derives. A
# stage code must therefore never be forwarded, or the public code would report the stage's
# internal vocabulary instead of the raised class. input_drift is the single sanctioned
# specialization in 0.2.
_PRESERVED_CODES = frozenset({"input_drift"})


def _translated_stage_error(error: Exception, stage: str | None = None) -> DamicoreError:
    code = str(getattr(error, "code", ""))
    for base, by_code, fallback in _STAGE_TRANSLATIONS:
        if isinstance(error, base):
            translated = by_code.get(code, fallback)
            return translated(
                str(error),
                code=code if code in _PRESERVED_CODES else None,
                stage=stage,
            )
    return DamicoreError(str(error), stage=stage)


def _artifact_inventory(run_dir: Path) -> dict[str, dict[str, object]]:
    paths = [
        run_dir / "report.json",
        run_dir / "distance.npy",
        run_dir / "labels.json",
        run_dir / "tree.json",
        run_dir / "tree.nwk",
        run_dir / "membership.csv",
        run_dir / "clusters.json",
    ]
    for directory_name in ("checkpoints", "diagnostics", "normalization"):
        directory = run_dir / directory_name
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return {
        path.relative_to(run_dir).as_posix(): artifact_record(path, run_dir)
        for path in sorted(paths)
    }


def run(
    source: str | Path | Sequence[str | Path],
    *,
    source_kind: str = "delimited",
    split: str | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    sheet: str | None = None,
    recursive: bool | None = None,
    include_hidden: bool | None = None,
    compressor: str = "zlib",
    compression_level: int = 6,
    num_clusters: int | None = None,
    output_dir: str | Path | None = None,
    keep_normalized: bool = False,
    save_diagnostics: bool = False,
    progress: bool = True,
    execution: ExecutionConfig | None = None,
) -> DamicoreResult:
    """Execute, verify, and if possible resume the complete DAMICORE pipeline.

    Preflight, normalization, the exact NCD matrix, Neighbor Joining, and FastGreedy run in
    that order, and the artifacts they leave behind are cross-checked against one another
    before the run is marked ``completed``. Once the run directory exists, a failure or an
    interruption still writes ``report.json`` and updates ``manifest.json`` before the
    exception propagates, so the partial state stays diagnosable and, where the checkpoints
    allow it, resumable.

    The distance stage spawns worker processes, which re-import the calling module, whenever
    ``execution.workers`` resolves above ``1``; ``"auto"`` resolves from the CPU count, so
    only an explicit ``workers=1`` rules the pool out. A call at module level in a ``.py``
    script must therefore sit under ``if __name__ == "__main__":``; a notebook or REPL already
    satisfies this.

    Parameters
    ----------
    source
        A dataset file, a directory of files, or a sequence of file paths, according to
        ``source_kind``.
    source_kind
        Exactly ``"delimited"``, ``"xlsx"``, or ``"files"``. It decides where objects come
        from and therefore which of the remaining arguments apply; one that does not apply is
        a ``ConfigurationError`` rather than a silently ignored setting. It also enters the
        run identity, so the same corpus split differently is a different run.
    split
        Exactly ``"columns"`` or ``"rows"``, defaulting to ``"columns"``. It decides what one
        object is for a dataset, and therefore the object count that the resource gates are
        quadratic and cubic in. It does not apply to ``"files"``, where each file is one
        object already.
    sheet
        The worksheet to read from an ``"xlsx"`` source. Required when the workbook holds more
        than one, because defaulting to the first would silently decide which data was
        analyzed.
    recursive, include_hidden
        How a ``"files"`` source is enumerated. Both are recorded in the manifest, so a corpus
        can be reconstructed from the run rather than from the command that produced it.
    compressor
        Exactly ``"zlib"`` or ``"gzip"``. NCD values are compressor-dependent, so changing it
        changes the run identity and the results, not merely their cost.
    num_clusters
        ``None`` lets FastGreedy choose the cut. Otherwise the upper bound is the object count
        this source produces, which is only known after preflight, so an oversized value is
        rejected there rather than at call time.
    output_dir
        ``None`` writes to ``./damicore-results/<run_id>``, where ``run_id`` is derived from
        the input hash and the configuration, so repeating a call lands in the same directory
        and reuses or resumes it. An explicit directory is used as given; it may be absent,
        empty, or a compatible earlier run, and is never overwritten.
    keep_normalized
        Keeps ``normalization/`` in the run directory. Otherwise it is deleted once
        verification succeeds, since the objects are reproducible from the source.
    progress
        Renders a progress bar for the distance stage through ``tqdm``. Purely presentational.
    execution
        ``None`` uses the defaults: automatic worker count, resume and completed-run reuse
        enabled, and the default ``ResourceLimits``.

    Returns
    -------
    DamicoreResult
        The verified result, loaded through :func:`load_result`. It owns an open memory map
        of ``distance.npy``; call ``close()`` when finished with it.

    Raises
    ------
    ConfigurationError
        An argument or configuration value is invalid, including ``num_clusters`` above the
        object count. Raised before any run directory is created.
    InputValidationError
        An input path is not a readable regular file, a corpus breaks the corpus rules, or an
        input changed between preflight and materialization (code ``input_drift``).
    DatasetFormatError
        The dataset violates the input contract.
    ResourceLimitError
        Preflight projected the run outside ``execution.limits``.
    OutputDirectoryConflictError
        ``output_dir`` is not a directory, holds no readable DAMICORE manifest, belongs to a
        different input or configuration, or holds a compatible run that ``reuse_completed``
        or ``resume`` forbids continuing.
    CheckpointMismatchError
        An incomplete run in ``output_dir`` exists but cannot be trusted to resume.
    CompressionError
        The compressor rejected an object.
    DistanceComputationError
        The NCD stage failed, including a worker pool that died.
    DistanceMatrixValidationError
        The NCD stage's own check rejected the matrix it computed.
    NormalizationError, TreeBuildError, TreeFormatError, ClusterizationError
        The corresponding stage failed with no more specific cause. ``TreeBuildError`` also
        covers the tree stage rejecting the matrix it was given, which does not surface as
        ``DistanceMatrixValidationError``.
    ArtifactValidationError
        An artifact failed one of its checks, including the cross-artifact verification a run
        must pass before it is marked ``completed``.
    DamicoreError
        Any other failure inside the pipeline, named by the underlying exception type.
    KeyboardInterrupt
        Re-raised unchanged, after the run is recorded as interrupted.
    """
    settings = _execution(execution)
    object_source = _object_source(
        source_kind,
        split=split,
        delimiter=delimiter,
        encoding=encoding,
        sheet=sheet,
        recursive=recursive,
        include_hidden=include_hidden,
    )
    checked_compressor = _validated_compressor(compressor)
    normalization_config = _normalization_config(object_source, settings)
    try:
        distance_config = DistanceConfig(
            compressor=checked_compressor,
            compression_level=compression_level,
            compression_chunk_bytes=settings.compression_chunk_bytes,
            workers=settings.effective_workers,
            pairs_per_shard=settings.pairs_per_shard,
            resume=settings.resume,
            save_diagnostics=save_diagnostics,
        )
        cluster_config = ClusterConfig(num_clusters=num_clusters)
        tree_config = TreeBuildConfig()
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc

    target_hint = (
        Path(output_dir).resolve() if output_dir is not None else Path.cwd() / "damicore-results"
    )
    preview = preflight(
        source,
        object_source=object_source,
        save_diagnostics=save_diagnostics,
        execution=settings,
        disk_target=target_hint,
    )
    logger.info(
        "preflight_completed",
        extra={"object_count": preview.object_count, "pair_count": preview.pair_count},
    )
    if not preview.within_limits:
        raise ResourceLimitError(
            f"Resource limits exceeded: {', '.join(preview.violations)}. {_SCALE_GUIDANCE}",
            estimate=preview,
        )
    # The upper bound on num_clusters is part of the argument contract, but it is expressed
    # in leaves, so preflight is the earliest point that can decide it. Checking here keeps a
    # rejected argument from paying for normalization, the NCD matrix and the tree first.
    if num_clusters is not None and num_clusters > preview.object_count:
        raise ConfigurationError(
            f"num_clusters must be between 1 and the {preview.object_count} objects this "
            f"{source_kind} source produces; got {num_clusters}"
        )
    config = _config_payload(
        object_source=object_source,
        compressor=compressor,
        compression_level=compression_level,
        num_clusters=num_clusters,
        keep_normalized=keep_normalized,
        save_diagnostics=save_diagnostics,
        execution=settings,
    )
    config_hash, run_id = _identity(preview.input_sha256, config)
    run_dir = (
        Path(output_dir).resolve()
        if output_dir is not None
        else (Path.cwd() / "damicore-results" / run_id).resolve()
    )
    manifest_path = run_dir / "manifest.json"
    existing_manifest: dict[str, Any] | None = None
    # iterdir() on an existing non-directory raises NotADirectoryError, which is neither a
    # public error nor a documented exit code, so the type is checked before it is walked.
    if run_dir.exists() and not run_dir.is_dir():
        raise OutputDirectoryConflictError(f"Output path is not a directory: {run_dir}")
    if run_dir.exists() and any(run_dir.iterdir()):
        try:
            existing_manifest = RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            ).model_dump(mode="json")
        except (OSError, ValidationError) as exc:
            raise OutputDirectoryConflictError(
                "Output directory has no readable DAMICORE manifest"
            ) from exc
        compatible = (
            existing_manifest.get("input", {}).get("sha256") == preview.input_sha256
            and existing_manifest.get("config_hash") == config_hash
            and existing_manifest.get("schema_version") == SCHEMA_VERSION
        )
        if not compatible:
            raise OutputDirectoryConflictError("Output directory belongs to another run")
        if existing_manifest.get("status") == "completed":
            if settings.reuse_completed:
                return load_result(run_dir)
            raise OutputDirectoryConflictError("Completed output reuse is disabled")
        if not settings.resume:
            raise OutputDirectoryConflictError("Incomplete output resume is disabled")
        recorded_runtime = {
            key: str(item) for key, item in json_mapping(existing_manifest.get("runtime")).items()
        }
        if not recorded_runtime or resume_fingerprint(recorded_runtime) != resume_fingerprint(
            runtime_fingerprint()
        ):
            raise CheckpointMismatchError(
                "Incomplete run was created by a different runtime fingerprint"
            )
        logger.info("resume_started", extra={"run_id": run_id})
    else:
        run_dir.mkdir(parents=True, exist_ok=True)

    created_at = utc_now()
    manifest = existing_manifest or {
        "schema_version": SCHEMA_VERSION,
        "damicore_version": VERSION,
        "run_id": run_id,
        "status": "created",
        "created_at": created_at,
        "updated_at": created_at,
        "completed_at": None,
        "run_dir": str(run_dir),
        "input": {
            "kind": preview.source_kind,
            "paths": [str(path) for path in preview.source_paths],
            "size_bytes": preview.input_size_bytes,
            "sha256": preview.input_sha256,
        },
        "config": config,
        "config_hash": config_hash,
        "estimate": preview.model_dump(mode="json"),
        "runtime": runtime_fingerprint(),
        "stages": {},
        "artifacts": {},
        "warnings": [],
    }
    atomic_json(manifest_path, manifest)
    journal = PipelineJournal(run_dir, manifest)
    logger.info("run_started", extra={"run_id": run_id})
    journal.transition("preflighted")
    current_stage = "normalizing"
    try:
        normalization_dir = run_dir / "normalization"
        if journal.reusable("normalizing"):
            normalization = _load_normalization(normalization_dir / "manifest.json")
        else:
            if normalization_dir.exists():
                shutil.rmtree(normalization_dir)
            started = journal.stage_started("normalizing", list(preview.source_paths))
            # The caller's own argument, not the file list preflight expanded from it. A
            # corpus labels its objects relative to what was requested, so handing back the
            # expanded files would re-derive that root from the files themselves: a corpus
            # whose files all sit in one subdirectory would be labelled without it, and the
            # set digest -- which covers labels -- would then disagree with preflight's.
            normalization = materialize_objects(
                source,
                normalization_dir,
                config=normalization_config,
            )
            normalization_manifest = NormalizationManifest.model_validate_json(
                normalization.manifest_path.read_text(encoding="utf-8")
            )
            if (
                normalization.total_bytes != preview.normalized_bytes
                or normalization_manifest.input.sha256 != preview.input_sha256
                or normalization_manifest.input.size_bytes != preview.input_size_bytes
            ):
                raise InputValidationError(
                    "Preflight and normalization byte counts differ",
                    code="input_drift",
                )
            outputs = [normalization.manifest_path]
            outputs.extend(normalization_dir / item.relative_path for item in normalization.objects)
            journal.stage_completed(
                "normalizing",
                started,
                outputs,
                {
                    "object_count": normalization.object_count,
                    "normalized_bytes": normalization.total_bytes,
                },
            )

        current_stage = "distancing"
        if journal.reusable("distancing"):
            # The matrix range is recorded as stage metrics rather than remeasured: the
            # resume fingerprint pins the damicore version, so a receipt this branch accepts
            # was written by this same build and always carries them.
            metrics = journal.receipts["distancing"]["metrics"]
            distance = DistanceResult(
                matrix_path=run_dir / "distance.npy",
                labels_path=run_dir / "labels.json",
                object_count=preview.object_count,
                pair_count=preview.pair_count,
                timing=_stage_seconds(journal, "distancing"),
                ncd_min=float(metrics["ncd_min"]),
                ncd_max=float(metrics["ncd_max"]),
                ncd_out_of_range_count=int(metrics["ncd_out_of_range_count"]),
            )
        else:
            for stale in (run_dir / "tree.json", run_dir / "tree.nwk", run_dir / "tree-work.npy"):
                if stale.exists():
                    stale.unlink()
            started = journal.stage_started(
                "distancing",
                [normalization.manifest_path],
            )
            callback, close_progress = distance_progress(progress)
            try:
                distance = compute_distance_matrix(
                    normalization.manifest_path,
                    run_dir,
                    config=distance_config,
                    progress=callback,
                )
            finally:
                close_progress()
            distance_outputs = [
                distance.matrix_path,
                distance.labels_path,
                run_dir / "checkpoints" / "compressed-sizes.json",
                run_dir / "checkpoints" / "distance-shards.json",
            ]
            diagnostics_dir = run_dir / "diagnostics"
            if diagnostics_dir.is_dir():
                distance_outputs.extend(
                    path for path in diagnostics_dir.iterdir() if path.is_file()
                )
            journal.stage_completed(
                "distancing",
                started,
                distance_outputs,
                {
                    "object_count": distance.object_count,
                    "pair_count": distance.pair_count,
                    "ncd_min": distance.ncd_min,
                    "ncd_max": distance.ncd_max,
                    "ncd_out_of_range_count": distance.ncd_out_of_range_count,
                },
            )

        current_stage = "tree_building"
        if journal.reusable("tree_building"):
            tree = TreeBuildResult(
                tree_path=run_dir / "tree.json",
                newick_path=run_dir / "tree.nwk",
                leaf_count=preview.object_count,
                negative_branch_count=int(
                    journal.receipts["tree_building"]["metrics"]["negative_branch_count"]
                ),
                timing=_stage_seconds(journal, "tree_building"),
            )
        else:
            started = journal.stage_started(
                "tree_building",
                [distance.matrix_path, distance.labels_path],
            )
            tree = build_tree(
                distance.matrix_path,
                distance.labels_path,
                run_dir,
                config=tree_config,
            )
            journal.stage_completed(
                "tree_building",
                started,
                [tree.tree_path, tree.newick_path],
                {
                    "leaf_count": tree.leaf_count,
                    "negative_branch_count": tree.negative_branch_count,
                },
            )

        current_stage = "clusterizing"
        if journal.reusable("clusterizing"):
            metrics = journal.receipts["clusterizing"]["metrics"]
            clustered = ClusterResult(
                membership_path=run_dir / "membership.csv",
                clusters_path=run_dir / "clusters.json",
                community_count=int(metrics["community_count"]),
                cluster_count=int(metrics["cluster_count"]),
                modularity=float(metrics["modularity"]),
                timing=_stage_seconds(journal, "clusterizing"),
            )
        else:
            for stale in (run_dir / "membership.csv", run_dir / "clusters.json"):
                if stale.exists():
                    stale.unlink()
            started = journal.stage_started("clusterizing", [tree.tree_path])
            clustered = cluster_tree(tree.tree_path, run_dir, config=cluster_config)
            journal.stage_completed(
                "clusterizing",
                started,
                [clustered.membership_path, clustered.clusters_path],
                {
                    "community_count": clustered.community_count,
                    "cluster_count": clustered.cluster_count,
                    "modularity": clustered.modularity,
                },
            )

        current_stage = "verifying"
        journal.transition("verifying")
        verification = _verify_cross_artifacts(
            run_dir,
            normalization,
            num_clusters,
            clustered.community_count,
        )
        if not keep_normalized:
            shutil.rmtree(normalization_dir)

        timings = {
            stage: _stage_seconds(journal, stage)
            for stage in ("normalizing", "distancing", "tree_building", "clusterizing")
        }
        timings["total"] = sum(timings.values())
        report = RunReport(
            status="completed",
            object_count=preview.object_count,
            pair_count=preview.pair_count,
            community_count=clustered.community_count,
            cluster_count=clustered.cluster_count,
            effective_workers=settings.effective_workers,
            csv_chunk_rows=settings.csv_chunk_rows,
            compression_chunk_bytes=settings.compression_chunk_bytes,
            pairs_per_shard=settings.pairs_per_shard,
            matrix_bytes=preview.matrix_bytes,
            required_free_disk_bytes=preview.required_free_disk_bytes,
            peak_rss_bytes=_peak_rss(),
            ncd_min=distance.ncd_min,
            ncd_max=distance.ncd_max,
            ncd_out_of_range_count=distance.ncd_out_of_range_count,
            negative_branch_count=tree.negative_branch_count,
            modularity=clustered.modularity,
            timings_seconds=timings,
            verification=verification,
        )
        atomic_json(run_dir / "report.json", report.model_dump(mode="json"))
        manifest.update(
            {
                "status": "completed",
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "objects": [
                    item.model_dump(mode="json", exclude={"relative_path"})
                    for item in normalization.objects
                ],
                "stages": journal.receipts,
                "cleanup_completed": not keep_normalized,
                "artifacts": _artifact_inventory(run_dir),
            }
        )
        atomic_json(manifest_path, manifest)
        logger.info("verification_completed", extra={"run_id": run_id})
        logger.info("run_completed", extra={"run_id": run_id})
        return load_result(run_dir)
    except KeyboardInterrupt as exc:
        _write_failure(
            journal,
            preview,
            current_stage,
            exc,
            interrupted=True,
        )
        raise
    except DamicoreError as exc:
        logger.error("run_failed", extra={"run_id": run_id, "stage": current_stage})
        _write_failure(journal, preview, current_stage, exc, interrupted=False)
        raise
    except (NormalizerError, DistanceError, TreeBuilderError, ClusterizerError) as exc:
        translated = _translated_stage_error(exc, current_stage)
        _write_failure(journal, preview, current_stage, translated, interrupted=False)
        logger.error("stage_failed", extra={"run_id": run_id, "stage": current_stage})
        raise translated from exc
    except Exception as exc:
        # Name the underlying failure: without it a MemoryError and an OSError produce
        # byte-identical report.json files, and the report is all a user has after the fact.
        translated = DamicoreError(
            f"Pipeline failed during {current_stage}: {type(exc).__name__}: {exc}"
        )
        _write_failure(journal, preview, current_stage, translated, interrupted=False)
        raise translated from exc


def load_result(output_dir: str | Path) -> DamicoreResult:
    """Load and verify a completed DAMICORE result without executing artifact code.

    Every artifact the manifest declares is re-checked against its recorded size and SHA-256,
    and any entry that is absolute or resolves outside the run directory is rejected before it
    is read. Containment is the rule, not file kind: an entry is resolved before it is checked,
    so a link and the file it points at are one path by then -- a link out of the directory is
    refused because its target is outside, and one within it is read like any other artifact,
    its bytes still held to the recorded digest. The matrix is memory-mapped with
    ``allow_pickle=False``, so no artifact can execute code on load. Only runs whose manifest
    and report both say ``completed``, at the current schema version, can be loaded; a failed
    or interrupted run is diagnostic only.

    Returns
    -------
    DamicoreResult
        A fresh result that owns its own open memory map of ``distance.npy``. Every call
        produces an independent one, and each has to be closed by whoever received it.

    Raises
    ------
    ArtifactValidationError
        The only public failure of this function: a missing, unreadable, incomplete, or
        wrong-version manifest or report; a hash or size mismatch; a path that escapes the
        run directory; or a membership, cluster, or Newick artifact that does not parse.
    """
    run_dir = Path(output_dir).resolve()
    paths = artifact_paths(run_dir)
    try:
        manifest = RunManifest.model_validate_json(paths.manifest.read_text(encoding="utf-8"))
        if manifest.status != "completed" or manifest.schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"Only completed schema-v{SCHEMA_VERSION} runs can be loaded"
            )
        for record in manifest.artifacts.values():
            relative = Path(record.path)
            candidate = (run_dir / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not candidate.is_relative_to(run_dir)
            ):
                raise ArtifactValidationError("Artifact path escapes the run directory")
            if (
                candidate.stat().st_size != record.size_bytes
                or sha256_file(candidate) != record.sha256
            ):
                raise ArtifactValidationError("Artifact hash or size mismatch")
        report = RunReport.model_validate_json(paths.report.read_text(encoding="utf-8"))
        if report.status != "completed":
            raise ArtifactValidationError("Result report is not completed")
        labels_payload = LabelsArtifact.model_validate_json(
            paths.labels.read_text(encoding="utf-8")
        )
        membership = pd.read_csv(
            paths.membership,
            dtype={"object_id": "string", "label": "string", "cluster": "int64"},
            keep_default_na=False,
            na_filter=False,
        )
        if list(membership.columns) != ["object_id", "label", "cluster"]:
            raise ArtifactValidationError("membership.csv columns are invalid")
        cluster_payload = ClustersArtifact.model_validate_json(
            paths.clusters.read_text(encoding="utf-8")
        )
        clusters = {item.cluster: list(item.labels) for item in cluster_payload.clusters}
        tree_newick = paths.tree_newick.read_text(encoding="utf-8").strip()
        if not tree_newick.endswith(";"):
            raise ArtifactValidationError("Newick artifact does not end with semicolon")
    except DamicoreError:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise ArtifactValidationError("Could not load completed result") from exc
    view = DistanceMatrixView(
        paths.distance_matrix,
        list(labels_payload.labels),
        materialization_limit_bytes=manifest.config.pandas_materialization_limit_bytes,
        materialization_error=MaterializationError,
    )
    return DamicoreResult(
        membership=membership,
        clusters=clusters,
        tree_newick=tree_newick,
        distance_matrix=view,
        report=report,
        artifacts=paths,
    )
