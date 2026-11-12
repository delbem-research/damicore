# ADR 0020: PPMd is the default compressor

`AGENTS.md` defers alternative compressors. This reopens that decision for one compressor,
PPMd variant I with model restart, and makes it the default. Two facts decide it: the 0.3
default measures the wrong thing above 32 KiB without saying so, and the implementation
DAMICORE was first published with already defaulted to PPMd.

## The original implementation

`brunokim/damicore-python` (2016, <https://github.com/brunokim/damicore-python>) has no DOI,
so this record is where it is named. Its `ncd.py` defaults to `ppmd` and runs Dmitry
Shkarin's `ppmd` binary as `ppmd e -o6 -f<out> <file>`. It never passes `-m` or `-r`: its
`--memory` option is parsed and dropped, so memory and restoration are the binary's
defaults. The binary of that era, Debian `ppmd` 10.1-5 (2012), states them as 10 MB and
`-r0`, restart, and its `-r` switch identifies it as variant I. Everything else matches
this implementation: unordered pairs `x < y`, the concatenation `xy`, equation (3.1) of
[cilibrasi2005] unclamped, and one value written to both triangles.

## The measurement

DEFLATE sees 32 KiB: past it, the second object of a pair is compressed with the first out
of view, and every distance nears 1. On columns written as the normalizer writes them, one
JSON value per line drawn from a vocabulary, two from one vocabulary and a third from
another, the gap between the distance across vocabularies and the distance within one is:

| object size | `zlib` | PPMd variant I, order 6, 64 MiB |
|---|---|---|
| 16 KiB | 0.127 | 0.413 |
| 128 KiB | 0.028 | 0.238 |
| 1 MiB | 0.005 | 0.160 |

What PPMd keeps is the distribution, not the copy. An object repeated verbatim is found
again by neither compressor at 128 KiB when it is one independently drawn value per line --
distance to itself 0.94 to 0.99 under both -- while on lines of twelve words from a
5,000-word vocabulary PPMd finds it (0.242, against 0.970 under `zlib`). Which content is
which was measured, not derived, so the gap above, not the distance of an object to itself,
is the measure that decides this record.

The original was run with that binary on the same objects, produced by this repository's
normalizer, and every matrix went through this repository's Neighbor Joining and FastGreedy.
Separation is the mean NCD between groups minus the mean within them.

| corpus | measure | original | `zlib` | PPMd variant I, `pyppmd` |
|---|---|---|---|---|
| 30 columns of 21 KB | separation | 0.225 | 0.123 | 0.286 |
| | Spearman against the original | 1 | 0.642 | 0.902 |
| 30 columns of 636 KB | separation | 0.176 | 0.009 | 0.187 |
| | Spearman against the original | 1 | 0.650 | 0.778 |
| 18 texts of 200 KB sharing only a distribution | separation | 0.286 | 0.037 | 0.288 |
| | Spearman against the original | 1 | 0.488 | 0.932 |

The distance between groups that `zlib` loses above its window is what the original
measured and what PPMd keeps. The original is not reproducible byte for byte: its archive
format stores the input file name, so its sizes depend on name lengths, and its revision of
variant I diverges from `pyppmd`'s after the first ten bytes of output, with sizes 0.4 to
0.8 % apart. No partition chosen by modularity matched the original's on any corpus under
any compressor, the closest configuration included; that is the cut, not the compressor,
and it is why the matrix and not the partition is the comparison above.

## What the dependency is

`pyppmd>=1.3.1,<1.4` becomes a runtime dependency of `damicore-distance`. It is a wheel, not
a system binary, published for CPython 3.10 to 3.14 on manylinux and musllinux (x86_64,
aarch64), macOS (x86_64, arm64) and Windows (x86, x64, arm64). `ppmd-cffi`, its predecessor,
publishes nothing past CPython 3.9. It is not an extra: a default that may be absent is
either a failure on the default path or a silent fallback, and this project allows neither.

## What is part of the value

`compressor="ppmd"` is variant I with restart, `pyppmd.Ppmd8Encoder`. Restoration is fixed
rather than exposed, because the original never changed it; cut-off, or variant H, would be
another name in the `Literal`, never a parameter.

The model order (2 to 64, default 6, the original's) and the model memory (2048 to
4294967259 bytes, default 64 MiB) change every NCD value, so they are configuration: covered
by the configuration hash, recorded in the manifest, and validated here, because `pyppmd`
clamps a value outside those ranges without reporting it and two configurations would give
one result. A parameter set for a compressor it does not apply to is refused, not ignored.
The default memory is not the original's 10 MB: the two agree until the model fills, which
on text happens around a few MiB per object, and past that the larger model keeps measuring
where the smaller one restarts and saturates. `ppmd_memory_bytes=10_485_760` reproduces the
original's setting.

Model memory is also the resource bound. A model can fill at 25 times its input on
incompressible data, so preflight charges the configured memory once per worker rather than
any multiple of the object size; four workers at the default fit the default working-memory
limit. The `pyppmd` version joins the runtime fingerprint beside the zlib versions, so a
checkpoint written by another build is refused rather than resumed.

The distance rule stays `ncd-max/v1`: the formula is unchanged, and the compressor `Literal`
is the rule's domain, which this record extends. ADR 0005 is not reopened: PPMd, like zlib,
gets a fresh encoder per pair and keeps no state across pairs.

## Status

Revisit when the window gate of issue 59 lands, which has to state a limit for a compressor
that has a model instead of a window, or when LZMA is measured on real corpora: on objects
that share content rather than a distribution, a 64 MiB LZMA2 dictionary separated groups by
0.940 where PPMd at order 6 separated them by 0.215.
