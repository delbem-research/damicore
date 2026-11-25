"""Produce C(s): the compressed size one stream gives for an object, or for a pair.

A single compressor stream is opened and every chunk of every path is pushed through it, so
a pair is compressed as a concatenation that is never written to disk. For ``zlib`` and
``gzip`` the stream is a ``zlib.compressobj`` with the largest window its format allows:
``zlib.MAX_WBITS`` for ``zlib``, 31 for ``gzip``. For ``ppmd`` it is a ``pyppmd``
``Ppmd8Encoder``: PPMd variant I, which restarts its model from scratch when the configured
memory fills, at the configured order. Those are the compressor, order and restoration of
the original DAMICORE implementation [adr-0020]. For ``bzip2`` it is a ``bz2.BZ2Compressor``
at the configured block size, and for ``lzma`` an ``lzma.LZMACompressor`` writing raw
LZMA2, with no container, at preset 6 and the configured dictionary [adr-0021].

References
----------
.. [cilibrasi2005] Clustering by Compression. This module is the C of its equation (3.1),
   and the paper is explicit that its results hold for a compressor that is *normal*:
   idempotent, monotonic, symmetric and distributive, each up to an additive O(log n) term
   (its Definition 3.2). What this unit does not take from the paper is any of those
   guarantees. It establishes none of the four, measures none of them, and the paper itself
   names the Lempel-Ziv family -- which `zlib` belongs to -- and the PPM family as "possibly
   not precisely symmetric". The window, and for PPMd the order and the model memory, are
   properties of this unit rather than of the method: the paper fixes none of them, and
   every NCD value is a function of whichever are in force, which is why
   `DistanceConfig.compressor` is a closed union of settings models and why a name outside
   it must fail validation rather than compress as something else.
   Read: 2026-11-06, primary.
.. [shkarin2003] PPM: one step to practicality. Describes PPMII, the model behind Shkarin's
   PPMd, whose variant I `pyppmd` implements and this unit drives. What this unit takes from
   it is the codec as `pyppmd` ships it, unmodified and unexamined: the model, its update
   rules and its restart are the library's, and nothing here checks them against the paper.
   Not read: cited for the algorithm the dependency implements, not for a claim made here.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from importlib.metadata import version
from io import BufferedReader
from pathlib import Path
from typing import cast

import pyppmd

from damicore_distance.config import (
    Bzip2Compressor,
    DeflateCompressor,
    DistanceConfig,
    InterleavePairing,
    LzmaCompressor,
    PpmdCompressor,
)
from damicore_distance.errors import DistanceError


def _stream(
    config: DistanceConfig,
) -> tuple[Callable[[bytearray | memoryview], bytes], Callable[[], bytes]]:
    """Open one stream and return how to feed it and how to finish it.

    The match is total over the compressor union: a model added there without a case here
    leaves a path with no return, which the type checker refuses.
    """
    settings = config.compressor
    match settings:
        case DeflateCompressor():
            # The tag is what makes the container total rather than a fallback: every NCD
            # value is a function of it, so a name outside the pair must fail validation,
            # never quietly compress as gzip.
            wbits = zlib.MAX_WBITS if settings.name == "zlib" else 31
            deflate = zlib.compressobj(level=settings.compression_level, wbits=wbits)
            return deflate.compress, deflate.flush
        case PpmdCompressor():
            model = pyppmd.Ppmd8Encoder(
                settings.ppmd_order,
                settings.ppmd_memory_bytes,
                pyppmd.PPMD8_RESTORE_METHOD_RESTART,
            )
            # pyppmd's stub declares `bytes`; its C extension takes any buffer (`y*`), which is
            # what lets the stage hand it one reused buffer rather than a new object per piece.
            encode = cast("Callable[[bytearray | memoryview], bytes]", model.encode)
            return encode, model.flush
        case Bzip2Compressor():
            block = bz2.BZ2Compressor(settings.bzip2_block_size_100k)
            return block.compress, block.flush
        case LzmaCompressor():
            dictionary = lzma.LZMACompressor(
                lzma.FORMAT_RAW, filters=[_lzma2(settings.lzma_dictionary_bytes)]
            )
            return dictionary.compress, dictionary.flush


def _lzma2(dictionary_bytes: int) -> dict[str, int]:
    return {"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": dictionary_bytes}


# Small enough that a probe costs a few milliseconds; long enough to exercise matching.
_PROBE = b"".join(b"damicore %d\n" % index for index in range(2_000))


def codec_fingerprint() -> dict[str, str]:
    """Name every codec this stage can measure with, as a result's identity needs it.

    zlib and pyppmd report their versions. libbz2 and liblzma come from the system and
    report none, so each is named by the size it gives a fixed payload: an encoder that
    compresses that payload differently changes that size, and a checkpoint or a run
    recorded under the other one is refused rather than resumed. A difference the payload
    does not reach goes unseen [adr-0021].
    """
    return {
        "zlib_build": zlib.ZLIB_VERSION,
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "pyppmd": version("pyppmd"),
        "libbz2_probe": str(len(bz2.compress(_PROBE, 1))),
        "liblzma_probe": str(
            len(lzma.compress(_PROBE, format=lzma.FORMAT_RAW, filters=[_lzma2(65_536)]))
        ),
    }


# pyppmd 1.3.1's `encode()` never releases the buffer it is handed, so the object it is given
# stays alive for good: fed fresh bytes, every stream would leave its whole input resident.
# Every compressor is therefore fed from one buffer per chunk size, kept for the life of the
# process, which it would outlive anyway; a stream leaves at most one view of it [adr-0027].
_BUFFERS: dict[int, bytearray] = {}


def _paired_chunks(
    paths: tuple[Path, ...], config: DistanceConfig
) -> Iterator[bytearray | memoryview]:
    """The bytes the compressor is fed, in order, a buffer of ``compression_chunk_bytes`` at a
    time; each piece is valid until the next is requested.

    Two paths are a pair, joined by ``config.pairing``: one after the other, or in blocks
    taken in turn, the rest of the longer after the shorter ends [adr-0026]. One path, or any
    other count, is fed whole in order. Full pieces are the buffer itself and only the last is
    a view of it, so the compressor is handed no new object but one per stream [adr-0027].
    """
    pairing = config.pairing
    block = (
        pairing.interleave_block_bytes
        if isinstance(pairing, InterleavePairing) and len(paths) == 2
        else None
    )
    size = config.compression_chunk_bytes
    buffer = _BUFFERS.get(size)
    if buffer is None:
        buffer = _BUFFERS[size] = bytearray(size)
    view = memoryview(buffer)
    filled = 0
    with ExitStack() as stack:
        sources: list[BufferedReader] = [stack.enter_context(path.open("rb")) for path in paths]
        while True:
            moved = 0
            # A round reads each source to its end, or, interleaved, one block of each.
            for source in sources:
                taken = 0
                while block is None or taken < block:
                    if filled == size:
                        yield buffer
                        filled = 0
                    room = size - filled if block is None else min(size - filled, block - taken)
                    got = source.readinto(view[filled : filled + room])
                    if not got:
                        break
                    filled += got
                    taken += got
                moved += taken
            if block is None or not moved:
                break
    if filled:
        yield view[:filled]


def compressed_size(paths: Iterable[Path], *, config: DistanceConfig) -> int:
    return compressed_fingerprint(paths, config=config)[0]


def compressed_fingerprint(paths: Iterable[Path], *, config: DistanceConfig) -> tuple[int, bytes]:
    """The size of the compressed stream and the SHA-256 of its bytes.

    Two streams of one size can still differ: the PPMd model check compares the bytes two
    models code, because a model and its half that both restart were found coding the same
    number of bytes as each other and not the number an unfilled model codes [adr-0025].
    """
    digest = hashlib.sha256()
    try:
        feed, finish = _stream(config)
    except MemoryError as exc:
        raise DistanceError(
            f"Could not allocate a {config.compressor.name} stream of "
            f"{config.compressor.stream_memory_bytes} bytes; lower its settings or the worker "
            "count",
            code="compression_error",
        ) from exc
    total = 0
    try:
        for chunk in _paired_chunks(tuple(paths), config):
            coded = feed(chunk)
            digest.update(coded)
            total += len(coded)
        coded = finish()
        digest.update(coded)
        total += len(coded)
    except OSError as exc:
        raise DistanceError(f"Could not compress object: {exc}", code="compression_error") from exc
    return total, digest.digest()
