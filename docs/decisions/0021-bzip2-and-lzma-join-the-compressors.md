# ADR 0021: bzip2 and LZMA join the compressors

ADR 0020 made PPMd the default and left every other compressor deferred. This adds two,
`bzip2` and `lzma`, both from the Python standard library, because past 32 KiB DEFLATE loses
shared content entirely and PPMd keeps less of it as objects grow, while LZMA keeps it at
any size its dictionary holds. PPMd stays the default.

## The measurement

Columns written as the normalizer writes them, one JSON value per line. *Distribution* is
the distance across two vocabularies minus the distance within one; *copy* is the distance
to an unrelated column minus the distance to the same column with 1 % of its bytes changed.

| object | `zlib` | `bzip2`, 900 kB block | `lzma`, 8 MiB dictionary | `ppmd` |
|---|---|---|---|---|
| 16 KiB, distribution / copy | 0.127 / 0.828 | 0.454 / 0.661 | 0.169 / 0.884 | 0.413 / 0.531 |
| 128 KiB | 0.028 / 0.019 | 0.313 / 0.590 | 0.084 / 0.906 | 0.238 / 0.298 |
| 1 MiB | 0.005 / 0.003 | 0.079 / 0.070 | 0.085 / 0.907 | 0.160 / 0.178 |
| 4 MiB | 0.001 / 0.001 | 0.025 / 0.025 | 0.030 / 0.906 | 0.090 / 0.090 |
| throughput | ~45 MB/s | ~14 MB/s | 1.7 to 3.4 MB/s | ~15 MB/s |

Each covers what the others miss. LZMA finds a copy at any size its dictionary holds the
pair in, and little of a distribution. PPMd keeps the distribution of large objects and
little of a copy. bzip2 sees both, but only within its block, and nothing past it. So the
choice is the caller's, and the default stays the one columns of a dataset need.

## What each one is

- `compressor="bzip2"` is `bz2.BZ2Compressor` at `bzip2_block_size_100k` (1 to 9, default 9,
  the largest block). The block is its window: two objects in different blocks share
  nothing.
- `compressor="lzma"` is LZMA2 in the raw format, preset 6, with `lzma_dictionary_bytes`
  (4096 to 1610612736, the codec's own bounds; default 8388608, preset 6's own). The
  dictionary is its window. The raw format carries no container: the `.xz` one adds 59 bytes
  to every stream, a constant that inflates the distance between small objects, the same
  defect ADR 0020 found in the original binary's file-name header.

A compressor exposes the settings that change what `C(xy)` can see across a pair -- a
window, a block, a model's order or memory -- and fixes the rest here. That is why the LZMA
preset is not a setting: preset 9 measured the same as preset 6 at every size above.
`compression_level` remains for `zlib` and `gzip` as the published contract it already is,
and is not a precedent for this rule.

In `damicore_distance` each compressor is its own settings model, and `DistanceConfig`
holds one of them, chosen by `name`: `PpmdCompressor`, `DeflateCompressor` for `zlib` and
`gzip`, which share their one setting, `Bzip2Compressor` and `LzmaCompressor`. A setting
for another compressor is then not refused by a rule but absent: the model has no field to
hold it, and a result's identity holds no default of a compressor it did not use. Each
setting keeps one name on every surface -- the model field, `damicore.run`'s argument, the
CLI flag and the manifest field -- so `damicore`, whose signature stays flat (ADR 0013),
derives which arguments each compressor accepts from the models rather than restating them,
and a test holds the four surfaces to the models. A compressor added later is a model added
to that union.

## What it costs

Every worker holds one stream, and preflight charges each compressor's stream memory once per
worker. Measured peak resident memory, on incompressible data, on Linux x86_64:

| stream | measured | charged |
|---|---|---|
| `bzip2`, block 9 | 9.5 MiB | 10 MiB at every block size |
| `lzma`, dictionary 4 KiB / 1 / 4 / 8 / 16 / 64 MiB | 1.5 / 14.5 / 50.7 / 94 / 187 / 675 MiB | 12 x dictionary + 4 MiB |
| `zlib`, `gzip`, level 6 | -- | 256 KiB, the two tables `zconf.h` sizes; the few kilobytes it adds are not charged |
| `ppmd` | -- | the configured model memory (ADR 0020) |

At the defaults, four workers stay inside the default 512 MiB limit for every compressor.
DEFLATE's state was never charged before this record; it is now, so a `zlib` estimate rises
by 256 KiB per worker.

## How a result names its codec

`bz2` and `lzma` call the system's libbz2 and liblzma, and Python exposes the version of
neither. A result is a function of the encoder, so the runtime fingerprint names each by
behavior instead: the compressed size of a fixed payload, recorded beside the zlib versions
and the `pyppmd` version. A system library that encodes that payload differently changes
that size, and a checkpoint or a completed run written under the old one is refused rather
than resumed. A difference that leaves the payload's size alone, or shows only at larger
blocks or dictionaries than the probe uses, goes unseen.

## Status

ADR 0005 stands: like zlib and PPMd, both compressors open a fresh stream per pair and
keep no state across pairs, so the process pool is unchanged; what their larger streams
change is the memory per worker, which preflight now charges. The window each compressor
declares for a gate is deferred to issue 59, which has the gate to use it. Revisit the LZMA
charge if the preset changes, since the match finder's memory is what the factor of 12
measures.
