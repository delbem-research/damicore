# The clusters: modularity on the tree's graph

## What this stage computes

The tree becomes an undirected weighted graph, and a greedy agglomerative procedure
partitions it.

**The graph.** Every node of the tree becomes a vertex **except the root**, which is
dropped. The root has exactly two children, and its two edges become one edge joining those
children, carrying the sum of their lengths. Every other edge is kept. The result is the
unrooted form of the tree, with `n - 1` edges over `n` retained nodes, which the code
asserts before continuing.

**The weights.** From the branch lengths, in `tree_graph._edge_weights`:

```
reciprocal_i = 1 / length_i
weight_i     = 1 + (reciprocal_i - min(reciprocals)) / stddev(reciprocals)
```

with every weight exactly `1.0` when the standard deviation is zero. Checked against the
implementation:

| lengths | weights |
|---|---|
| `[0.5, 1, 2, 4]` | `[3.611, 2.119, 1.373, 1.0]` |
| `[2, 2, 2]` | `[1.0, 1.0, 1.0]` |
| `[-1, 2, 4]` | `[1.0, 3.286, 2.905]` |

A shorter branch joins a closer pair, so it carries a larger weight. The branch weighing
exactly one is whichever has the smallest reciprocal: the longest, while every length is
positive, and otherwise a negative one. The division by the standard deviation is what
bounds the result and what makes it independent of the unit the lengths are measured in;
[adr-0016] records that mapping, its measurements and why a zero length raises rather than
being repaired.

**The partition.** `fastgreedy.py` calls `graph.community_fastgreedy(weights="weight")` and
then `as_clustering()`, or `as_clustering(n=num_clusters)` when a count is requested. It
returns the membership vector, the number of communities, and the modularity igraph reports.
Communities are then projected onto the leaves.

## What the method comes from, and from where exactly

Two works, and the split between them matters.

The **procedure** is [clauset2004]: "starting with each vertex being the sole member of a
community of one, we repeatedly join together the two communities whose amalgamation
produces the largest increase in Q", producing a dendrogram. Its stated running time is
`O(m d log n)` for `n` vertices, `m` edges and dendrogram depth `d`.

The **quantity maximized** is not from that paper. [clauset2004] defines modularity in its
equation (4) with `A_vw` equal to 1 if two vertices are connected and 0 otherwise, and says
of it: "This assumes the graph is unweighted; weighted graphs are a simple generalization",
deferring that generalization to [newman2004]. This stage hands igraph a *weighted* graph,
so what is maximized is the weighted form, and [newman2004] is its source.

**The implementation is igraph's, not this project's.** [csardi2006] is the library that
runs the agglomeration; `fastgreedy.py` builds the graph and reads the result.

## What this implementation does not take from those sources

- **The number of communities is not chosen by the method.** `as_clustering()` with no
  argument cuts the dendrogram where igraph's own rule says to; passing `num_clusters`
  overrides it. Neither is a criterion from [clauset2004], which produces a dendrogram and
  leaves the cut open.
- **The weights are this project's.** No published work specifies the branch-length to
  edge-weight mapping; [adr-0016] is its basis, and [clauset2004] is cited for the procedure,
  not for how the weights it is given are built.
- **No optimality is claimed.** [clauset2004] says "finding the global maximum modularity
  over all possible divisions seems hard in general", and offers the greedy procedure as an
  approximation. The modularity this stage reports is the one igraph reached, not a maximum.
- **Nothing here checks the 0.3 heuristic.** The paper notes that "in practice it is found
  that a value above about 0.3 is a good indicator of significant community structure"; this
  code reports modularity and draws no conclusion from its value.

## Status in this implementation

| | |
|---|---|
| Available | greedy modularity maximization on the weighted unrooted tree, via igraph |
| Not available | any other community detection method; any criterion for the number of groups beyond igraph's cut or an explicit count |
| Guarded | a root that is not internal, a root without exactly two children, an edge naming an unknown node, a zero or non-finite branch length, and an edge count other than `n - 1`, each raising `ClusterizerError` with code `tree_format_error` |
