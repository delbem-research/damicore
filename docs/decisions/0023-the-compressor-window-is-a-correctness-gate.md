# ADR 0023: The compressor window is a correctness gate, with a factor to lift it

NCD estimates what two objects share through `C(xy)`. A compressor finds that sharing only
while both objects are inside what it can match across -- DEFLATE's window, bzip2's block,
LZMA's dictionary. Past that, `C(xy)` approaches `C(x) + C(y)` and every distance moves
toward 1, which is a measurement that failed while looking exactly like one that worked:
the values stay inside [0, 1], the matrix validates, Neighbor Joining runs, and the tree it
produces is an artifact of object size. By this project's quality-driver order that is a
correctness defect, so preflight refuses it before a run starts, and the result records it
afterwards.

## What each compressor declares

`window_bytes` on the compressor's settings model, beside `stream_memory_bytes`:

| compressor | window | why that number |
|---|---|---|
| `zlib`, `gzip` | 32,768 | `zlib.MAX_WBITS`, the most either container allows |
| `bzip2` | `bzip2_block_size_100k * 100_000` | the block; two objects in different blocks share nothing [adr-0021] |
| `lzma` | `lzma_dictionary_bytes` | the dictionary |
| `ppmd` | none | below |

## The measurement

One JSON value per line, as the normalizer writes a column, and a copy of the same object
with 1 % of its bytes changed. A compressor that can still match across the pair reports a
distance near 0; one that cannot reports one near 1. Pair bytes are the two objects
together.

| pair bytes | `zlib` | `bzip2`, 100 kB block | `bzip2`, 900 kB block | `lzma`, 64 KiB dictionary | `lzma`, 8 MiB | `ppmd`, 1 / 16 / 64 MiB model |
|---|---|---|---|---|---|---|
| 16 KiB | 0.166 | 0.432 | 0.432 | 0.084 | 0.084 | 0.550 |
| 32 KiB | 0.199 | 0.431 | 0.431 | 0.085 | 0.085 | 0.635 |
| 64 KiB | 0.669 | 0.384 | 0.384 | 0.095 | 0.095 | 0.695 |
| 128 KiB | 0.806 | 0.829 | 0.380 | 0.112 | 0.112 | 0.755 |
| 256 KiB | 0.894 | 0.890 | 0.424 | 0.665 | 0.150 | 0.816 |
| 512 KiB | 0.942 | 0.956 | 0.524 | 0.797 | 0.197 | 0.879 |
| 2 MiB | 0.982 | 0.968 | 0.878 | 0.938 | 0.271 | 0.963 / 0.963 / 0.964 |

Each column rises past its own declared limit and not before: `zlib` between a 32 KiB pair
and a 64 KiB one, `bzip2` at 100 kB with the smallest block and at 900 kB with the largest,
`lzma` with a 64 KiB dictionary between 128 and 256 KiB, and `lzma` with 8 MiB nowhere in
this range. Below its limit a column is not monotone -- both `bzip2` columns fall from 0.432 at a
16 KiB pair to 0.384 at a 64 KiB one, as the fixed cost of a block is spread over more data --
and the rise past the limit is what the window predicts, not the shape before it. The two
`bzip2` columns differ only in the block, which is the point. The LZMA row shows that a declared window is conservative -- the
copy survives at twice the dictionary and is gone at four times, and this grid does not say
where between them it goes -- which is the direction a gate should err in.

**Where the loss actually happens, and what the gate costs.** It is not a cliff at the pair
boundary. Measured finely around DEFLATE's window, a pair of 32,768 bytes reads 0.199, one of
40,960 reads 0.211, one of 49,152 reads 0.216, and one of 65,536 reads 0.669. The collapse is
at the last of those, where the *first object alone* fills the window and is gone before the
second is read; between the pair boundary and that point the measurement degrades only
slightly.

The gate is nonetheless at the pair boundary, and that is a deliberate cost: at the default
factor it refuses pairs that still measure acceptably -- two 20 KiB objects under `zlib` are
a 40,960-byte pair, which reads 0.211 against the 0.199 of a pair exactly at the window, and
it is refused. The reason is that the pair
boundary is the last point at which the whole of the first object is visible for the whole of
the second, so it is the last point at which the value is a measurement of the pair rather
than of a prefix of it; where exactly the degradation becomes unacceptable is a judgement
about the corpus, which is what `max_pair_window_factor` hands to the caller. A caller who
knows their objects are far below the window of a compressor they could use should choose
that compressor; one who wants the measurement anyway raises the factor and gets the count of
pairs above the window in the report.

## Why PPMd declares no window

PPMd has a model, not a window, and the measurement says the model's size is not the limit.
Across 1 MiB, 16 MiB and 64 MiB of model memory the distance to a 1 % perturbed copy is the
same value at every object size up to 512 KiB and differs by 0.0013 at 1 MiB objects, while
degrading with object size under all three. There is therefore no byte count at which PPMd
stops seeing, and any number put in that field would be invented rather than measured.

What PPMd loses with size is the copy, not the distribution: at 1 MiB objects it still
separates columns drawn from different vocabularies by 0.160 where `zlib` separates them by
0.005 [adr-0020]. That is why it is the default and why gating it on size would refuse the
runs it exists for. A PPMd run is therefore not gated here, and
`DistanceResult.ncd_self_largest` is where a filled model shows: it is the same measurement
as the "copy" column above, taken on the run's own largest object.

`compressor_window_bytes` is `None` rather than `0` wherever no window is declared, in the
estimate, in the result and in `report.json`. Zero pairs over a window nobody declared is
the only count that would be true, and it would read as a safety nobody measured.

## The gate

`ResourceLimits.max_pair_window_factor`, default 1.0. Preflight already computes
`max_pair_bytes`, the two largest objects together; the violation `compressor_window` is
raised when that exceeds `window_bytes * max_pair_window_factor`. `estimate()` reports it
like every other violation and `run()` turns it into `ResourceLimitError`.

It is a factor rather than a flag because the honest override is not "ignore the window" but
"I know this measurement reaches past it by this much, and I want it anyway" -- for a corpus
whose objects share their opening bytes, or a run being compared with an older one. The value
is recorded in the manifest under `config.limits`, so a run says which gate admitted it. It
is also the only limit here that bounds what a run *measures* rather than what it costs,
which is why it is a factor on a declared property of the compressor rather than a byte
count a caller would have to derive.

`DistanceResult.pairs_over_window` and the same field in `report.json` count every pair above
the window, not just the largest, computed from the object sizes rather than during
compression: the pair loop runs across shards and workers, so a resumed run would otherwise
count only the shards it ran. A run admitted with a raised factor therefore still says how
much of its matrix was measured with one object out of view.

## What this does not do

It does not fix the measurement for objects past the window. Interleaved pairing (issue 62)
and the larger-window compressors already here are the fix; this record is the gate that
stops the defect from being silent. The works issue 59 names as its basis belong to that
fix rather than to this gate, and are not cited here: `cesar2016` is the alternative pairing
function, which issue 62 implements and will cite where it is implemented.

## Status

The window each compressor declares is a property of the compressor, so a compressor added
later declares its own, and `window_bytes` returning `None` stays available for one whose
reach is not a byte count. Revisit the factor's default if a corpus is found whose
measurement is sound past the window: that would be evidence that the window is the wrong
place for the gate, which no corpus measured here shows.

The exemption of PPMd is revised by [adr-0025]: the measurement above was taken on data whose
model barely fills, and on data that fills it the model's restart is a limit after all. PPMd
still declares no window; each of its measurements is checked instead.
