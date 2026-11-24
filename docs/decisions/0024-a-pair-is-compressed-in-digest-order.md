# ADR 0024: A pair is compressed in the order of its contents' digests

NCD measures a pair through `C(xy)`, the compressed size of one object followed by the
other. Until 0.5 the object that came first was whichever came first in the input, and no
compressor this stage offers is symmetric: the paper the formula comes from names the
Lempel-Ziv and PPM families as "possibly not precisely symmetric", and `docs/method/ncd.md`
records that this implementation never established `C(xy) = C(yx)`. What had never been
measured was the consequence.

## The measurement

Over every pair of DAMICORE's own 44 implementation modules (195 to 59,191 bytes), the
difference between the distance computed through `C(xy)` and through `C(yx)`:

| compressor | median | p95 | max | objects whose nearest neighbour changes |
|---|---|---|---|---|
| `ppmd` | 0.0016 | 0.0051 | 0.0120 | 3 / 44 |
| `zlib` | 0.0027 | 0.0105 | 0.0296 | 4 / 44 |
| `lzma` | 0.0024 | 0.0090 | 0.0222 | 1 / 44 |
| `bzip2` | 0 | 0 | 0 | 0 / 44 |

Small next to a distance, but distances between similar objects lie close together, and the
order decides which pair Neighbor Joining joins first. End to end under the default
compressor, the same objects reversed gave a different partition -- adjusted Rand index 0.76
against the forward run on those 44 modules, and 0.81 on a 12-column table of 10,000 rows --
while the forward run's own matrix, reversed, gave an index of 1.00 through the tree and the
clusterer. The tree and clusterer are order-invariant; the matrix was not. A result that
moves when columns are reordered or files renamed fails what `docs/quality-drivers.md` ranks
right after correctness.

## The rule

Each pair is compressed with the object whose SHA-256 is lower first. The digests are the
ones the normalization manifest already records and this stage already verifies before it
reads an object, so the rule costs one comparison per pair. Equal digests are equal bytes,
where the order cannot matter.

The matrix is then a function of the set of objects: reordered, it comes back with rows and
columns reordered alike and every value bitwise equal, which a metamorphic property test
holds for every compressor. The rule is `ncd-max/v2` in the manifest's `method` block
[adr-0018], and the distance checkpoint's identity moves to schema version 2, so a shard
measured in input order is refused on resume rather than mixed with shards measured in digest
order.

## What was not chosen

- **Symmetrising**, by compressing both orders and keeping the smaller size or the mean. It
  would also make each value nearer to a symmetric compressor's, which the digest order does
  not: the asymmetry is still in every value, now in a direction fixed by content instead of
  by position. It costs a second compression of every pair, which is the whole cost of this
  stage, and nothing measured shows that the closer value changes a partition. The digest
  order removes the defect that was measured at no cost; symmetrising stays open as a
  separate rule if a measurement shows it is worth twice the work.
- **Ordering by label**, which is free too but still moves the result when a file or a
  column is renamed.

## What this does not do

It makes no compressor symmetric and measures no other normality condition. Interleaved
pairing, when it is added, is a second way of building the pair from two objects, and this
rule fixes which of the two objects it starts from.

## Status

Every NCD value whose pair was compressed in the other order changes, so every run gets a
different matrix than 0.5 would have given it. The version moves with the rule, which is what
keeps a 0.5 run directory from being reused or resumed under it.
