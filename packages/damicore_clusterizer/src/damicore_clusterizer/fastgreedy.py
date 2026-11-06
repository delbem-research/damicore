"""Partition the tree's graph by greedy modularity maximization.

This module does not implement the method. It calls igraph, reads back the membership
vector, the community count and the modularity igraph reports, and converts them to plain
Python values.

References
----------
.. [clauset2004] Finding community structure in very large networks. The source of the
   procedure igraph runs here: repeatedly join the two communities whose amalgamation
   produces the largest increase in modularity, yielding a dendrogram. What this unit does
   not take from it is the objective, below, and any claim of optimality: the paper offers
   the greedy procedure as an approximation, saying "finding the global maximum modularity
   over all possible divisions seems hard in general".
.. [newman2004] Analysis of weighted networks. The source of the quantity actually
   maximized. `community_fastgreedy(weights="weight")` is called on a weighted graph, and
   [clauset2004] defines modularity with an adjacency entry of 1 or 0, saying "This
   assumes the graph is unweighted; weighted graphs are a simple generalization" and
   deferring it to this work.
.. [csardi2006] The igraph software package. The implementation that runs, rather than a
   description of it. Where the cut of the dendrogram falls when no count is requested is
   igraph's rule, not a criterion from either paper above.
"""

from __future__ import annotations

from typing import Any

import igraph as ig


def fastgreedy_membership(
    graph: ig.Graph,
    num_clusters: int | None,
) -> tuple[list[int], int, float]:
    dendrogram = graph.community_fastgreedy(weights="weight")
    # igraph's VertexDendrogram.as_clustering is untyped in the shipped stubs, so the result
    # arrives as Unknown; Any isolates that one library boundary rather than the module.
    clustering: Any = (
        dendrogram.as_clustering()
        if num_clusters is None
        else dendrogram.as_clustering(n=num_clusters)
    )
    return (
        [int(value) for value in clustering.membership],
        len(clustering),
        float(clustering.modularity),
    )
