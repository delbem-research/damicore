# The evidence behind the roadmap

## What this page is

[The roadmap](../roadmap.md) says which capabilities the published DAMICORE literature
attests and DAMICORE 0.3 does not have. This page is where those claims come from: an audit
of 82 documents of DAMICORE work, read against the 0.3 source tree. It states the method, the
limitations, and the counts, and it ships the per-document data the counts are computed from,
so a reader can disagree with a number by recomputing it rather than by argument.

The corpus is the literature that descends from [sanches2011], the work this implementation
takes its pipeline from.

## The corpus

82 documents, classified by what each one does with the method:

| Role | Documents | What it means |
|---|---|---|
| `application` | 45 | applies DAMICORE to a domain problem |
| `context` | 16 | cites or surveys it without applying or extending it |
| `method_contribution` | 15 | changes the method itself: a stage, a criterion, an encoding |
| `adjacent_method` | 4 | a clustering method used as a comparison or a component |
| `review` | 2 | a review covering the method among others |

## How a document was read

Each document was read for what it states about the pipeline, and twelve fields were
extracted from it: the novelty it claims, the library feature it would need, the compressors
it uses, the distance, the tree stage, the partition stage, the criterion for the number of
groups, the input encoding, the labeling, the validation, the limitations it states, and the
scale it reports.

A capability is **attested by** a document when the lexical pattern for that capability
matches the concatenation of those fields, case-insensitively. The count for a capability is
the number of documents it matches. Nothing is weighted: a paper whose whole contribution is
cluster labeling counts once, and so does one that mentions labeling as future work.

## The data

`evidence-corpus.csv`, beside this page, is one row per document: year, first author, short
title, role, the object count it reports if it reports one, and one `0`/`1` column per
capability. The title is the document's title cut to its first fifty characters, so it
identifies a document rather than citing it; where two documents by the same author and year
cut to the same string, the second carries a `(2)` suffix and they are two distinct works.

**The table below is that file's column sums**, which is what makes the counts checkable:
load the file, sum the column, compare.

## What the corpus attests, and what 0.3 has

| Capability attested in the literature | Documents | Years | State in 0.3 | Column |
|---|---|---|---|---|
| Automatic cluster labeling | 19 | 2012-2024 | absent | `labeling` |
| Cluster validation and stability | 18 | 2012-2025 | partial - modularity only | `validation_stability` |
| Compressors beyond DEFLATE (PPMd, bzip2, LZMA) | 15 | 2015-2024 | partial - zlib and gzip only | `compressors_beyond_deflate` |
| Tree and dendrogram visualization | 15 | 2012-2023 | absent - Newick export only | `visualization` |
| Discretization and symbolic encoding | 13 | 2013-2023 | partial - JSON lines, Fisher-Jenks | `discretization_encoding` |
| Consensus and resampling of trees | 10 | 2016-2024 | absent - one deterministic tree | `consensus_resampling` |
| Number-of-clusters criterion | 9 | 2013-2025 | partial - modularity or manual k | `k_selection` |
| Objects larger than the compressor window | 7 | 2013-2022 | absent | `window_overflow` |
| Scale beyond 1,000 objects | 7 | 2016-2024 | partial - capped at 1,000 objects | `scale_beyond_1000` |
| Incremental NCD reusing compressions | 7 | 2016-2024 | absent | `incremental_ncd` |
| Pluggable distance metric | 5 | 2016-2024 | absent - NCD fixed | `pluggable_metric` |
| Alternative community detection | 5 | 2014-2019 | absent - FastGreedy only | `alternative_community_detection` |
| Alternative tree builders (UPGMA, linkage) | 3 | 2017-2024 | absent | `alternative_tree_builders` |
| Multiple representations per object | 2 | 2013-2022 | absent | `multiple_representations` |
| Binary front-end (strings, CFG, lifting) | 2 | 2024-2024 | absent | `binary_frontend` |
| Out-of-sample classification | 1 | 2017-2017 | absent | `out_of_sample` |

The two most attested capabilities in the whole corpus -- automatic cluster labeling and
cluster validation -- are exactly the two the library does not have. A 0.3 run returns a
partition and a modularity value: groups with no meaning attached and no measure of how
stable they are.

## The scale profile

27 of the 82 documents report how many objects they ran on. The median is 133, the third
quartile 846, the maximum 8,755, and six report more than 1,000. The preflight cap of 1,000
objects is therefore above the median published study and below the six largest, which is the
measurement behind the roadmap treating scale as a real limit rather than a theoretical one.

## Limitations

1. **Pages were sampled, not read whole.** Each document was read at its method-dense pages
   rather than end to end. A capability stated only in a passage the sampling missed is not
   counted, so every count is a lower bound.
2. **Six documents carry no text layer** and were read as page images. Their extraction
   depends on what was legible in the rendering.
3. **The match is lexical.** A work that implements a capability without naming it in the
   vocabulary the pattern expects is missed, and a work that only mentions one is counted.
   This is why the table is evidence of *attention* in the literature, not a census of
   implementations.

## What this page is not

It is not a bibliography. The works counted here are not cited by key and are not in
`docs/refs.json`, which records the works *this implementation rests on* -- a different set,
held to a different rule: every entry there is fetched from a resolver and cited by a unit
that implements it (ADR 0015). The audit corpus is data about the literature, and it ships as
data.
