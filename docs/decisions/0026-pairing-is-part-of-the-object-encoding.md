# ADR 0026: Pairing is part of the object encoding

ADR 0008 named the encoding that turns a source into an object's bytes, because every NCD
value is a function of those bytes. `C(xy)` is a function of one more choice: how the bytes
of two objects become one stream. Until now there was one way, concatenation, and it was
not named. The original implementation had two, and this record brings the second back as a
named rule beside the first.

## The reference: the original implementation

`damicore-python` (Bruno Kim, `src/ncd.py`, read on 2026-11-25 from the archive the
maintainer supplied; the repository has no DOI, so it is named here rather than in
`docs/refs.json`) offers two pairing functions, chosen by `--pairing`:

- `concat(fname1, fname2)`: the first file followed by the second;
- `interleave(fname1, fname2, block_size=1024)`: both files cut into blocks of `block_size`
  bytes, taken in turn -- `x1 y1 x2 y2 ...` -- the remaining blocks of the longer file after
  the shorter one ends. Its docstring gives the purpose: it brings `NCD(x, x)` closer to zero
  for objects larger than the compressor's window, which it names as 32 KiB for gzip and
  900 KiB for bzip2.

That implementation worked and produced the published results, so it is the reference.
Each way this record departs from it has to be an improvement on it that loses nothing, and
has to come with the evidence that says so.

## Decision

`DistanceConfig.pairing` is `ConcatPairing()`, the default, or
`InterleavePairing(interleave_block_bytes=1024)`; `run()` and `estimate()` take them as
`pairing="concat"` or `"interleave"` and `interleave_block_bytes`, and the CLI as `--pairing`
and `--interleave-block-bytes`. The names, the block layout, the remainder rule, the default
block of 1024 bytes and the accepted domain, any positive block, are the original
implementation's. Each pairing is its own rule in the manifest's `method` block --
`ncd-max/v2` and `ncd-max-interleaved/v1` -- and both settings enter the run identity, since
they change every value.

### Where this departs from the reference, and why each departure loses nothing

| departure | the reference | here | evidence |
|---|---|---|---|
| which object goes first | the file name that sorts first | the lower SHA-256 [adr-0024] | ADR 0024: renaming files changed the partition (ARI 0.76 and 0.81). Digest order costs nothing more, and renaming cannot move a value |
| how the pair reaches the compressor | written to a temporary file per pair, then compressed | streamed from both files, one block at a time | a property test holds the bytes fed to the compressor equal to a line-for-line port of `interleave`, and the compressed size equal to compressing that port's output, for every compressor, empty objects, a block of one byte and a block longer than both objects included. It writes nothing per pair, and a worker reads each block a compression chunk at a time, holding none whole |
| a block the window cannot hold | measured, and reported as a distance | refused by preflight, like any pair past the window [adr-0023] | below. `max_pair_window_factor` measures it anyway, so nothing the reference could do is lost |
| `ncd_self_largest` | not computed | `C` of the largest object paired with itself under the run's pairing | it is the reference's own `NCD(x, x)` under interleaving, the quantity its docstring names |

### The window gate reads one block of each object

Interleaved, a block of one object lies exactly one block behind the same block of the
other, so a compressor holding a block pair sees aligned content across objects of any size.
What the compressor must hold of a pair at once is each object's span: the whole object when
concatenated, `min(size, block)` when interleaved. Concatenation is interleaving with a block
that holds either object, and a test holds the two equal there. So the gate does not get a
new rule: preflight and `pairs_over_window` read the span where they read the size.

Measured on two 1 MiB objects -- lines of one letter repeated one to three times followed by
a number below a million, seed 62 -- the second a copy of the first with one byte in a
hundred replaced by a digit, under `zlib` and its 32 KiB window, with an unrelated 1 MiB
object of the same kind for comparison. Other content moves the values a little and keeps
the pattern; a second verification measured 0.10 to 0.145 below the window and 0.998 past
it:

| pairing | near-copy | unrelated |
|---|---|---|
| concatenated | 0.996 | 0.996 |
| blocks of 1 to 24 KiB | 0.09 to 0.14 | 0.996 to 0.998 |
| blocks of 32 KiB to 512 KiB | 0.980 to 0.981 | 0.996 to 0.997 |

Past the window the reference reports a near-copy as unrelated, which is the failure ADR 0023
refuses. The gate is conservative in the same way as for concatenation: it refuses a 24 KiB
block, whose pair of 48 KiB still measured here, because the declared window is what it can
promise.

## What interleaving changes on tables

