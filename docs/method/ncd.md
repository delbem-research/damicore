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
stream is opened, every chunk of every path is pushed through it, and the returned total is
the sum of the compressed output plus the final flush. `C(xy)` is the same call given two
paths in order, so the concatenation is never materialised on disk.

The compressor and its settings are part of the value, and each compressor reads only its
own; a setting for another compressor is refused, not ignored. `damicore` takes them as
flat arguments; `damicore_distance` takes one settings model per compressor --
`PpmdCompressor`, `DeflateCompressor` (`name="zlib"` or `"gzip"`), `Bzip2Compressor`,
`LzmaCompressor` -- which has no field for a setting its compressor does not read.

- `compressor="ppmd"`, the default, is PPMd variant I through `pyppmd`, restarting its
  model from scratch when the model memory fills. `ppmd_order` (2 to 64, default 6) and
  `ppmd_memory_bytes` (2048 to 4294967259, default 67108864) set it; the bounds are the
  codec's own, outside which it would clamp without saying so. It has no window: what it
  keeps across a pair is the statistics of what it has read, until its memory fills.
- `compressor="zlib"` and `compressor="gzip"` are DEFLATE through `zlib.compressobj`, with
  `wbits = zlib.MAX_WBITS` and `wbits = 31`, and `compression_level` (0 to 9, default 6).
  Both see a 32 KiB window: past it, the first object of a pair is out of view while the
  second is compressed, and the distance nears 1 whatever the objects are.
- `compressor="bzip2"` is `bz2.BZ2Compressor` with `bzip2_block_size_100k` (1 to 9,
  default 9, a 900 kB block). The block is its window: a pair spanning two blocks shares
  nothing between them.
- `compressor="lzma"` is raw LZMA2, with no container, at preset 6, with
  `lzma_dictionary_bytes` (4096 to 1610612736, default 8388608). The dictionary is its
  window, and it keeps finding a copy of an object at any size the dictionary holds the
  pair in, where bzip2 stops at its block; it is also the slowest, at 1.7 to 3.4 MB/s
  against PPMd's 15.

Which one measures what, at which size, is the table in [adr-0021]: LZMA finds shared
content at any size its dictionary holds, PPMd keeps a shared distribution across large
objects, and bzip2 sees both only within its block.

The `Literal` is what keeps the choice total: a name outside it must fail to type-check
rather than quietly compress as something else, because every NCD value is a function of
the compressor and its settings. Why PPMd is the default, and how it compares with `zlib`
and with the original DAMICORE implementation on the same objects, is [adr-0020].

Execution defaults, from `DistanceConfig`: `compression_chunk_bytes=4194304`,
`pairs_per_shard="auto"`, `workers="auto"`, `resume=True`. None of them changes an NCD
value.

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

`zlib` and LZMA are Lempel-Ziv and PPMd is of the PPM family, so with any of them this
implementation inherits the caveat the paper names for that family; `bzip2` is
block-sorting, a family the paper does not name, and nothing here establishes its symmetry
either. This implementation takes no step to correct
it.

## What this implementation does not take from that source

- **No symmetrisation.** The matrix is symmetric because each unordered pair is computed
  once and written to both triangles, not because `C(xy) = C(yx)` was checked or enforced.
- **No normality check.** Nothing measures idempotency, monotonicity, symmetry or
  distributivity for the configured compressor on the data at hand. The paper's guarantees
  are conditional on them; this code does not establish the condition.
- **No correction for the window or the model memory.** A pair whose concatenation exceeds
  the DEFLATE window, the bzip2 block or the LZMA dictionary, or fills the PPMd model until it
  restarts, is compressed anyway, and the resulting value is reported. Refusing or flagging
  that case is open work.
- **Idempotency is measured once, not established.** The largest object is compressed a
  second time after itself, and `ncd_self_largest`, `(C(xx) - C(x)) / C(x)`, reports how
  much of it the compressor recognized; the paper's normal compressor gives zero. It is
  never a gate, and a high value is not by itself a fault: on one independently drawn value
  per line neither DEFLATE nor PPMd recognizes a repeat of 128 KiB, while PPMd still
  separates such columns by their distributions [adr-0020]; LZMA recognizes it.
- **No `1+e` bound is enforced.** The paper's range is `0 <= r <= 1+e`; the validation pass
  measures the realised range and reports `ncd_min`, `ncd_max` and
  `ncd_out_of_range_count` rather than rejecting a value outside it.

## Status in this implementation

| | |
|---|---|
| Available | `ppmd` through `pyppmd`; `zlib` and `gzip` through `zlib.compressobj`; `bzip2` and `lzma` through the standard library; exact (every pair computed) |
| Not available | any other compressor; approximate or sampled NCD; a distance other than NCD |
| Guarded | `max(C(x), C(y)) == 0` raises `DistanceError` with code `distance_computation_error`; a compressor I/O failure, or a compressor stream that cannot be allocated, raises the same class with code `compression_error` |
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
- [shkarin2003] — PPMII, the model behind Shkarin's PPMd, whose variant I `pyppmd`
  implements. What this implementation does not take: anything from the paper itself. The
  codec is the library's, unmodified, and nothing here checks it against the paper.
  Not read: cited for the algorithm the dependency implements, not for a claim made here.
- [adr-0020] — why PPMd variant I with restart is the default, and the comparison with
  `zlib` and with the original DAMICORE implementation on the same objects.
- [adr-0021] — why bzip2 and LZMA are offered, what each measures at which size, and the
  memory each stream is charged.
