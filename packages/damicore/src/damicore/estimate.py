from __future__ import annotations

import math
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from damicore_normalizer import NormalizationConfig, NormalizerError
from damicore_normalizer.api import scan_source
from damicore_normalizer.config import ObjectSource
from pydantic import BaseModel, ConfigDict, Field

from damicore.config import ExecutionConfig
from damicore.errors import DatasetFormatError, InputValidationError


class ResourceEstimate(BaseModel):
    """What a run of this source under this configuration would cost, measured before it starts.

    The counts and the input and object byte totals come from the same traversal the run
    itself performs, so they are exact rather than sampled; the memory and disk figures are
    conservative projections, and free disk is the one number read from the live filesystem.
    ``within_limits`` is ``False`` exactly when ``violations`` is non-empty, and ``violations``
    names the gates in a fixed order. ``estimate()`` returns this either way; ``run()`` turns
    a non-empty ``violations`` into ``ResourceLimitError``.

    ``input_sha256`` identifies the whole input: one file's digest for a dataset, and a digest
    over every adopted file for a corpus, since no single file identifies that run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    source_kind: Literal["delimited", "xlsx", "files"]
    source_paths: tuple[Path, ...]
    input_sha256: str
    input_size_bytes: int = Field(ge=0)
    split: Literal["columns", "rows"] | None
    object_count: int = Field(ge=0)
    pair_count: int = Field(ge=0)
    effective_workers: int = Field(gt=0)
    matrix_bytes: int = Field(ge=0)
    # Equal to matrix_bytes by construction. Not a bug and not removable before a breaking
    # release; see ADR 0011.
    tree_workspace_bytes: int = Field(ge=0)
    normalized_bytes: int = Field(ge=0)
    max_serialized_chunk_bytes: int = Field(ge=0)
    estimated_working_memory_bytes: int = Field(ge=0)
    estimated_final_metadata_bytes: int = Field(ge=0)
    estimated_diagnostic_bytes: int = Field(ge=0)
    estimated_artifact_bytes: int = Field(ge=0)
    required_free_disk_bytes: int = Field(ge=0)
    available_free_disk_bytes: int = Field(ge=0)
    within_limits: bool
    violations: list[str]


def _fingerprints(paths: Sequence[Path]) -> tuple[tuple[int, int], ...]:
    stats = [path.stat() for path in paths]
    return tuple((item.st_size, item.st_mtime_ns) for item in stats)


def preflight(
    source: str | Path | Sequence[str | Path],
    *,
    object_source: ObjectSource,
    save_diagnostics: bool,
    execution: ExecutionConfig,
    disk_target: Path,
) -> ResourceEstimate:
    """Project a run's exact cost by performing the run's own traversal without writing.

    The scan is the one the normalizer will repeat, so the object count and byte totals here
    are the ones the run produces rather than a second estimate of them.
    """
    try:
        scan = scan_source(
            source,
            NormalizationConfig(source=object_source, chunk_rows=execution.csv_chunk_rows),
        )
    except NormalizerError as exc:
        error_type = (
            DatasetFormatError if exc.code == "dataset_format_error" else InputValidationError
        )
        raise error_type(str(exc), code=exc.code, stage="preflight") from exc
    try:
        if _fingerprints(scan.source_paths) != scan.source_fingerprints:
            raise InputValidationError("Input changed during preflight", code="input_drift")
    except OSError as exc:
        raise InputValidationError(
            "Input disappeared during preflight", code="input_drift"
        ) from exc

    objects = len(scan.objects)
    pairs = objects * (objects - 1) // 2
    matrix_bytes = objects * objects * 8
    checkpoint_bytes = math.ceil(pairs / execution.pairs_per_shard) * 256
    label_bytes = sum(len(item.label.encode("utf-8")) for item in scan.objects)
    metadata_bytes = 1_048_576 + objects * 4_096 + 8 * label_bytes
    diagnostic_bytes = (
        objects * objects * 32 + pairs * 96 + 2 * label_bytes if save_diagnostics else 0
    )
    artifact_bytes = (
        scan.total_bytes + 2 * matrix_bytes + checkpoint_bytes + metadata_bytes + diagnostic_bytes
    )
    required_disk = math.ceil(artifact_bytes * execution.limits.required_free_disk_factor)
    working_memory = max(
        6 * scan.max_serialized_chunk_bytes,
        execution.effective_workers * 2 * execution.compression_chunk_bytes
        + execution.pairs_per_shard * 24,
    )
    existing = disk_target
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    available = shutil.disk_usage(existing).free
    checks = [
        (objects > execution.limits.max_objects, "max_objects"),
        (pairs > execution.limits.max_pairs, "max_pairs"),
        (matrix_bytes > execution.limits.max_matrix_bytes, "max_matrix_bytes"),
        (working_memory > execution.limits.max_working_memory_bytes, "max_working_memory_bytes"),
        (required_disk > available, "free_disk"),
    ]
    violations = [name for failed, name in checks if failed]
    manifest_input = scan.manifest_input
    return ResourceEstimate(
        source_kind=manifest_input.kind,
        source_paths=scan.source_paths,
        input_sha256=manifest_input.sha256,
        input_size_bytes=manifest_input.size_bytes,
        split=None if manifest_input.kind == "files" else manifest_input.split,
        object_count=objects,
        pair_count=pairs,
        effective_workers=execution.effective_workers,
        matrix_bytes=matrix_bytes,
        tree_workspace_bytes=matrix_bytes,
        normalized_bytes=scan.total_bytes,
        max_serialized_chunk_bytes=scan.max_serialized_chunk_bytes,
        estimated_working_memory_bytes=working_memory,
        estimated_final_metadata_bytes=metadata_bytes,
        estimated_diagnostic_bytes=diagnostic_bytes,
        estimated_artifact_bytes=artifact_bytes,
        required_free_disk_bytes=required_disk,
        available_free_disk_bytes=available,
        within_limits=not violations,
        violations=violations,
    )
