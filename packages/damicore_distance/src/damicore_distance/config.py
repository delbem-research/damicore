from __future__ import annotations

import os
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

# One shard occupies one worker, so the pairs are spread over at least this many shards: a
# run of few, large objects has few pairs, and a fixed shard size left it in a single shard
# that only one worker could run. Dividing by a count rather than fixing a size also keeps
# the shard count -- and the checkpoint, rewritten after every shard -- below twice this
# count until the cap, which bounds the result a worker holds for one shard.
_AUTO_SHARD_COUNT = 64
_MAX_AUTO_PAIRS_PER_SHARD = 10_000


class DeflateCompressor(BaseModel):
    """DEFLATE through ``zlib``: ``"zlib"`` in the zlib container, ``"gzip"`` in the gzip one.

    Both see a 32 KiB window, the most either format allows: past it, the first object of a
    pair is out of view while the second is compressed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["zlib", "gzip"] = "zlib"
    compression_level: int = Field(default=6, ge=0, le=9)

    @property
    def stream_memory_bytes(self) -> int:
        """The memory one stream is charged, once per worker, by preflight.

        Stated rather than measured per run: a stream fills to it on incompressible data
        [adr-0021].
        """
        # zconf.h: (1 << (windowBits + 2)) + (1 << (memLevel + 9)) at 15 and 8; the few
        # kilobytes it adds for small objects are not charged.
        return 262_144


class PpmdCompressor(BaseModel):
    """PPMd variant I with model restart, at ``ppmd_order`` with ``ppmd_memory_bytes`` of
    model [adr-0020]. The bounds are the codec's own: pyppmd would clamp a value outside them
    without saying so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["ppmd"] = "ppmd"
    ppmd_order: int = Field(default=6, ge=2, le=64)
    ppmd_memory_bytes: int = Field(default=67_108_864, ge=2_048, le=4_294_967_259)

    @property
    def stream_memory_bytes(self) -> int:
        """The memory one stream is charged, once per worker, by preflight.

        Stated rather than measured per run: a stream fills to it on incompressible data
        [adr-0021].
        """
        return self.ppmd_memory_bytes


class Bzip2Compressor(BaseModel):
    """bzip2 at ``bzip2_block_size_100k``; the block is its window [adr-0021]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["bzip2"] = "bzip2"
    bzip2_block_size_100k: int = Field(default=9, ge=1, le=9)

    @property
    def stream_memory_bytes(self) -> int:
        """The memory one stream is charged, once per worker, by preflight.

        Stated rather than measured per run: a stream fills to it on incompressible data
        [adr-0021].
        """
        # The measured peak at the largest block, 9.5 MiB, charged at every block size.
        return 10_485_760


class LzmaCompressor(BaseModel):
    """Raw LZMA2 at preset 6 with ``lzma_dictionary_bytes``; the dictionary is its window
    [adr-0021]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["lzma"] = "lzma"
    lzma_dictionary_bytes: int = Field(default=8_388_608, ge=4_096, le=1_610_612_736)

    @property
    def stream_memory_bytes(self) -> int:
        """The memory one stream is charged, once per worker, by preflight.

        Stated rather than measured per run: a stream fills to it on incompressible data
        [adr-0021].
        """
        # Above every measured peak from a 4 KiB to a 64 MiB dictionary at preset 6.
        return 12 * self.lzma_dictionary_bytes + 4_194_304


# Each compressor is its own model, so a setting it does not read is not a field to write:
# refused by name, never ignored, and never part of a result's identity.
Compressor = Annotated[
    PpmdCompressor | DeflateCompressor | Bzip2Compressor | LzmaCompressor,
    Field(discriminator="name"),
]


class DistanceConfig(BaseModel):
    """What the distance stage measures with, and how it schedules the work.

    ``compressor`` and its settings change every NCD value; everything else changes only
    cost and recovery. See ADR 0020 for the default, ADR 0021 for bzip2 and LZMA, and
    ``docs/method/ncd.md`` for what each compressor computes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    compressor: Compressor = PpmdCompressor()
    compression_chunk_bytes: int = Field(default=4_194_304, gt=0)
    workers: int | Literal["auto"] = "auto"
    pairs_per_shard: int | Literal["auto"] = "auto"
    resume: bool = True
    save_diagnostics: bool = False

    @field_validator("compressor", mode="before")
    @classmethod
    def _a_model_not_a_name(cls, value: object) -> object:
        # 0.3 took the compressor by name. A name is refused rather than read as that
        # compressor at its defaults, which no type checker could see; the error says what
        # to write, which the union's own "not a dictionary" would not.
        if not isinstance(value, str):
            return value
        models = get_args(get_args(Compressor)[0])
        for model in models:
            tags = get_args(model.model_fields["name"].annotation)
            if value in tags:
                written = f"name={value!r}" if len(tags) > 1 else ""
                raise ValueError(
                    f"compressor takes a settings model, not a name: write "
                    f"{model.__name__}({written})"
                )
        *others, last = (f"{model.__name__}()" for model in models)
        raise ValueError(f"compressor takes a settings model: {', '.join(others)} or {last}")

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
        """Return the shard size for ``pair_count`` pairs.

        ``"auto"`` depends on the pair count alone, never on the machine, so the shard layout
        a checkpoint records stays valid when a run resumes elsewhere.
        """
        if isinstance(self.pairs_per_shard, int):
            return self.pairs_per_shard
        return min(_MAX_AUTO_PAIRS_PER_SHARD, max(1, pair_count // _AUTO_SHARD_COUNT))
