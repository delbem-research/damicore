# What DAMICORE computes

This section states what each stage of the pipeline computes, where that definition comes
from, and — the part worth reading — what this implementation does **not** take from its
source. It is the companion to the API reference: that page says how to call the library,
these say what the call means.

The pipeline is one path with no branches:

```
objects  ->  exact NCD matrix  ->  deterministic Neighbor Joining tree  ->  FastGreedy clusters
```

| stage | what it decides | page |
|---|---|---|
| materialization | the bytes every later number is a function of | [normalization](normalization.md) |
| distance | the dissimilarity between every pair | [ncd](ncd.md) |
| tree | the topology and branch lengths | [neighbor-joining](neighbor-joining.md) |
| clusters | the groups, and how many | [community-detection](community-detection.md) |

## The open choices, and who makes them

A method is only identified when the choices it leaves open are named. These are this
project's, not the literature's, and each is a versioned record rather than a parameter
value:

| choice | decided by | recorded in |
|---|---|---|
| the exact bytes of an object | this project | [adr-0001] |
| how a spreadsheet cell becomes text | this project | [adr-0010] |
| which compressor, and its window or model | configuration, PPMd variant I by default ([adr-0020]) | [`ncd.md`](ncd.md) |
| which pair joins when two Q scores tie | this project | [adr-0017] |
| how a branch length becomes an edge weight | this project | [adr-0016] |
| how many communities to cut the dendrogram into | igraph, or an explicit count | [`community-detection.md`](community-detection.md) |

Four of the six have no published source at all. That is the reason this section exists:
naming the paper a stage comes from does not identify the result a run reports, because the
result also depends on every choice the paper left open.

## The run says which of these produced it

Every run's `manifest.json` carries a `method` block naming the rule in force at each stage,
with the citation keys above beside it. A run directory therefore answers this page's
question on its own, a year later, without the release that wrote it being available to ask.
See [ADR 0018](../decisions/0018-method-provenance-in-the-manifest.md) and
[the artifact contract](../artifacts.md).

## Where the method comes from

DAMICORE was introduced in [sanches2011], which combines the three stages above — NCD, then
Neighbor Joining, then Fast Newman — and applies them to software. This implementation is
that pipeline and only that pipeline; what the paper does around it, it does not. The
entry in `## Sources` states the difference.

## How to read these pages against the code

Every formula on these pages was checked by running the function it describes and
reproducing the numbers by hand; the pages show the inputs and the results. Where a page
states what a published work says, it quotes it. Where it has not been checked against the
primary source, it says so rather than implying it has — a citation that resolves proves the
work exists, never that the work supports the sentence beside it.

## Status in this implementation

| | |
|---|---|
| Checked against the code | every formula on these pages, by running the function and reproducing the numbers |
| Checked against the source | which attributions have been read, and which have not, is declared on each attribution itself — in the `References` section of the module that makes the claim, and in the `## Sources` section of the page that makes it. Nothing restates it here, because two places holding one fact is how they come to disagree |
| Not stated anywhere yet | a comparison of this implementation's output against the results published in [sanches2011] |

## Sources

- [sanches2011] — the paper that names this pipeline: NCD for the distances, Neighbor
  Joining for the tree, Fast Newman for the communities, applied to software. What this
  implementation does not take from it is everything around that pipeline. The paper feeds
  DAMICORE two representations of the same programs — the C source, and a "DNA" string
  produced from it by transformation rules that map each construct to a letter — and reads
  the *consensus* of the two clusterings; this library measures the bytes it is given,
  offers the two encodings of [adr-0008] and derives neither representation. The paper's
  purpose, merging kernels into one FPGA core and measuring the area saved, is downstream
  of the clustering and absent here. Its own NCD, Neighbor Joining and Fast Newman
  attributions are to other works, which the stage pages cite directly rather than through
  it.
  Read: 2026-11-07, primary.
