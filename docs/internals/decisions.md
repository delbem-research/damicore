# Decision records

Each record fixes one decision that would otherwise be reopened by accident. They rank
above the implementation and below `AGENTS.md` in the precedence order that file states.
Applying that order means knowing what each record decides, which is what this index is for:
one line per record, saying what it settles rather than what it is about.

| Record | What it decides |
|---|---|
| [0001](../decisions/0001-foundational-decisions.md) | The four decisions the project was founded on, in the words they were first written in. No shared core package; 0002-0004 are retired and never reused |
| [0005](../decisions/0005-process-pool-for-distance-workers.md) | Distance shards run on a `ProcessPoolExecutor` with the `spawn` context, measured against threads |
| [0006](../decisions/0006-object-source-axis.md) | Producing object bytes is one concern and it belongs to the normalizer, whether the objects come from a table or from files |
| [0007](../decisions/0007-run-self-containment.md) | Adopted files are copied into the run directory, never referenced where they live |
| [0008](../decisions/0008-named-object-encoding.md) | The encoding that produced the measured bytes is part of the result, recorded as a named, versioned `object_encoding` |
| [0009](../decisions/0009-spreadsheet-engine.md) | Spreadsheets are read with `openpyxl` in read-only mode, not with `pandas.read_excel`, because bounded memory is the invariant |
| [0010](../decisions/0010-cell-text-rule.md) | The rule that turns a typed spreadsheet cell into text is this project's, not the parsing library's |
| [0011](../decisions/0011-dead-schema-fields-await-a-breaking-release.md) | Three informationless fields in the published 0.2 schemas stay until a breaking release removes them |
| [0012](../decisions/0012-duplicated-helpers-stay-duplicated.md) | Small helpers duplicated across packages stay duplicated; the evidence ADR 0001 asserted without it is here |
| [0013](../decisions/0013-the-public-signature-stays-flat.md) | `run` and `estimate` take the source axis as flat keyword arguments rather than a union a caller would have to construct |
| [0014](../decisions/0014-dataset-partitioning.md) | Dataset partitioning lives upstream of the source axis, emits datasets, and its results are fixed by a named rule |
| [0015](../decisions/0015-the-bibliography-is-fetched.md) | A work with a DOI is never typed: `docs/refs.json` is fetched from a resolver, and no mechanism here checks that a cited work supports the claim beside it |
| [0016](../decisions/0016-branch-lengths-become-edge-weights.md) | The mapping from branch lengths to FastGreedy edge weights is this project's own and decides the partition |
| [0017](../decisions/0017-the-neighbor-joining-tie-rule.md) | Q scores within a relative band are one tie, broken in lexicographic label order; neither published source states a tie rule |
| [0018](../decisions/0018-method-provenance-in-the-manifest.md) | The manifest records which named rule was in force at each stage, separately from the parameters `config_hash` already covers |
| [0019](../decisions/0019-an-attribution-declares-whether-it-was-read.md) | Every attribution to a published work declares whether anybody read it against this code, and the gate checks shape, never truth |
| [0020](../decisions/0020-ppmd-is-the-default-compressor.md) | PPMd variant I with restart is the default compressor, its order and memory are configuration, and `pyppmd` is a required dependency |
| [0021](../decisions/0021-bzip2-and-lzma-join-the-compressors.md) | bzip2 and raw LZMA2 join the compressors; each exposes only the settings that change its window, its stream memory is charged per worker, and its codec is named by a probe |
| [0022](../decisions/0022-execution-settings-are-not-run-identity.md) | `config_hash` leaves out the execution settings, which `config` still records, so a run resumes and is reused on any machine and under any worker count |

```{toctree}
:maxdepth: 1

/decisions/0001-foundational-decisions
/decisions/0005-process-pool-for-distance-workers
/decisions/0006-object-source-axis
/decisions/0007-run-self-containment
/decisions/0008-named-object-encoding
/decisions/0009-spreadsheet-engine
/decisions/0010-cell-text-rule
/decisions/0011-dead-schema-fields-await-a-breaking-release
/decisions/0012-duplicated-helpers-stay-duplicated
/decisions/0013-the-public-signature-stays-flat
/decisions/0014-dataset-partitioning
/decisions/0015-the-bibliography-is-fetched
/decisions/0016-branch-lengths-become-edge-weights
/decisions/0017-the-neighbor-joining-tie-rule
/decisions/0018-method-provenance-in-the-manifest
/decisions/0019-an-attribution-declares-whether-it-was-read
/decisions/0020-ppmd-is-the-default-compressor
/decisions/0021-bzip2-and-lzma-join-the-compressors
/decisions/0022-execution-settings-are-not-run-identity
```
