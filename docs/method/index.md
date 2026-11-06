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
| which compressor, and its window | configuration, `zlib` by default | [`ncd.md`](ncd.md) |
| which pair joins when two Q scores tie | this project | [adr-0017] |
| how a branch length becomes an edge weight | this project | [adr-0016] |
| how many communities to cut the dendrogram into | igraph, or an explicit count | [`community-detection.md`](community-detection.md) |

Four of the six have no published source at all. That is the reason this section exists:
naming the paper a stage comes from does not identify the result a run reports, because the
result also depends on every choice the paper left open.

## Where the method comes from

DAMICORE was introduced in [sanches2011]. **This page does not yet state what this
implementation takes from that paper and what it changes** — that check is owed, and issue
#93 carries it. The individual stage pages cite the source of each stage's method, and mark
the same way any attribution that has not been read against the primary work.

## How to read these pages against the code

Every formula on these pages was checked by running the function it describes and
reproducing the numbers by hand; the pages show the inputs and the results. Where a page
states what a published work says, it quotes it. Where it has not been checked against the
primary source, it says so rather than implying it has — a citation that resolves proves the
work exists, never that the work supports the sentence beside it.
