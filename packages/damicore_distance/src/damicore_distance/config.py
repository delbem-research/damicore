from __future__ import annotations

import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# One shard occupies one worker, so the pairs are spread over at least this many shards: a
# run of few, large objects has few pairs, and a fixed shard size left it in a single shard
# that only one worker could run. Dividing by a count rather than fixing a size also keeps
# the shard count -- and the checkpoint, rewritten after every shard -- below twice this
# count until the cap, which bounds the result a worker holds for one shard.
_AUTO_SHARD_COUNT = 64
_MAX_AUTO_PAIRS_PER_SHARD = 10_000


class DistanceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    compressor: Literal["zlib", "gzip"] = "zlib"
    compression_level: int = Field(default=6, ge=0, le=9)
    compression_chunk_bytes: int = Field(default=4_194_304, gt=0)
    workers: int | Literal["auto"] = "auto"
    pairs_per_shard: Annotated[int, Field(gt=0)] | Literal["auto"] = "auto"
    resume: bool = True
    save_diagnostics: bool = False

    @field_validator("workers")
    @classmethod
    def _validate_workers(cls, value: int | str) -> int | str:
        if isinstance(value, int) and value < 1:
            raise ValueError("workers must be at least one")
        return value

    @property
    def effective_workers(self) -> int:
        if isinstance(self.workers, int):
            return self.workers
        return min(4, max(1, (os.cpu_count() or 1) - 1))

    def effective_pairs_per_shard(self, pair_count: int) -> int:
        """Return the shard size for ``pair_count`` pairs.

        ``"auto"`` depends on the pair count alone, never on the machine, so the shard layout
        a checkpoint records stays valid when a run resumes elsewhere.
        """
        if isinstance(self.pairs_per_shard, int):
            return self.pairs_per_shard
        return min(_MAX_AUTO_PAIRS_PER_SHARD, max(1, pair_count // _AUTO_SHARD_COUNT))
