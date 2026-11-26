import csv
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

import pytest
from pydantic import ValidationError

import damicore_clusterizer.api as api
import damicore_clusterizer.artifacts as artifacts
import damicore_clusterizer.tree_graph as tree_graph
from damicore_clusterizer import ClusterConfig, ClusterizerError, cluster_tree

pytestmark = pytest.mark.unit


def _tree(tmp_path: Path) -> Path:
    path = tmp_path / "tree.json"
    payload = {
        "schema_version": 1,
        "root_id": "nj_root",
        "nodes": [
            {"id": "a", "kind": "leaf", "label": "A"},
            {"id": "b", "kind": "leaf", "label": "B"},
            {"id": "c", "kind": "leaf", "label": "C"},
            {"id": "d", "kind": "leaf", "label": "D"},
            {"id": "i1", "kind": "internal", "label": None},
            {"id": "i2", "kind": "internal", "label": None},
            {"id": "nj_root", "kind": "internal", "label": None},
        ],
        "edges": [
            {"source": "i1", "target": "a", "length": -0.5},
            {"source": "i1", "target": "b", "length": 1.0},
            {"source": "i2", "target": "c", "length": 1.0},
            {"source": "i2", "target": "d", "length": 1.0},
            {"source": "nj_root", "target": "i1", "length": 2.0},
            {"source": "nj_root", "target": "i2", "length": 2.0},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_fastgreedy_projects_every_leaf_deterministically(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    first = cluster_tree(tree, tmp_path / "one", config=ClusterConfig(num_clusters=2))
    second = cluster_tree(tree, tmp_path / "two", config=ClusterConfig(num_clusters=2))
    assert first.community_count == 2
    assert first.clusters_path.read_bytes() == second.clusters_path.read_bytes()
    with first.membership_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["object_id"] for row in rows] == ["a", "b", "c", "d"]
    assert sorted({int(row["cluster"]) for row in rows}) == list(range(first.cluster_count))


def test_a_negative_branch_becomes_the_weakest_edge(tmp_path: Path) -> None:
    """Neighbor Joining emits a negative branch where the matrix contradicts the split, so
    that edge must be the weakest link in the graph. Weighting the lengths rather than their
    reciprocals made it the strongest, which is the inversion this fixture pins."""
    weights = _weights_by_edge(tmp_path)
    assert weights[frozenset(("a", "i1"))] == pytest.approx(1.0)
    assert weights[frozenset(("a", "i1"))] == pytest.approx(min(weights.values()))


def test_an_occupied_output_directory_is_rejected(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    cluster_tree(tree, tmp_path / "one", config=ClusterConfig(num_clusters=2))
    with pytest.raises(ClusterizerError, match="already exist"):
        cluster_tree(tree, tmp_path / "one", config=ClusterConfig(num_clusters=2))


def test_an_automatic_cut_yields_at_least_one_community(tmp_path: Path) -> None:
    automatic = cluster_tree(_tree(tmp_path), tmp_path / "automatic")
    assert automatic.community_count >= 1


def test_more_requested_clusters_than_leaves_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ClusterizerError, match="leaf count"):
        cluster_tree(
            _tree(tmp_path),
            tmp_path / "too-many",
            config=ClusterConfig(num_clusters=5),
        )


# JSON payloads are dynamic by nature, hence the Any in the mutation signature.
TreeMutation = Callable[[dict[str, Any]], object]


def _leave_one_leaf(value: dict[str, Any]) -> None:
    """Demote every leaf but the first, so the graph carries fewer than two objects."""
    for node in value["nodes"][1:4]:
        node.update(kind="internal")


def _add_redundant_edge(value: dict[str, Any]) -> None:
    """An extra edge breaks the n-1 count that makes the graph a tree."""
    value["edges"].append({"source": "i1", "target": "c", "length": 1.0})


def _strand_one_leaf(value: dict[str, Any]) -> None:
    """Point i2's second edge back at c, leaving d isolated while the edge count still
    equals n-1 -- the one shape that reaches the connectivity check rather than the count."""
    value["edges"][3].update(target="c")


def _overflow_root_merge(value: dict[str, Any]) -> None:
    """The root's two branches are summed into one edge, and that sum can leave float range
    even though each operand is finite, so the schema's allow_inf_nan=False cannot catch it."""
    value["edges"][4].update(length=1e308)
    value["edges"][5].update(length=1e308)


# Each row breaks one clause of the tree contract and names the message that clause raises.
# The discriminator is what makes the row test the clause it is named for: without it every
# row asserts only "some ClusterizerError", and several would pass through a different guard
# entirely -- `duplicate-node` still raised with its own guard deleted, and the old
# `missing-edge` row popped a ROOT edge and so never reached the edge-count check at all.
TREE_CONTRACT_MUTATIONS: list[tuple[str, TreeMutation, str]] = [
    ("wrong-schema-version", lambda value: value.update(schema_version=2), "Invalid tree artifact"),
    ("unknown-root", lambda value: value.update(root_id="missing"), "root_id names no node"),
    (
        "duplicate-node",
        lambda value: value["nodes"].append(value["nodes"][0]),
        "node ids are not unique",
    ),
    (
        "unknown-node-kind",
        lambda value: value["nodes"][0].update(kind="unknown"),
        "Invalid tree artifact",
    ),
    (
        "root-as-leaf",
        lambda value: value["nodes"][-1].update(kind="leaf", label="root"),
        "Tree root must be internal",
    ),
    (
        "leaf-without-label",
        lambda value: value["nodes"][0].update(label=None),
        "Every leaf must have a string label",
    ),
    ("node-without-id", lambda value: value["nodes"][0].pop("id"), "Invalid tree artifact"),
    (
        "missing-non-root-edge",
        lambda value: value["edges"].pop(0),
        "Unrooted tree must have n-1 edges",
    ),
    (
        "missing-root-edge",
        lambda value: value["edges"].pop(),
        "Tree root must have exactly two children",
    ),
    (
        "non-finite-length",
        lambda value: value["edges"][0].update(length=float("nan")),
        "Invalid tree artifact",
    ),
    (
        "dangling-edge",
        lambda value: value["edges"][0].update(target="missing"),
        "Tree edge references an unknown node",
    ),
    ("extra-field", lambda value: value.update(unexpected=True), "Invalid tree artifact"),
    # Structural clauses the schema alone cannot express: they hold over the assembled graph,
    # after the root's two edges are merged into the single unrooted edge.
    ("fewer-than-two-leaves", _leave_one_leaf, "at least two leaves"),
    ("edge-count-not-n-minus-one", _add_redundant_edge, "Unrooted tree must have n-1 edges"),
    ("disconnected-graph", _strand_one_leaf, "Tree graph is disconnected"),
    ("root-merge-overflows-to-infinity", _overflow_root_merge, "Branch lengths must be finite"),
]


@pytest.mark.parametrize(
    ("mutation", "discriminator"),
    [(mutation, discriminator) for _, mutation, discriminator in TREE_CONTRACT_MUTATIONS],
    ids=[name for name, _, _ in TREE_CONTRACT_MUTATIONS],
)
def test_invalid_tree_contract_is_rejected(
    tmp_path: Path, mutation: TreeMutation, discriminator: str
) -> None:
    source = _tree(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    mutation(payload)
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ClusterizerError, match=discriminator):
        cluster_tree(source, tmp_path / "invalid")


def test_a_membership_that_misses_a_leaf_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every leaf must land in exactly one cluster. A membership shorter than the vertex list
    silently drops the trailing leaves, so the completeness check is the only thing standing
    between that and a membership.csv missing rows the caller asked for."""

    def truncated_membership(
        graph: object, num_clusters: int | None
    ) -> tuple[list[int], int, float]:
        return ([0, 0, 0], 1, 0.5)

    monkeypatch.setattr(api, "fastgreedy_membership", truncated_membership)
    with pytest.raises(ClusterizerError, match="incomplete") as raised:
        cluster_tree(_tree(tmp_path), tmp_path / "out")
    assert raised.value.code == "clusterization_error"


def test_an_unexpected_graph_failure_is_reported_as_a_tree_format_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_tree_graph wraps the assembly step so a low-level failure from igraph or from
    float handling reaches the caller as this package's own typed error, never as a raw
    library exception the caller cannot classify."""

    def failing_graph(*args: object, **kwargs: object) -> object:
        raise TypeError("simulated igraph construction failure")

    monkeypatch.setattr(tree_graph.ig, "Graph", failing_graph)
    with pytest.raises(ClusterizerError, match="schema version 1") as raised:
        cluster_tree(_tree(tmp_path), tmp_path / "out")
    assert raised.value.code == "tree_format_error"


# Both artifacts are written through their own temporary file, so each rename is a separate
# place a partial file could survive a failure. The index selects which one fails.
@pytest.mark.parametrize(
    "failing_rename",
    [pytest.param(1, id="membership-csv"), pytest.param(2, id="clusters-json")],
)
def test_a_failed_artifact_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_rename: int
) -> None:
    renames = 0
    real_replace = os.replace

    def counting_replace(src: object, dst: object) -> None:
        nonlocal renames
        renames += 1
        if renames == failing_rename:
            raise OSError("simulated failure while committing an artifact")
        real_replace(src, dst)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(artifacts.os, "replace", counting_replace)
    output = tmp_path / "out"
    with pytest.raises(OSError):
        cluster_tree(_tree(tmp_path), output)

    assert not list(output.glob(".membership.*"))
    assert not list(output.glob(".clusters.json.*"))
    # Neither artifact may survive a partial write: the next run refuses a directory holding
    # either one, so a stranded membership.csv would need manual cleanup to recover from.
    assert not (output / "membership.csv").exists()
    assert not (output / "clusters.json").exists()


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.json"
    source.write_text("nope", encoding="utf-8")
    with pytest.raises(ClusterizerError):
        cluster_tree(source, tmp_path / "invalid")


def test_an_unknown_configuration_field_is_rejected() -> None:
    """A misspelled option must fail rather than be dropped: ClusterConfig(num_cluster=3)
    silently clustering with the default is worse than not running at all."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClusterConfig(num_cluster=3)  # pyright: ignore[reportCallIssue]


def test_the_root_is_removed_by_merging_its_two_edges(tmp_path: Path) -> None:
    """A degree-two root is not a real bifurcation, so its two edges
    become one edge between its children carrying the summed length, and every edge weight
    carries the summed length. This was unasserted while coverage read 100%."""
    graph_input = tree_graph.load_tree_graph(_tree(tmp_path))
    names = graph_input.vertex_names
    weights = _weights_by_edge(tmp_path)

    assert "nj_root" not in names
    # The fixture's root edges are 2.0 and 2.0, so one edge of length 4.0 joins i1 and i2.
    # Reciprocals are [-2, 1, 1, 1, 0.25]; their population deviation is sqrt(1.35), and each
    # weight is 1 + (reciprocal - -2) / that deviation.
    deviation = 1.35**0.5
    assert weights[frozenset(("i1", "i2"))] == pytest.approx(1.0 + 2.25 / deviation)
    assert weights[frozenset(("b", "i1"))] == pytest.approx(1.0 + 3.0 / deviation)


def test_a_branch_far_below_zero_is_weighted_rather_than_refused(tmp_path: Path) -> None:
    """A large negative branch used to exhaust the absolute 1e-12 epsilon and refuse a
    structurally valid tree. Weighting the reciprocals has no epsilon to exhaust, so the
    branch is simply the weakest edge however far below zero it sits."""
    payload = json.loads(_tree(tmp_path).read_text(encoding="utf-8"))
    payload["edges"][0].update(length=-1e5)
    source = tmp_path / "deep.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = cluster_tree(source, tmp_path / "out")
    assert result.cluster_count >= 1


def test_a_branch_whose_reciprocal_overflows_is_rejected(tmp_path: Path) -> None:
    """A nonzero length too small for its reciprocal to be a float has no usable weight.
    Repairing it silently would reintroduce the unbounded weight the formula exists to avoid,
    so it is refused with the reason named. A zero length is not this case: it is one point
    [adr-0028]."""
    payload = json.loads(_tree(tmp_path).read_text(encoding="utf-8"))
    payload["edges"][0].update(length=5e-324)
    source = tmp_path / "degenerate.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ClusterizerError, match="finite weight") as raised:
        cluster_tree(source, tmp_path / "out")
    assert raised.value.code == "tree_format_error"


def _clusters(result_path: Path) -> list[list[str]]:
    with result_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["cluster"], []).append(row["object_id"])
    return sorted(sorted(group) for group in grouped.values())


def test_leaves_joined_by_zero_branches_are_one_point_in_one_cluster(tmp_path: Path) -> None:
    """Two objects that no distance separates -- identical columns compressed by PPMd -- reach
    the tree as two zero branches under one node. They are one point, so they share a cluster,
    and the rest of the tree is partitioned as if the point were a single leaf."""
    payload = json.loads(_planted_tree(tmp_path / "planted.json", 1.0).read_text(encoding="utf-8"))
    for edge in payload["edges"]:
        if edge["source"] == "i1":
            edge["length"] = 0.0
    source = tmp_path / "coincident.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = cluster_tree(source, tmp_path / "out")
    assert _clusters(result.membership_path) == [sorted(group) for group in PLANTED_GROUPS]


def test_a_tree_that_is_one_point_is_one_cluster(tmp_path: Path) -> None:
    """Every branch zero leaves a single point and no edge for FastGreedy to weigh, which is
    one community, not a division by an empty spread."""
    payload = json.loads(_tree(tmp_path).read_text(encoding="utf-8"))
    for edge in payload["edges"]:
        edge["length"] = 0.0
    source = tmp_path / "point.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = cluster_tree(source, tmp_path / "out")
    assert (result.community_count, result.cluster_count, result.modularity) == (1, 1, 0.0)
    assert _clusters(result.membership_path) == [["a", "b", "c", "d"]]


def test_more_requested_clusters_than_points_is_rejected(tmp_path: Path) -> None:
    """Joining zero branches can leave fewer points than leaves, and FastGreedy cannot cut a
    dendrogram into more communities than it has points, so the count is refused by name."""
    payload = json.loads(_tree(tmp_path).read_text(encoding="utf-8"))
    for edge in payload["edges"]:
        edge["length"] = 0.0
    source = tmp_path / "point.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ClusterizerError, match="1 distinct point") as raised:
        cluster_tree(source, tmp_path / "out", config=ClusterConfig(num_clusters=2))
    assert raised.value.code == "configuration_error"


def test_equal_branch_lengths_give_every_edge_the_same_weight(tmp_path: Path) -> None:
    """With no spread there is no ordering to preserve, and the deviation the weights are
    divided by is zero. The limit is one weight for every edge, not a division by zero."""
    payload = json.loads(_tree(tmp_path).read_text(encoding="utf-8"))
    for edge in payload["edges"]:
        # The root's two edges are merged into one, so they carry half each for the collapsed
        # edge to match the rest.
        edge["length"] = 0.5 if edge["source"] == "nj_root" else 1.0
    source = tmp_path / "flat.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    graph = cast(Any, tree_graph.load_tree_graph(source).graph)
    assert [float(edge["weight"]) for edge in graph.es] == [1.0] * len(graph.es)


def test_rescaling_every_branch_leaves_the_weights_unchanged(tmp_path: Path) -> None:
    """The weights answer how branches compare, not what unit they are in, so a distance
    matrix in different units must cluster identically."""
    payload = json.loads(_tree(tmp_path).read_text(encoding="utf-8"))
    scaled = tmp_path / "scaled.json"
    for edge in payload["edges"]:
        edge["length"] *= 1000.0
    scaled.write_text(json.dumps(payload), encoding="utf-8")
    original = cast(Any, tree_graph.load_tree_graph(_tree(tmp_path)).graph)
    rescaled = cast(Any, tree_graph.load_tree_graph(scaled).graph)
    assert [float(e["weight"]) for e in rescaled.es] == pytest.approx(
        [float(e["weight"]) for e in original.es]
    )


# Staging happens before either rename, so a failure there must also leave nothing behind.
# The index selects which staged write fails: 1 is membership.csv, 2 is clusters.json.
@pytest.mark.parametrize(
    "failing_write",
    [pytest.param(1, id="staging-membership"), pytest.param(2, id="staging-clusters")],
)
def test_a_failed_staged_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_write: int
) -> None:
    writes = 0
    real_fdopen = os.fdopen

    def counting_fdopen(descriptor: int, mode: str = "r", **kwargs: Any) -> IO[Any]:
        nonlocal writes
        writes += 1
        if writes == failing_write:
            os.close(descriptor)
            raise OSError("simulated failure while staging an artifact")
        return real_fdopen(descriptor, mode, **kwargs)

    monkeypatch.setattr(artifacts.os, "fdopen", counting_fdopen)
    output = tmp_path / "out"
    with pytest.raises(OSError, match="simulated failure while staging an artifact"):
        cluster_tree(_tree(tmp_path), output)

    assert list(output.iterdir()) == []


def _weights_by_edge(tmp_path: Path) -> dict[frozenset[str], float]:
    """Edge weights of the fixture tree, keyed by the pair of node names each edge joins."""
    graph_input = tree_graph.load_tree_graph(_tree(tmp_path))
    names = graph_input.vertex_names
    # igraph's edge list and attribute access are untyped in the shipped stubs; the cast
    # isolates that boundary rather than letting Unknown leak into the assertions.
    graph = cast(Any, graph_input.graph)
    return {
        frozenset((names[int(edge.source)], names[int(edge.target)])): float(edge["weight"])
        for edge in graph.es
    }


# Three pairs, each under its own internal node, joined in a ladder. The structure is plain
# enough that any weighting which reads the tree at all must recover exactly these groups.
PLANTED_GROUPS = [("a", "b"), ("c", "d"), ("e", "f")]


def _planted_tree(path: Path, shortest: float) -> Path:
    """The planted tree with the a-i1 branch set to `shortest`, leaving the topology fixed."""
    payload = {
        "schema_version": 1,
        "nodes": [
            *({"id": leaf, "kind": "leaf", "label": leaf.upper()} for leaf in "abcdef"),
            *(
                {"id": inner, "kind": "internal", "label": None}
                for inner in ("i1", "i2", "i3", "ix", "nj_root")
            ),
        ],
        "root_id": "nj_root",
        "edges": [
            {"source": "i1", "target": "a", "length": shortest},
            {"source": "i1", "target": "b", "length": 1.0},
            {"source": "i2", "target": "c", "length": 1.0},
            {"source": "i2", "target": "d", "length": 1.0},
            {"source": "i3", "target": "e", "length": 1.0},
            {"source": "i3", "target": "f", "length": 1.0},
            {"source": "ix", "target": "i2", "length": 2.0},
            {"source": "ix", "target": "i3", "length": 2.0},
            {"source": "nj_root", "target": "i1", "length": 2.0},
            {"source": "nj_root", "target": "ix", "length": 2.0},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "shortest",
    [
        pytest.param(1.0, id="ordinary"),
        pytest.param(1e-1, id="short"),
        pytest.param(1e-3, id="very-short"),
        pytest.param(1e-6, id="near-zero"),
        pytest.param(0.0, id="zero"),
        pytest.param(-1e-3, id="slightly-negative"),
        pytest.param(-1.0, id="far-below-zero"),
    ],
)
def test_the_recovered_partition_does_not_depend_on_the_shortest_branch(
    tmp_path: Path, shortest: float
) -> None:
    """Modularity is a weighted sum, so an unbounded weight on one edge decides the partition
    by itself and the planted structure stops being visible. Only the a-i1 branch varies here
    and the topology never does, so recovering anything other than the planted groups means
    the weighting, not the tree, chose the answer.
    """
    source = _planted_tree(tmp_path / "planted.json", shortest)
    result = cluster_tree(source, tmp_path / "out")
    with result.membership_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(row["cluster"], set()).add(row["object_id"])
    assert sorted(map(sorted, grouped.values())) == [sorted(group) for group in PLANTED_GROUPS]
