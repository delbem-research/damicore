from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from damicore.estimate import ResourceEstimate


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalArtifactRecord(ArtifactRecord):
    @field_validator("path")
    @classmethod
    def _path_is_contained(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or not path.parts or ".." in path.parts or path.as_posix() != value:
            raise ValueError("artifact path must be a contained POSIX path")
        return value


class ManifestObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    object_id: str
    label: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunInput(BaseModel):
    """What this run read. ``paths`` is a tuple because a corpus has no single input file,
    and ``sha256`` is that whole input's digest: one file's for a dataset, and a digest over
    every adopted file for a corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["delimited", "xlsx", "files"]
    paths: tuple[str, ...]
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManifestResourceLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    max_objects: int = Field(gt=0)
    max_pairs: int = Field(gt=0)
    max_matrix_bytes: int = Field(gt=0)
    max_working_memory_bytes: int = Field(gt=0)
    required_free_disk_factor: float = Field(ge=1.0, allow_inf_nan=False)


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    # Conditional on the source rather than universal: a corpus has no split, delimiter, or
    # encoding, and a workbook declares neither delimiter nor encoding. `None` records that
    # the setting does not apply, which is what keeps the configuration hash honest.
    source_kind: Literal["delimited", "xlsx", "files"]
    split: Literal["columns", "rows"] | None
    delimiter: str | None = Field(min_length=1, max_length=1)
    encoding: str | None
    sheet: str | None
    recursive: bool | None
    include_hidden: bool | None
    compressor: Literal["zlib", "gzip"]
    compression_level: int = Field(ge=0, le=9)
    num_clusters: int | None = Field(default=None, ge=1)
    keep_normalized: bool
    save_diagnostics: bool
    workers: int = Field(gt=0)
    csv_chunk_rows: int = Field(gt=0)
    compression_chunk_bytes: int = Field(gt=0)
    pairs_per_shard: int = Field(gt=0)
    pandas_materialization_limit_bytes: int = Field(gt=0)
    limits: ManifestResourceLimits


class StageReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: Literal["running", "completed"]
    started_at: str
    finished_at: str | None
    runtime: dict[str, str]
    inputs: tuple[ArtifactRecord, ...]
    outputs: tuple[ArtifactRecord, ...]
    metrics: dict[str, int | float | str | bool]


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[2]
    damicore_version: str
    run_id: str
    status: Literal[
        "created",
        "preflighted",
        "normalizing",
        "distancing",
        "tree_building",
        "clusterizing",
        "verifying",
        "completed",
        "failed",
        "interrupted",
    ]
    created_at: str
    updated_at: str
    completed_at: str | None
    run_dir: str
    input: RunInput
    config: RunConfig
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimate: ResourceEstimate
    runtime: dict[str, str]
    stages: dict[str, StageReceipt]
    artifacts: dict[str, FinalArtifactRecord]
    warnings: tuple[str, ...]
    objects: tuple[ManifestObject, ...] = ()
    cleanup_completed: bool | None = None
    failed_stage: str | None = None

    @model_validator(mode="after")
    def _completed_manifest_is_total(self) -> Self:
        if any(key != record.path for key, record in self.artifacts.items()):
            raise ValueError("artifact inventory keys must equal record paths")
        if self.status == "completed":
            required = {
                "report.json",
                "distance.npy",
                "labels.json",
                "tree.json",
                "tree.nwk",
                "membership.csv",
                "clusters.json",
                "checkpoints/compressed-sizes.json",
                "checkpoints/distance-shards.json",
            }
            if not required.issubset(self.artifacts):
                raise ValueError("completed manifest does not declare every required artifact")
            if self.completed_at is None or len(self.objects) < 2:
                raise ValueError("completed manifest lacks completion metadata or objects")
        return self


class LabelsArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    object_ids: tuple[str, ...]
    labels: tuple[str, ...]


class ClusterArtifactItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cluster: int = Field(ge=0)
    object_ids: tuple[str, ...]
    labels: tuple[str, ...]


class ClustersArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    clusters: tuple[ClusterArtifactItem, ...]


def sha256_file(path: Path, chunk_size: int = 4_194_304) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def artifact_record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def json_mapping(value: object) -> dict[str, object]:
    """Narrow a decoded JSON value to a string-keyed mapping, or an empty one.

    Deserialized JSON carries no static shape, so every reader of a manifest, receipt, or
    checkpoint would otherwise re-derive `Unknown` from `dict[str, Any]`. This is the single
    place that crosses from `object` into a typed mapping, which keeps the readers checked
    and confines the one unavoidable cast to a premise `isinstance` just established.
    """
    if not isinstance(value, dict):
        return {}
    entries = cast(Mapping[object, object], value)
    return {str(key): item for key, item in entries.items()}


def json_sequence(value: object) -> list[object]:
    """Narrow a decoded JSON value to a list, or an empty one. See `json_mapping`."""
    if not isinstance(value, list):
        return []
    return list(cast(Sequence[object], value))
