# ADR 0012: Duplicated helpers stay duplicated

ADR 0001 states that no shared core package is introduced and that small, specific models
stay with the package that owns their contract. It asserts this without evidence, so the
duplication it permits reads as an oversight and gets re-proposed. This ADR records the
measurement, so the next audit starts from it instead of repeating it.

## What is actually duplicated, and what only looks it

Two categories, and only one of them is a candidate for consolidation.

**Wire-format validators — not duplication.** `NormalizationManifest` and its three input
variants, `LabelsArtifact`, the tree node/edge/artifact models, `ClustersArtifact`, and the
two matrix validators. The stage packages exchange versioned artifacts and never each other's
code, so each side declaring what it accepts is the mechanism, not a defect: sharing a model
would couple the producer's and the consumer's evolution and remove the independent
versioning the file contract exists to provide. The two matrix validators belong here for the
same reason and are easy to mistake for one function -- `damicore_distance` validates the
matrix it just produced, `damicore_tree_builder` validates the one it was handed plus its
agreement with the labels.

**Implementation helpers — genuinely duplicated.** These carry no contract:

| Helper | Copies | Lines today | Shared | Net |
|---|---:|---:|---:|---:|
| atomic JSON writer | 3 | 58 | ~25 | −33 |
| chunked SHA-256 | 3 | 18 | ~9 | −9 |
| two-phase stager | 2 | 24 | ~16 | −8 |
| error base `__init__` | 4 | 46 | ~36 | −10 |
| `effective_workers` | 2 | 10 | ~7 | −3 |

About sixty lines in total.

## The drift argument does not hold

Measured across all copies at 0.2, comparing normalised function bodies:

- the atomic writer differs between `damicore` and `damicore_distance` only in how the
  `json.dumps` call is wrapped;
- the SHA-256 helpers differ only in a parameter name;
- the two-phase stagers differ because the clusterizer's takes `newline` for CSV output,
  which makes it a strict generalisation of the tree builder's;
- the four error bases have identical `__init__` bodies and differ in their default `code`
  and their docstring, both of which are per-package by design.

No defect has entered through divergence. The one asymmetry found was
`damicore_normalizer._atomic_json` missing the parent `mkdir` its two siblings perform; it
was corrected where it stood rather than by consolidating, because every caller already
created the directory.

## Why every mechanism costs more than sixty lines

A shared helper reached by `import` must be resolvable by `pip install damicore-distance`,
so "internal" and "independently installable" cannot both hold for a real dependency. That
leaves three shapes, and each is worse than the duplication:

**A sixth published distribution.** Another lockstep version to keep in step across
`version_guard.py`, `CITATION.cff` and the changelog; two more trusted publishers, so twelve
entries; another release environment; a three-stage publish order instead of two; and five
more packaging files, which alone offset most of the sixty lines. It also becomes public
surface that users may depend on.

**Vendoring a shared source directory into each wheel at build time.** One source of truth
in the repository and five copies in the wheels. Development imports do not resolve, since
the directory does not exist in the source tree; the fix is a tracked symlink, which costs
Windows contributors a non-default checkout setting this project supports. An sdist unpacked
outside the monorepo cannot resolve the include path. And a shared *class* becomes one
distinct class per wheel, so the error base -- the only helper whose consolidation would
enable something new, a single `except` for any stage failure -- is exactly the one this
shape cannot deliver.

**Keeping the copies under a non-divergence test**, as the Ruff and sdist-exclusion
conventions already are. This adds more lines than it removes and buys protection against a
failure the measurement above shows has not occurred.

## Decision

No shared helper package, published or vendored. The copies stay.

## Revision condition

Reopen when any of these becomes true, and prefer the published sixth distribution if so,
since vendoring cannot carry shared classes:

- a defect is traced to divergence between copies rather than to one copy alone;
- the shared surface grows past roughly two hundred lines, at which point the packaging
  overhead stops dominating;
- a stage package needs to expose a type that another stage must recognise with
  `isinstance`, which the current shape cannot express at all.
