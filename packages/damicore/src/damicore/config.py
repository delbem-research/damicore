from __future__ import annotations

import os
from typing import Literal

from damicore_distance import DistanceConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class ResourceLimits(BaseModel):
    """The gates preflight applies before a run starts, sized for the exact 0.2 algorithm.

    Raise one only after reading an ``estimate()``: NCD is quadratic and Neighbor Joining
    cubic in the object count, so a limit lifted without that check buys a run that does not
    finish. Two gates are less visible than the object, pair, and matrix caps.
    ``max_working_memory_bytes`` bounds the peak RAM the distance stage is projected to need
    from the largest serialized chunk, the worker count, the shard size, and the PPMd model
    each worker holds, charged at its configured size.
    ``required_free_disk_factor`` is the headroom multiplier applied to the projected artifact
    bytes; the free-disk check it produces is enforced no matter how the limits are set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_objects: int = Field(default=1_000, gt=0)
    max_pairs: int = Field(default=500_000, gt=0)
    max_matrix_bytes: int = Field(default=536_870_912, gt=0)
    max_working_memory_bytes: int = Field(default=536_870_912, gt=0)
    required_free_disk_factor: float = Field(default=1.25, ge=1.0)


class ExecutionConfig(BaseModel):
    """How a run is executed, as opposed to what it computes.

    These settings change cost and restart behavior, not the clustering: the pipeline is
    exact and deterministic under any of them. They do change run identity -- every field
    except ``resume`` and ``reuse_completed`` enters the configuration hash that names the
    default output directory, so ``workers="auto"`` resolving differently on another machine
    produces a different run rather than resuming the existing one. ``pairs_per_shard="auto"``
    resolves from the pair count alone, so it names the same run on every machine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workers: int | Literal["auto"] = "auto"
    csv_chunk_rows: int = Field(default=50_000, gt=0)
    compression_chunk_bytes: int = Field(default=4_194_304, gt=0)
    pairs_per_shard: int | Literal["auto"] = "auto"
    resume: bool = True
    reuse_completed: bool = True
    pandas_materialization_limit_bytes: int = Field(default=268_435_456, gt=0)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @field_validator("workers", "pairs_per_shard")
    @classmethod
    def _validate_positive_or_auto(cls, value: int | str, info: ValidationInfo) -> int | str:
        if isinstance(value, int) and value < 1:
            raise ValueError(f"{info.field_name} must be at least one")
        return value

    @property
    def effective_workers(self) -> int:
        if isinstance(self.workers, int):
            return self.workers
        return min(4, max(1, (os.cpu_count() or 1) - 1))

    def effective_pairs_per_shard(self, pair_count: int) -> int:
        # The distance stage owns the rule, and it is the stage that lays the shards out:
        # resolving through it keeps what preflight projects and the manifest records equal
        # to what the stage runs.
        return DistanceConfig(pairs_per_shard=self.pairs_per_shard).effective_pairs_per_shard(
            pair_count
        )
