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
# Small enough that one shard's PPMd stream residue, checked, fits what the default limit
# leaves each of four workers, so a worker can always be replaced between shards [adr-0027].
_MAX_AUTO_PAIRS_PER_SHARD = 2_048
# pyppmd 1.3.1's `Ppmd8Encoder` frees its model on deallocation but never the `CPpmd8` that
# holds it, 7,392 bytes on x86-64, and its `encode()` keeps whatever it is handed; fed from a
# reused buffer, a stream keeps at most one 312-byte view of it: 7,704 bytes traced, left by
# every PPMd stream in the process that opened it, rounded up to 8 KiB for the allocator's
# headers and the interpreter's own bookkeeping [adr-0027].
PPMD_STREAM_RESIDUE_BYTES = 8_192
# What a pooled worker may accumulate before it is replaced, where no limit is declared.
_DEFAULT_RESIDUE_BUDGET = 32 * 1_048_576


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

    @property
    def window_bytes(self) -> int:
        """The bytes of a pair this compressor can still match across [adr-0023].

        32 KiB, which is ``2 ** zlib.MAX_WBITS`` and the most either container allows.
        """
        return 32_768


# pyppmd's own ceiling on model memory, past which it clamps without saying so.
PPMD_MEMORY_LIMIT = 4_294_967_259


class PpmdCompressor(BaseModel):
    """PPMd variant I with model restart, at ``ppmd_order`` with ``ppmd_memory_bytes`` of
    model [adr-0020]. The bounds are the codec's own -- pyppmd would clamp a value outside them
    without saying so -- except the lower memory bound, twice the codec's: the stage checks
    every measurement again under half the model, which must still be one the codec takes
    [adr-0025]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["ppmd"] = "ppmd"
    ppmd_order: int = Field(default=6, ge=2, le=64)
    ppmd_memory_bytes: int = Field(default=67_108_864, ge=4_096, le=PPMD_MEMORY_LIMIT)

    @property
    def stream_memory_bytes(self) -> int:
        """The memory one stream is charged, once per worker, by preflight.

        Stated rather than measured per run: a stream fills to it on incompressible data
        [adr-0021].
        """
        return self.ppmd_memory_bytes

    @property
    def window_bytes(self) -> None:
        """PPMd declares no window, because it has none to declare [adr-0023].

        Its reach across a pair is set by the model memory together with the content, not by
        a byte count: the model restarts when it fills, and how fast it fills depends on how
        varied the data is and on what the two objects share. The stage therefore checks each
        measurement instead of gating it by size [adr-0025].
        """
        return None


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

    @property
    def window_bytes(self) -> int:
        """The bytes of a pair this compressor can still match across [adr-0023].

        The block, which bzip2 sizes in units of 100,000 bytes rather than of 100 KiB. Two
        objects in different blocks share nothing [adr-0021].
        """
        return self.bzip2_block_size_100k * 100_000


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

    @property
    def window_bytes(self) -> int:
        """The bytes of a pair this compressor can still match across [adr-0023].

        The dictionary. Measured, a copy still survives at twice it [adr-0023], so declaring
        the dictionary gates earlier than the measurement requires, never later.
        """
        return self.lzma_dictionary_bytes


# Each compressor is its own model, so a setting it does not read is not a field to write:
# refused by name, never ignored, and never part of a result's identity.
Compressor = Annotated[
    PpmdCompressor | DeflateCompressor | Bzip2Compressor | LzmaCompressor,
    Field(discriminator="name"),
]


class ConcatPairing(BaseModel):
    """The pair is one object followed by the other, the pairing NCD is defined with
    [cilibrasi2005]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["concat"] = "concat"

    def span_bytes(self, object_bytes: int) -> int:
        """The bytes of one object the compressor holds at once to match the other across a
        pair: all of it [adr-0026]."""
        return object_bytes


