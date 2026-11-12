"""Produce C(s): the compressed size one stream gives for an object, or for a pair.

A single compressor stream is opened and every chunk of every path is pushed through it, so
a pair is compressed as a concatenation that is never written to disk. For ``zlib`` and
``gzip`` the stream is a ``zlib.compressobj`` with the largest window its format allows:
``zlib.MAX_WBITS`` for ``zlib``, 31 for ``gzip``. For ``ppmd`` it is a ``pyppmd``
``Ppmd8Encoder``: PPMd variant I, which restarts its model from scratch when the configured
memory fills, at the configured order. Those are the compressor, order and restoration of
the original DAMICORE implementation [adr-0020].

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

import zlib
from collections.abc import Callable, Iterable
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
            try:
                model = pyppmd.Ppmd8Encoder(
                    config.ppmd_order,
                    config.ppmd_memory_bytes,
                    pyppmd.PPMD8_RESTORE_METHOD_RESTART,
                )
            except MemoryError as exc:
                raise DistanceError(
                    "Could not allocate the PPMd model; lower ppmd_memory_bytes or workers",
                    code="compression_error",
                ) from exc
            return model.encode, model.flush


def compressed_size(paths: Iterable[Path], *, config: DistanceConfig) -> int:
    feed, finish = _stream(config)
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
