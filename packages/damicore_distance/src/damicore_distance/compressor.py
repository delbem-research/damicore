"""Produce C(s): the compressed size one stream gives for an object, or for a pair.

A single `zlib.compressobj` is opened and every chunk of every path is pushed through it, so
a pair is compressed as a concatenation that is never written to disk. The window is the
maximum the chosen format allows: `zlib.MAX_WBITS` for `zlib`, 31 for `gzip`.

References
----------
.. [cilibrasi2005] Clustering by Compression. This module is the C of its equation (3.1),
   and the paper is explicit that its results hold for a compressor that is *normal*:
   idempotent, monotonic, symmetric and distributive, each up to an additive O(log n) term
   (its Definition 3.2). What this unit does not take from the paper is any of those
   guarantees. It establishes none of the four, measures none of them, and the paper itself
   names the Lempel-Ziv family -- which `zlib` belongs to -- as "possibly not precisely
   symmetric". The window is a property of this unit rather than of the method: the paper
   does not fix one, and every NCD value is a function of whichever is in force, which is
   why `DistanceConfig.compressor` is a `Literal` and why a name outside it must fail to
   type-check rather than compress as something else.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from damicore_distance.errors import DistanceError


def compressed_size(
    paths: Iterable[Path],
    *,
    compressor: Literal["zlib", "gzip"],
    level: int,
    chunk_bytes: int,
) -> int:
    # The literal is what makes the else total rather than a fallback: every NCD value is a
    # function of this window, so a name outside the pair must fail to type-check, never
    # quietly compress as gzip.
    wbits = zlib.MAX_WBITS if compressor == "zlib" else 31
    stream = zlib.compressobj(level=level, wbits=wbits)
    total = 0
    try:
        for path in paths:
            with path.open("rb") as source:
                while chunk := source.read(chunk_bytes):
                    total += len(stream.compress(chunk))
        total += len(stream.flush())
    except OSError as exc:
        raise DistanceError(f"Could not compress object: {exc}", code="compression_error") from exc
    return total
