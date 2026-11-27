"""Turn the tree into the weighted graph FastGreedy partitions.

The root is dropped and its two edges become one joining its children with their lengths
summed, so the graph is the unrooted form of the tree. Nodes joined by a zero branch are one
point and become one vertex; every other branch length becomes a strictly positive weight.

References
----------
.. [adr-0016] Branch lengths become edge weights by a rule this project owns. No published
   work specifies the mapping, and it decides which clusters a run reports from the same
   tree, so the record is its basis. What this unit does not take from the record is any
   claim about the method downstream: the record fixes the weights, and the objective they
   are fed into is cited where that objective is computed.
.. [adr-0028] A zero branch joins its two nodes into one point instead of being refused, and
   replaces the section of [adr-0016] that refused it. No published work specifies this
   either; the record is its basis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import igraph as ig
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from damicore_clusterizer.errors import ClusterizerError


class _TreeNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: str
    kind: Literal["leaf", "internal"]
    label: str | None


class _TreeEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    source: str
    target: str
    length: float = Field(allow_inf_nan=False)


class _TreeArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    root_id: str
    nodes: tuple[_TreeNode, ...]
    edges: tuple[_TreeEdge, ...]


@dataclass(frozen=True)
class GraphInput:
    graph: ig.Graph
    # Vertex order as igraph holds it. Exposed here so callers never read graph.vs
    # themselves: this module is the only place that touches igraph's attribute API. A vertex
    # that is several nodes joined by zero branches is named after the first of them.
    vertex_names: tuple[str, ...]
    object_ids: tuple[str, ...]
    labels: dict[str, str]
    # The vertex each object sits at. Several objects share one where zero branches join them.
    vertex_of: dict[str, int]


def _edge_weights(lengths: list[float]) -> list[float]:
    """Map branch lengths onto the strictly positive edge weights FastGreedy is given.

    A shorter branch joins a closer pair, so a weight starts as the reciprocal of the length.
    The reciprocals are then shifted so the smallest sits at zero, divided by their standard
    deviation, and offset by one: a weight is one plus the number of standard deviations its
    reciprocal lies above the smallest.

    The division is what bounds the result, and the shift alone would not. Modularity is a
    weighted sum, so a single unbounded weight would decide the partition by itself, and a
    reciprocal is unbounded as the length approaches zero; dividing contains that because the
    branch that sends a reciprocal to infinity sends the standard deviation there with it.
    The division is also what makes the weights independent of the unit the lengths are
    measured in. When every branch is the same length there is no spread to divide by and
    every weight is one, which leaves FastGreedy an unweighted graph -- the honest reading,
    since the lengths then carry no ordering.

    It also settles what a negative branch means. Neighbor Joining emits one where the matrix
    contradicts the split it is making, and the reciprocal of a negative length sorts below
    every positive one, so such a branch becomes the weakest edge rather than the strongest.

    The result is at least one everywhere and is unchanged by rescaling every length, the
    second being a consequence of the division. See [adr-0016].

    No length here is zero: a zero branch joins its nodes into one point before any weight is
    computed [adr-0028]. An empty list, the tree that is a single point, has no weights.

    Raises
    ------
    ClusterizerError
        If a length is so small that its reciprocal overflows. That is not repaired here: a
        repair would be the unbounded weight this function exists to avoid.
    """
    if not lengths:
        return []
    reciprocals: list[float] = []
    for length in lengths:
        value = 1.0 / length
        if not math.isfinite(value):
            raise ClusterizerError(
                "Branch length is too small to carry a finite weight",
                code="tree_format_error",
            )
        reciprocals.append(value)

    mean = sum(reciprocals) / len(reciprocals)
    deviation = math.sqrt(sum((value - mean) ** 2 for value in reciprocals) / len(reciprocals))
    if deviation == 0.0:
        # Every branch has the same length, so the reciprocals carry no ordering to spread.
        return [1.0] * len(reciprocals)
    lowest = min(reciprocals)
    return [1.0 + (value - lowest) / deviation for value in reciprocals]


def _load_tree_graph(path: Path) -> GraphInput:
    try:
        artifact = _TreeArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        root_id = artifact.root_id
        nodes = artifact.nodes
        edges = artifact.edges
    except (OSError, ValidationError) as exc:
        raise ClusterizerError("Invalid tree artifact", code="tree_format_error") from exc

    node_ids = [node.id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ClusterizerError("Tree node ids are not unique", code="tree_format_error")
    if root_id not in node_ids:
        raise ClusterizerError("Tree root_id names no node", code="tree_format_error")
    root_node = next(node for node in nodes if node.id == root_id)
    if root_node.kind != "internal":
        raise ClusterizerError("Tree root must be internal", code="tree_format_error")
    leaves = tuple(node.id for node in nodes if node.kind == "leaf")
    if len(leaves) < 2:
        raise ClusterizerError("Tree must contain at least two leaves", code="tree_format_error")
    # Validate and collect in one pass: a separate `any(...)` guard establishes the invariant
    # for a reader but not for the comprehension that follows, which then carries `str | None`.
    labels: dict[str, str] = {}
    for node in nodes:
        if node.kind != "leaf":
            continue
        if not isinstance(node.label, str):
            raise ClusterizerError("Every leaf must have a string label", code="tree_format_error")
        labels[node.id] = node.label
    root_edges = [edge for edge in edges if edge.source == root_id]
    if len(root_edges) != 2:
        raise ClusterizerError("Tree root must have exactly two children", code="tree_format_error")

    retained_nodes = [node_id for node_id in node_ids if node_id != root_id]
    index = {node_id: position for position, node_id in enumerate(retained_nodes)}
    graph_edges: list[tuple[int, int]] = []
    lengths: list[float] = []
    for edge in edges:
        source, target = edge.source, edge.target
        if source == root_id:
            continue
        if source not in index or target not in index:
            raise ClusterizerError("Tree edge references an unknown node", code="tree_format_error")
        graph_edges.append((index[source], index[target]))
        lengths.append(edge.length)
    left, right = root_edges
    graph_edges.append((index[left.target], index[right.target]))
    lengths.append(left.length + right.length)
    if any(not math.isfinite(length) for length in lengths):
        raise ClusterizerError("Branch lengths must be finite", code="tree_format_error")
    if len(graph_edges) != len(retained_nodes) - 1:
        raise ClusterizerError("Unrooted tree must have n-1 edges", code="tree_format_error")
    if not ig.Graph(n=len(retained_nodes), edges=graph_edges, directed=False).is_connected():
        raise ClusterizerError("Tree graph is disconnected", code="tree_format_error")

    # A zero branch says its two nodes are one point, so they become one vertex [adr-0028].
    # The graph is a tree, so joining along its edges never closes a cycle: no kept edge can
    # end where it starts, and no two kept edges join the same pair of points.
    point = list(range(len(retained_nodes)))

    def find(node: int) -> int:
        while point[node] != node:
            point[node] = point[point[node]]
            node = point[node]
        return node

    for (source, target), length in zip(graph_edges, lengths, strict=True):
        if length == 0.0:
            # The smaller index represents the point, so its name is the first node's.
            low, high = sorted((find(source), find(target)))
            point[high] = low
    representatives = sorted({find(node) for node in range(len(retained_nodes))})
    vertex = {representative: index for index, representative in enumerate(representatives)}
    kept = [
        ((vertex[find(source)], vertex[find(target)]), length)
        for (source, target), length in zip(graph_edges, lengths, strict=True)
        if length != 0.0
    ]
    graph = ig.Graph(n=len(representatives), edges=[edge for edge, _ in kept], directed=False)
    graph.vs["name"] = [retained_nodes[representative] for representative in representatives]
    graph.es["weight"] = _edge_weights([length for _, length in kept])
    return GraphInput(
        graph=graph,
        vertex_names=tuple(retained_nodes[representative] for representative in representatives),
        object_ids=leaves,
        labels=labels,
        vertex_of={leaf: vertex[find(index[leaf])] for leaf in leaves},
    )


def load_tree_graph(path: Path) -> GraphInput:
    try:
        return _load_tree_graph(path)
    except ClusterizerError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ClusterizerError(
            "Tree artifact does not match schema version 1",
            code="tree_format_error",
        ) from exc
