from __future__ import annotations

import os
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

# One shard occupies one worker, so the pairs are spread over at least this many shards: a
# run of few, large objects has few pairs, and a fixed shard size left it in a single shard
# that only one worker could run. Dividing by a count rather than fixing a size also keeps
# the shard count -- and the checkpoint, rewritten after every shard -- below twice this
# count until the cap, which bounds the result a worker holds for one shard.
_AUTO_SHARD_COUNT = 64
_MAX_AUTO_PAIRS_PER_SHARD = 10_000

# The settings each compressor reads. One set for a compressor it does not apply to is
# refused rather than ignored: a caller who wrote it believes it took effect.
_SETTINGS: dict[str, frozenset[str]] = {
    "zlib": frozenset({"compression_level"}),
    "gzip": frozenset({"compression_level"}),
    "ppmd": frozenset({"ppmd_order", "ppmd_memory_bytes"}),
    "bzip2": frozenset({"bzip2_block_size_100k"}),
    "lzma": frozenset({"lzma_dictionary_bytes"}),
}


class DistanceConfig(BaseModel):
    """What the distance stage measures with, and how it schedules the work.

    ``compressor`` and its settings change every NCD value. ``compression_level`` applies
    to ``"zlib"`` and ``"gzip"``; ``ppmd_order`` and ``ppmd_memory_bytes`` to ``"ppmd"``,
    PPMd variant I with model restart; ``bzip2_block_size_100k`` to ``"bzip2"``; and
    ``lzma_dictionary_bytes`` to ``"lzma"``, raw LZMA2 at preset 6. Each bound is the codec's
    own. See ADR 0020 for the default, ADR 0021 for bzip2 and LZMA, and ``docs/method/ncd.md``
    for what each compressor computes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    compressor: Literal["zlib", "gzip", "ppmd", "bzip2", "lzma"] = "ppmd"
    compression_level: int = Field(default=6, ge=0, le=9)
    ppmd_order: int = Field(default=6, ge=2, le=64)
    ppmd_memory_bytes: int = Field(default=67_108_864, ge=2_048, le=4_294_967_259)
    bzip2_block_size_100k: int = Field(default=9, ge=1, le=9)
    lzma_dictionary_bytes: int = Field(default=8_388_608, ge=4_096, le=1_610_612_736)
    compression_chunk_bytes: int = Field(default=4_194_304, gt=0)
    workers: int | Literal["auto"] = "auto"
    pairs_per_shard: int | Literal["auto"] = "auto"
    resume: bool = True
    save_diagnostics: bool = False

    @field_validator("workers", "pairs_per_shard")
    @classmethod
    def _validate_positive_or_auto(cls, value: int | str, info: ValidationInfo) -> int | str:
        if isinstance(value, int) and value < 1:
            raise ValueError(f"{info.field_name} must be at least one")
        return value

    @model_validator(mode="after")
    def _settings_apply_to_the_compressor(self) -> Self:
        foreign = sorted(
            (self.model_fields_set & frozenset().union(*_SETTINGS.values()))
            - _SETTINGS[self.compressor]
        )
        if foreign:
            raise ValueError(f"{', '.join(foreign)} does not apply to {self.compressor}")
        return self

    @property
    def compressor_settings(self) -> dict[str, int]:
        """The settings ``compressor`` reads, by name, with the values in force."""
        return {name: getattr(self, name) for name in sorted(_SETTINGS[self.compressor])}

    @property
    def stream_memory_bytes(self) -> int:
        """The memory one compressor stream can hold, which every worker holds one of.

        Stated, not measured per run: a stream fills to it on incompressible data. ADR 0021
        records the measurements each figure is checked against.
        """
        match self.compressor:
            case "zlib" | "gzip":
                # zlib.h: (1 << (windowBits + 2)) + (1 << (memLevel + 9)) at 15 and 8.
                return 262_144
            case "ppmd":
                return self.ppmd_memory_bytes
            case "bzip2":
                # The measured peak at the largest block, 9.5 MiB, charged at every block size.
                return 10_485_760
            case "lzma":
                # Above every measured peak from a 4 KiB to a 64 MiB dictionary at preset 6.
                return 12 * self.lzma_dictionary_bytes + 4_194_304

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
