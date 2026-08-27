# ADR 0001: Foundational decisions

The four decisions DAMICORE was founded on, each recorded here in the words it was first
written in. They were four files of five lines; the numbers 0002, 0003 and 0004 are retired
and not reused, and nothing outside this directory ever referenced them. Every later ADR
states one decision and keeps its own file.

## Package boundaries

The four stage distributions are independently installable and never import a
sibling. Only `damicore` depends on and sequences them. Paths and versioned
artifacts are the integration boundary; no shared core package is introduced.

## Canonical CSV serialization

CSV values remain text. Columns use one compact JSON string per line and rows
use one compact JSON array per line. UTF-8 and LF make bytes independent of
chunking and platform, which makes NCD inputs reproducible and hashable.

## Memory maps and resource gates

The exact distance matrix and Neighbor Joining workspace are float64 `.npy`
memory maps. A mandatory preflight calculates hard bounds before object files
are created. This bounds memory without disguising quadratic storage or cubic
tree construction.

## Exact local algorithms

Version 0.1 uses incremental zlib/gzip NCD without clamping, deterministic
Neighbor Joining, and igraph FastGreedy. There is no network service, external
compressor, approximation, or algorithm fallback.
