from __future__ import annotations

import time
from pathlib import Path

from damicore_clusterizer.artifacts import write_cluster_artifacts
from damicore_clusterizer.config import ClusterConfig
from damicore_clusterizer.errors import ClusterizerError
from damicore_clusterizer.fastgreedy import fastgreedy_membership
from damicore_clusterizer.models import ClusterResult
from damicore_clusterizer.tree_graph import load_tree_graph


def cluster_tree(
    tree_path: str | Path,
    output_dir: str | Path,
    *,
    config: ClusterConfig | None = None,
) -> ClusterResult:
    """Cluster every node in an unrooted tree and project communities to leaves.

    Takes ``tree.json`` as the tree stage writes it and runs FastGreedy over the whole tree
    graph, internal nodes included, then keeps only the leaves of each community. A
    community made entirely of internal nodes therefore disappears, which is why
    ``cluster_count`` can be lower than ``community_count``. Nodes joined by a branch of
    length zero are one point: they become one vertex, and every object at it lands in the
    same cluster [adr-0028]. A tree that is a single point is one cluster, reported with
    modularity 0. Cluster numbers are assigned by sorting the groups by their object ids, so
    the numbering depends on the tree rather than on FastGreedy's internal ordering. Writes
    ``membership.csv`` and ``clusters.json`` into ``output_dir``; both must be absent, since
    neither is overwritten. ``output_dir`` is created before that check, so a call rejected
    for the conflict still leaves the directory behind.

    Raises
    ------
    ClusterizerError
        ``config.num_clusters`` exceeds the leaf count, or the number of distinct points the
        zero branches leave (``configuration_error``); the tree artifact is missing,
        unparsable, not a valid unrooted binary tree, or carries a nonzero branch length too
        small for a finite reciprocal weight (``tree_format_error``);
        the resulting communities do not cover every leaf
        (``clusterization_error``); the outputs already exist
        (``output_directory_conflict_error``).
    """
    started = time.monotonic()
    settings = config or ClusterConfig()
    source = load_tree_graph(Path(tree_path).resolve())
    if settings.num_clusters is not None and settings.num_clusters > len(source.object_ids):
        raise ClusterizerError(
            "num_clusters cannot exceed the leaf count", code="configuration_error"
        )
    points = len(source.vertex_names)
    if settings.num_clusters is not None and settings.num_clusters > points:
        raise ClusterizerError(
            f"num_clusters cannot exceed the {points} distinct point(s) the tree's zero "
            "branches leave",
            code="configuration_error",
        )
    if points == 1:
        # Every branch is zero: one point and no edge to weigh. One community holding every
        # edge has modularity 0 by convention, where igraph would report NaN, which no JSON
        # artifact can carry.
        membership, community_count, modularity = [0], 1, 0.0
    else:
        membership, community_count, modularity = fastgreedy_membership(
            source.graph,
            settings.num_clusters,
        )
    raw_leaf_groups: dict[int, list[str]] = {}
    for object_id in source.object_ids:
        vertex = source.vertex_of[object_id]
        if vertex < len(membership):
            raw_leaf_groups.setdefault(int(membership[vertex]), []).append(object_id)
    ordered_groups = sorted(
        (tuple(sorted(group)) for group in raw_leaf_groups.values() if group),
        key=lambda group: group,
    )
    cluster_for = {
        object_id: cluster for cluster, group in enumerate(ordered_groups) for object_id in group
    }
    if set(cluster_for) != set(source.object_ids):
        raise ClusterizerError("Cluster membership is incomplete", code="clusterization_error")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "membership.csv").exists() or (destination / "clusters.json").exists():
        raise ClusterizerError(
            "Cluster outputs already exist without a reusable receipt",
            code="output_directory_conflict_error",
        )
    membership_path, clusters_path = write_cluster_artifacts(
        destination,
        source.object_ids,
        source.labels,
        cluster_for,
        ordered_groups,
    )
    return ClusterResult(
        membership_path=membership_path,
        clusters_path=clusters_path,
        community_count=community_count,
        cluster_count=len(ordered_groups),
        modularity=modularity,
        timing=time.monotonic() - started,
    )
