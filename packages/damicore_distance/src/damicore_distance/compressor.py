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
   `DistanceConfig.compressor` is a `Literal` and why a name outside it must fail to
   type-check rather than compress as something else.
   Read: 2026-11-06, primary.
.. [shkarin2003] PPM: one step to practicality. Describes PPMII, the model behind Shkarin's
   PPMd, whose variant I `pyppmd` implements and this unit drives. What this unit takes from
   it is the codec as `pyppmd` ships it, unmodified and unexamined: the model, its update
   rules and its restart are the library's, and nothing here checks them against the paper.
   Not read: cited for the algorithm the dependency implements, not for a claim made here.
"""

from __future__ import annotations

import bz2
import lzma
import zlib
from collections.abc import Callable, Iterable
from importlib.metadata import version
from pathlib import Path

import pyppmd

from damicore_distance.config import DistanceConfig
from damicore_distance.errors import DistanceError


def _stream(config: DistanceConfig) -> tuple[Callable[[bytes], bytes], Callable[[], bytes]]:
    """Open one stream and return how to feed it and how to finish it.

    The match is total over the compressor ``Literal``: a name added there without a case
    here leaves a path with no return, which the type checker refuses.
    """
    match config.compressor:
        case "zlib" | "gzip":
            # The literal is what makes the window total rather than a fallback: every NCD
            # value is a function of it, so a name outside the pair must fail to type-check,
            # never quietly compress as gzip.
            wbits = zlib.MAX_WBITS if config.compressor == "zlib" else 31
            deflate = zlib.compressobj(level=config.compression_level, wbits=wbits)
            return deflate.compress, deflate.flush
        case "ppmd":
            model = pyppmd.Ppmd8Encoder(
                config.ppmd_order,
                config.ppmd_memory_bytes,
                pyppmd.PPMD8_RESTORE_METHOD_RESTART,
            )
            return model.encode, model.flush
        case "bzip2":
            block = bz2.BZ2Compressor(config.bzip2_block_size_100k)
            return block.compress, block.flush
        case "lzma":
            dictionary = lzma.LZMACompressor(
                lzma.FORMAT_RAW, filters=[_lzma2(config.lzma_dictionary_bytes)]
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


def compressed_size(paths: Iterable[Path], *, config: DistanceConfig) -> int:
    try:
        feed, finish = _stream(config)
    except MemoryError as exc:
        raise DistanceError(
            f"Could not allocate a {config.compressor} stream of "
            f"{config.stream_memory_bytes} bytes; lower its settings or the worker count",
            code="compression_error",
        ) from exc
    total = 0
    try:
        for path in paths:
            with path.open("rb") as source:
                while chunk := source.read(config.compression_chunk_bytes):
                    total += len(feed(chunk))
        total += len(finish())
    except OSError as exc:
        raise DistanceError(f"Could not compress object: {exc}", code="compression_error") from exc
    return total
