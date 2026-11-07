"""The distance every later stage is a function of.

NCD(x, y) = (C(xy) - min(C(x), C(y))) / max(C(x), C(y)), where C is the compressed size in
bytes of a single compressor stream. The denominator is refused rather than guarded when it
is zero: a zero compressed size means the compressor produced no output for the larger of
the two objects, which is not a distance of any value.

References
----------
.. [cilibrasi2005] Clustering by Compression. The formula above is its equation (3.1),
   unchanged. What this unit does not take from it is the rest: the paper proves its
   results for a compressor that is *normal* -- satisfying idempotency, monotonicity,
   symmetry and distributivity up to an additive O(log n) term -- and nothing here
   establishes that the configured compressor is. The paper names the Lempel-Ziv family,
   which `zlib` belongs to, as "possibly not precisely symmetric", and states that "the
   theory developed in [29] does not hold directly for the (possibly poorly) approximating
   NCD". The matrix is symmetric because each unordered pair is computed once and written
   to both triangles, not because C(xy) = C(yx) was checked. See `docs/method/ncd.md`.
   Read: 2026-11-06, primary.
"""

from __future__ import annotations

from damicore_distance.errors import DistanceError


def normalized_compression_distance(cx: int, cy: int, cxy: int) -> float:
    denominator = max(cx, cy)
    if denominator == 0:
        raise DistanceError("NCD denominator is zero", code="distance_computation_error")
    return (cxy - min(cx, cy)) / denominator