class InterleavePairing(BaseModel):
    """The pair is the two objects' blocks of ``interleave_block_bytes`` taken in turn, the
    rest of the longer one after the shorter ends: the original implementation's
    ``interleave``, unchanged [adr-0026].

    A block of one object lies one block behind the same block of the other, so a compressor
    whose window holds a block pair matches aligned content across objects of any size.
    Content shared out of alignment is matched only within that reach.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["interleave"] = "interleave"
    interleave_block_bytes: int = Field(default=1_024, ge=1)

    def span_bytes(self, object_bytes: int) -> int:
        """The bytes of one object the compressor holds at once to match the other across a
        pair: one block, or the object if it is shorter [adr-0026]."""
        return min(object_bytes, self.interleave_block_bytes)


# Each pairing is its own model, like each compressor: a block size is not a setting of the
# concatenation, so it is refused there rather than ignored.
Pairing = Annotated[ConcatPairing | InterleavePairing, Field(discriminator="name")]


class DistanceConfig(BaseModel):
    """What the distance stage measures with, and how it schedules the work.

    ``compressor`` and ``pairing``, with their settings, change every NCD value; everything
    else changes only cost, recovery or admission. See ADR 0020 for the default, ADR 0021
    for bzip2 and LZMA, ADR 0026 for the pairings, and ``docs/method/ncd.md`` for what each
    computes.

    ``allow_model_restart`` lifts the one admission check this stage makes itself: under
    PPMd, every object and every pair is compressed again under half the model. A stream half
    the model codes into other bytes may have restarted the model, so it is compressed once
    more under twice the model, as many at once as ``max_working_memory_bytes`` holds, and the
    run is refused if the model's bytes differ from those: the model restarted, and the size
    is not the objects' own [adr-0025]. Objects are checked before any pair, pairs as they are
    computed and looked at again once all are. Setting it measures anyway, and skips the check
    and its cost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    compressor: Compressor = PpmdCompressor()
    pairing: Pairing = ConcatPairing()
    compression_chunk_bytes: int = Field(default=4_194_304, gt=0)
    workers: int | Literal["auto"] = "auto"
    pairs_per_shard: int | Literal["auto"] = "auto"
    resume: bool = True
    save_diagnostics: bool = False
    allow_model_restart: bool = False
    # The working memory the caller allows the stage, or `None` for what its workers are
    # charged. It bounds the second look under twice the PPMd model [adr-0025].
    max_working_memory_bytes: int | None = Field(default=None, gt=0)

    @property
    def worker_memory_bytes(self) -> int:
        """The memory one worker is charged, which is its compressor stream and two of whatever
        it reads at once -- a compression chunk, or under interleaving a block of each object
        if that is larger [adr-0021] [adr-0026]."""
        pairing = self.pairing
        block = pairing.interleave_block_bytes if isinstance(pairing, InterleavePairing) else 0
        return 2 * max(self.compression_chunk_bytes, block) + self.compressor.stream_memory_bytes

    def stream_residue(self, object_count: int, pair_count: int) -> tuple[int, int | None, bool]:
        """How the PPMd stream residue is contained [adr-0027]: the bytes one process is
        charged for it, the residue a pooled worker may hold before it is replaced (``None``,
        never), and whether the run needs worker processes at all.

        Every stream a process opens stays resident in it, so a process's residue grows with
        the streams it has run. A pooled worker is replaced before a task would take it past
        what the working-memory budget leaves it -- the limit, less what every worker is
        charged for its stream, shared among the workers -- and never before its first task,
        whose residue cannot be split, so it holds at most the larger of the two. A single
        worker runs in the calling process, as it always has, while the residue of the whole
        run fits that budget, and in one process of its own, replaced the same way, once it
        does not. Other compressors leave nothing.
        """
        workers = self.effective_workers
        if not isinstance(self.compressor, PpmdCompressor):
            return 0, None, workers > 1
        checked = not self.allow_model_restart
        pairs_per_shard = self.effective_pairs_per_shard(pair_count)
        # Checked, an object is opened four times (its size, the model, half, twice) and a
        # pair three (the model, half, twice); a shard task holds two per pair.
        total = PPMD_STREAM_RESIDUE_BYTES * (
            object_count * (4 if checked else 1) + pair_count * (3 if checked else 1) + 1
        )
        per_task = PPMD_STREAM_RESIDUE_BYTES * pairs_per_shard * (2 if checked else 1)
        if self.max_working_memory_bytes is None:
            if workers == 1:
                return total, None, False
            budget = _DEFAULT_RESIDUE_BUDGET
        else:
            spare = self.max_working_memory_bytes - workers * self.worker_memory_bytes
            budget = (spare - pairs_per_shard * 24) // workers
            if workers == 1 and total <= budget:
                return total, None, False
        held = max(budget, per_task)
        return min(total, held), held, True

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