The question the reference's docstring does not answer is columns of a table, where block
`k` of one column does not hold the same rows as block `k` of another. Twelve synthetic
columns in three known groups, one JSON value per line as the normalizer writes them, 2 MiB
per column, one seed each. The distances came from a measurement script with the same
compressor settings and pairing rule, pairs in index order rather than digest order; the
tree and the partition from this repository's Neighbor Joining and FastGreedy. Adjusted
Rand index against the groups (1.00 is exact; 0.84 to 0.88 is one group split in two):

| columns | PPMd: concat / interleave | zlib | bzip2 | LZMA |
|---|---|---|---|---|
| own vocabulary per group | 0.88 / 1.00 | 1.00 / 1.00 | 0.70 / 1.00 | 1.00 / 0.88 |
| one vocabulary, other weights | 1.00 / 1.00 | 1.00 / 1.00 | 0.84 / 0.84 | 0.84 / 1.00 |
| numbers in other ranges | 0.88 / 1.00 | 1.00 / 0.84 | 0.62 / 0.84 | 0.88 / 0.84 |
| near-copies | 1.00 / 1.00 | 0.18 / 1.00 | -0.09 / 1.00 | 1.00 / 1.00 |
| tied row by row, one distribution | 0.26 / 1.00 | 0.05 / 1.00 | -0.04 / 0.14 | 0.71 / 1.00 |

On the columns that differ by distribution no pairing lost the partition, and where
interleaving scored below concatenation it was by one split. The separation -- distance
between groups minus distance within -- grew in every one of those twelve cells: under PPMd
from 0.021-0.126 to 0.042-0.163, under `zlib` from 0.000-0.002 to 0.036-0.060. Near-copies
past the window are where interleaving recovers what concatenation loses, which is the
reference's own claim.

A second calibration replicated this through the stage's own code: every compressed size from
`compressed_size`, pairs in digest order, five seeds, 256 KiB per column under all four
compressors and 1 MiB under PPMd and `zlib`, blocks of 256 bytes to 16 KiB -- 150 runs per
pairing, each paired with the concatenated run of the same columns:

| pairing | adjusted Rand index, pooled | exact partitions | higher / equal / lower | sign test | separation |
|---|---|---|---|---|---|
| concatenated | 0.750 | 81 of 150 | | | 0.076 |
| blocks of 256 B | 0.898 | 98 | 57 / 63 / 30 | p = 0.005 | 0.153, higher in 139 |
| blocks of 1 KiB | 0.907 | 98 | 57 / 61 / 32 | p = 0.011 | 0.148, higher in 133 |
| blocks of 4 KiB | 0.917 | 101 | 62 / 57 / 31 | p = 0.002 | 0.145, higher in 134 |
| blocks of 16 KiB | 0.902 | 95 | 59 / 63 / 28 | p = 0.001 | 0.138, higher in 131 |

No combination of kind of column, compressor and size lost more than 0.2 of its mean index to
interleaving under any block. Single runs did, 4 to 7 of 150 per block, mostly by FastGreedy
splitting a true group in two, against 37 to 43 that gained as much. The block sizes do not
differ on the index (every pairwise sign test p >= 0.165); separation falls as the block grows,
significantly between every two sizes. The gains are where concatenation sees nothing: near-
copies past `zlib`'s window (0.22-0.31 to 0.85-0.98) and columns related only row by row.

The last row is a caution, not a feature. Columns whose values have similar widths keep the
same rows in the same block, so interleaving partly pairs each value with its row's value in
the other column and sees a relation concatenation cannot -- but by accident of width: under
PPMd the separation behind that 1.00 was 0.001, against 0.185 when the same columns were
paired line by line. A run interleaving columns of a table measures their distributions and
some of their row-by-row association, in a proportion it cannot report.

## What was not chosen

- **Interleaving line by line.** It measures how two columns relate record by record, a
  quantity the reference never measured, and it means something only where each line of an
  object is a record. It needs its own record and its own issue.
- **Interleaving by default.** It recovered the groups better on average, with statistical
  support, but not in every run, so it is not an improvement that loses nothing: a default
  is every user's result. The concatenation stays the default, as in the reference.
- **A lower or upper bound on the block.** The reference takes any positive block. A small
  one costs time. A large one costs no memory, since a block is read into the compressor's
  buffer a chunk at a time and never held whole [adr-0027], and one past the compressor's
  window is refused by the window gate, so neither needs a bound to stay safe.
- **A smaller default block.** 256 bytes separated the groups significantly better than
  1 KiB and recovered them no better, at four times the reads per pair. The reference's
  1,024 bytes stays.

## Status

Added in 0.6.0, the release that already changes the run identity for ADR 0024, so the
identity changes once. Runs that do not set the pairing measure exactly what they measured
before. The PPMd model check of ADR 0025 applies to the interleaved stream as to any other.
