# The distance: normalized compression distance

## What this stage computes

For two objects `x` and `y`, with `C(s)` the compressed size in bytes of `s`:

```
NCD(x, y) = (C(xy) - min(C(x), C(y))) / max(C(x), C(y))
```

`damicore_distance.ncd.normalized_compression_distance` is that expression and nothing
more. Checked against the implementation: `C(x)=100`, `C(y)=140`, `C(xy)=180` gives
`(180 - 100) / 140 = 0.5714285714285714`, which is what the function returns.

`C(s)` is not a property of `s` alone. It is the byte length of **one** compressor stream
fed the object's bytes, from `damicore_distance.compressor.compressed_size`: a single
`zlib.compressobj` is opened, every chunk of every path is pushed through it, and the
returned total is the sum of the compressed output plus the final flush. `C(xy)` is the
same call given two paths in order, so the concatenation is never materialised on disk.

The compressor and its window are part of the value. `compressor="zlib"` uses
`wbits = zlib.MAX_WBITS`; `compressor="gzip"` uses `wbits = 31`. The code comments that the
`Literal` is what keeps the branch total: a name outside the pair must fail to type-check
rather than quietly compress as something else, because every NCD value is a function of
this window.

Defaults, from `DistanceConfig`: `compressor="zlib"`, `compression_level=6`,
`compression_chunk_bytes=4194304`, `pairs_per_shard=10000`, `workers="auto"`,
`resume=True`.

## What the definition comes from

The quantity has a lineage, and the three works in it are not interchangeable:
[bennett1998] defines the distance, [li2004] normalizes it, and [cilibrasi2005] replaces
the uncomputable term with a compressor. Only the last of the three is what this code
computes; the first two are why the quantity is worth computing, and they are cited in
`## Sources` below with what this implementation does not inherit from each.

The formula is equation (3.1) of [cilibrasi2005], which writes it as

```
NCD(x,y) = (C(xy) - min{C(x),C(y)}) / max{C(x),C(y)}
```

and describes the result as "a non-negative number 0 <= r <= 1+e representing how different
the two files are". The paper presents NCD as "the applied approximation of the 'normalized
information distance'", with Kolmogorov complexity — "non-computable in the Turing sense" —
replaced by "a far less powerful real-world reference compressor C".

That substitution is the whole of the gap between the theory and this code. The paper is
explicit that its results are proved for a compressor that is *normal*, meaning it satisfies
four properties up to an additive `O(log n)` term, which its Definition 3.2 lists as
idempotency (`C(xx) = C(x)`), monotonicity (`C(xy) >= C(x)`), symmetry (`C(xy) = C(yx)`) and
distributivity (`C(xy) + C(z) <= C(xz) + C(yz)`). It says of symmetry that "stream-based
compressors of the Lempel-Ziv family, like gzip and pkzip, and the predictive PPM family,
like PPMZ, are possibly not precisely symmetric", and that "the properties of the NCD
defined below depend on the details of the compressor used". It also states plainly that
"the theory developed in [29] does not hold directly for the (possibly poorly) approximating
NCD".

`zlib` is Lempel-Ziv. This implementation therefore inherits the caveat the paper names for
that family, and takes no step to correct it.

## What this implementation does not take from that source

- **No symmetrisation.** The matrix is symmetric because each unordered pair is computed
  once and written to both triangles, not because `C(xy) = C(yx)` was checked or enforced.
- **No normality check.** Nothing measures idempotency, monotonicity, symmetry or
  distributivity for the configured compressor on the data at hand. The paper's guarantees
  are conditional on them; this code does not establish the condition.
- **No correction for the window.** A pair whose concatenation exceeds the compressor's
  window is compressed anyway, and the resulting value is reported. Refusing or flagging
  that case is open work.
- **No `1+e` bound is enforced.** The paper's range is `0 <= r <= 1+e`; the validation pass
  measures the realised range and reports `ncd_min`, `ncd_max` and
  `ncd_out_of_range_count` rather than rejecting a value outside it.

## Status in this implementation

| | |
|---|---|
| Available | `zlib` and `gzip`, both through `zlib.compressobj`, exact (every pair computed) |
| Not available | any other compressor; approximate or sampled NCD; a distance other than NCD |
| Guarded | `max(C(x), C(y)) == 0` raises `DistanceError` with code `distance_computation_error`; a compressor I/O failure raises the same class with code `compression_error` |
| Validated before the result returns | the matrix is finite, zero on the diagonal and bitwise symmetric |

## Sources

- [bennett1998] — defines the information distance this quantity descends from, "The max
  distance E1 between x and y is defined by E1(x, y) := max{K(x|y), K(y|x)}", and proves it
  universal among admissible distances. What this implementation does not take: everything
  the proof rests on. `K` is uncomputable, the metric and universality statements hold only
  up to additive logarithmic terms, and nothing here is `E1` — a compressed size is not a
  Kolmogorov complexity, and no bound relates the two.
  Read: 2026-11-07, primary.
- [li2004] — normalizes that distance into `d(x, y) = max{K(x|y*), K(y|x*)} / max{K(x),
  K(y)}` and is where the compression form this code computes first appears, as NCD in its
  Section VII. What this implementation does not take: the universality and metricity
  results, which the paper proves for the uncomputable `d` and not for its compressed
  approximation.
  Read: 2026-11-07, primary.
- [cilibrasi2005] — the equation this stage computes, and the normality conditions it is
  proved under. Stated in full under "What the definition comes from" above, and in the
  `References` section of `damicore_distance.ncd`, which is where the module's own
  attribution lives.
  Read: 2026-11-06, primary.
