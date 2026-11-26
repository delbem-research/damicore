import json
import os
import random
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import IO, Any

import numpy as np
import numpy.typing as npt
import pytest

import damicore_tree_builder.api as api
import damicore_tree_builder.artifacts as artifacts
from damicore_tree_builder import (
    Tree,
    TreeBuilderError,
    TreeEdge,
    TreeNode,
    build_tree,
    neighbor_joining,
)
from damicore_tree_builder.neighbor_joining import build_neighbor_joining
from damicore_tree_builder.newick import _escape, to_newick

pytestmark = pytest.mark.unit


def test_two_leaf_tree_preserves_half_distance() -> None:
    matrix = np.array([[0.0, 4.0], [4.0, 0.0]], dtype=np.float64)
    tree = neighbor_joining(matrix, ["a", "b"])
    assert [edge.length for edge in tree.edges] == [2.0, 2.0]
    assert tree.root_id == "nj_root"


# The additive five-taxon matrix of Saitou and Nei. Neighbor Joining is exact on an additive
# matrix, so this fixture pins the Q criterion, the delta split and the d(u,k) update at once:
# a wrong formula changes the topology, a branch length, or both.
ADDITIVE_LABELS = ["a", "b", "c", "d", "e"]
ADDITIVE_MATRIX = np.array(
    [
        [0.0, 5.0, 9.0, 9.0, 8.0],
        [5.0, 0.0, 10.0, 10.0, 9.0],
        [9.0, 10.0, 0.0, 8.0, 7.0],
        [9.0, 10.0, 8.0, 0.0, 3.0],
        [8.0, 9.0, 7.0, 3.0, 0.0],
    ],
    dtype=np.float64,
)
ADDITIVE_EDGES = [
    ("nj_000001", "a", 2.0),
    ("nj_000001", "b", 3.0),
    ("nj_000002", "c", 4.0),
    ("nj_000002", "nj_000001", 3.0),
    ("nj_000003", "d", 2.0),
    ("nj_000003", "e", 1.0),
    ("nj_root", "nj_000002", 1.0),
    ("nj_root", "nj_000003", 1.0),
]


def _leaf_path_length(tree: Tree, source: str, target: str) -> float:
    incident: dict[str, list[tuple[str, float]]] = {}
    for edge in tree.edges:
        incident.setdefault(edge.source, []).append((edge.target, edge.length))
        incident.setdefault(edge.target, []).append((edge.source, edge.length))
    pending = [(source, 0.0, "")]
    while pending:
        node, walked, previous = pending.pop()
        if node == target:
            return walked
        pending.extend(
            (neighbor, walked + length, node)
            for neighbor, length in incident[node]
            if neighbor != previous
        )
    raise AssertionError(f"{source} and {target} are not connected")


def test_additive_matrix_yields_the_expected_topology_and_branch_lengths() -> None:
    tree = neighbor_joining(ADDITIVE_MATRIX, ADDITIVE_LABELS)
    assert [(edge.source, edge.target, edge.length) for edge in tree.edges] == ADDITIVE_EDGES
    assert [node.id for node in tree.nodes] == [
        *ADDITIVE_LABELS,
        "nj_000001",
        "nj_000002",
        "nj_000003",
        "nj_root",
    ]


def test_additive_matrix_is_reconstructed_exactly_by_path_sums() -> None:
    """The independent check on the fixture above: on an additive matrix every leaf-to-leaf
    path in the result must sum back to the input distance with no floating-point slack."""
    tree = neighbor_joining(ADDITIVE_MATRIX, ADDITIVE_LABELS)
    for left in range(len(ADDITIVE_LABELS)):
        for right in range(left + 1, len(ADDITIVE_LABELS)):
            walked = _leaf_path_length(tree, ADDITIVE_LABELS[left], ADDITIVE_LABELS[right])
            assert walked == float(ADDITIVE_MATRIX[left, right])


def test_negative_branch_length_is_preserved_through_every_artifact(tmp_path: Path) -> None:
    """A negative length is a real Neighbor Joining outcome on a
    non-additive matrix and is reported, never truncated to zero."""
    matrix = np.array([[0.0, 1.0, 1.0], [1.0, 0.0, 10.0], [1.0, 10.0, 0.0]], dtype=np.float64)
    ids = ["a", "b", "c"]
    tree = neighbor_joining(matrix, ids)
    assert [edge.length for edge in tree.edges if edge.target == "a"] == [-4.0]
    assert "a:-4.0" in to_newick(tree)

    matrix_path = tmp_path / "distance.npy"
    np.save(matrix_path, matrix, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps({"schema_version": 1, "object_ids": ids, "labels": ids}),
        encoding="utf-8",
    )
    result = build_tree(matrix_path, labels_path, tmp_path)
    assert result.negative_branch_count == 1
    persisted = json.loads(result.tree_path.read_text(encoding="utf-8"))
    assert [edge for edge in persisted["edges"] if edge["length"] < 0] == [
        {"source": "nj_000001", "target": "a", "length": -4.0}
    ]
    assert "a:-4.0" in result.newick_path.read_text(encoding="utf-8")


def test_tie_break_and_path_api_are_deterministic(tmp_path: Path) -> None:
    matrix = np.array(
        [
            [0.0, 5.0, 9.0, 9.0],
            [5.0, 0.0, 10.0, 10.0],
            [9.0, 10.0, 0.0, 8.0],
            [9.0, 10.0, 8.0, 0.0],
        ],
        dtype=np.float64,
    )
    ids = ["a", "b", "c", "d"]
    in_memory = neighbor_joining(matrix, ids)
    matrix_path = tmp_path / "distance.npy"
    np.save(matrix_path, matrix, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps({"schema_version": 1, "object_ids": ids, "labels": ids}),
        encoding="utf-8",
    )
    result = build_tree(matrix_path, labels_path, tmp_path)
    persisted = json.loads(result.tree_path.read_text(encoding="utf-8"))
    assert persisted == in_memory.model_dump(mode="json")
    assert result.newick_path.read_text(encoding="utf-8").strip().endswith(";")
    assert not (tmp_path / "tree-work.npy").exists()
    with pytest.raises(TreeBuilderError, match="already exist"):
        build_tree(matrix_path, labels_path, tmp_path)


# Each row is one property a distance matrix must have. Keeping them separate means a
# regression names the property that broke rather than stopping at the first bad matrix.
def _matrix(rows: list[list[float]]) -> npt.NDArray[np.float64]:
    return np.array(rows, dtype=np.float64)


# Every guard here raises the same code, so the message is the only thing that says WHICH
# property broke. Without it the rows are interchangeable: delete the finiteness check and
# the nan row still passes, because np.array_equal reports NaN as unequal and the symmetry
# guard fires in its place.
@pytest.mark.parametrize(
    ("matrix", "labels", "discriminator"),
    [
        pytest.param(
            _matrix([[0.0, float("nan")], [float("nan"), 0.0]]),
            ["a", "b"],
            "must be finite",
            id="nan",
        ),
        pytest.param(
            _matrix([[0.0, 1.0], [2.0, 0.0]]), ["a", "b"], "bitwise symmetric", id="asymmetric"
        ),
        pytest.param(
            _matrix([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]),
            ["a", "b"],
            "shape does not match labels",
            id="not-square",
        ),
        pytest.param(
            _matrix([[0.0, 1.0], [1.0, 0.0]]),
            ["a", "a"],
            "two unique labels",
            id="duplicate-labels",
        ),
        pytest.param(
            _matrix([[1.0, 1.0], [1.0, 0.0]]),
            ["a", "b"],
            "diagonal must be exactly zero",
            id="non-zero-diagonal",
        ),
    ],
)
def test_invalid_matrix_is_rejected(
    matrix: npt.NDArray[np.float64], labels: list[str], discriminator: str
) -> None:
    with pytest.raises(TreeBuilderError, match=discriminator):
        neighbor_joining(matrix, labels)


def test_a_float32_matrix_is_converted_rather_than_rejected() -> None:
    converted = neighbor_joining(np.zeros((2, 2), dtype=np.float32), ["a", "b"])
    assert converted.root_id == "nj_root"


def test_labels_artifact_and_newick_escaping(tmp_path: Path) -> None:
    matrix = np.array([[0.0, 2.0], [2.0, 0.0]], dtype=np.float64)
    np.save(tmp_path / "distance.npy", matrix, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    labels = tmp_path / "labels.json"
    labels.write_text("not-json", encoding="utf-8")
    with pytest.raises(TreeBuilderError, match="labels"):
        build_tree(tmp_path / "distance.npy", labels, tmp_path)
    tree = neighbor_joining(matrix, ["a space", "b"])
    assert "'a space'" in to_newick(tree)


def test_labels_schema_rejects_coercion_and_extra_fields(tmp_path: Path) -> None:
    matrix = np.array([[0.0, 2.0], [2.0, 0.0]], dtype=np.float64)
    np.save(tmp_path / "distance.npy", matrix, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "object_ids": [1, "b"],
                "labels": ["a", "b"],
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TreeBuilderError, match="labels"):
        build_tree(tmp_path / "distance.npy", labels, tmp_path / "tree-output")


def _artifacts(
    tmp_path: Path, ids: list[str], labels: list[str] | None = None
) -> tuple[Path, Path]:
    """Write the matrix and labels artifacts build_tree reads, sized to `ids`."""
    size = len(ids)
    matrix = np.full((size, size), 2.0, dtype=np.float64)
    np.fill_diagonal(matrix, 0.0)  # pyright: ignore[reportUnknownMemberType]
    matrix_path = tmp_path / "distance.npy"
    np.save(matrix_path, matrix, allow_pickle=False)  # pyright: ignore[reportUnknownMemberType]
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps({"schema_version": 1, "object_ids": ids, "labels": labels or ids}),
        encoding="utf-8",
    )
    return matrix_path, labels_path


# object_ids and labels index the same objects positionally, so any of these breaks that pairing.
INCONSISTENT_LABEL_ARTIFACTS = [
    pytest.param(["a", "b"], ["only-one"], id="length-mismatch"),
    pytest.param(["a", "a"], ["A", "B"], id="duplicate-object-ids"),
    pytest.param(["a", "b"], ["same", "same"], id="duplicate-labels"),
]


@pytest.mark.parametrize(("ids", "labels"), INCONSISTENT_LABEL_ARTIFACTS)
def test_an_inconsistent_labels_artifact_is_rejected(
    tmp_path: Path, ids: list[str], labels: list[str]
) -> None:
    """Schema validation accepts any two string tuples; their pairing is a separate contract."""
    matrix_path, labels_path = _artifacts(tmp_path, ids, labels)
    with pytest.raises(TreeBuilderError, match="equal length and unique") as raised:
        build_tree(matrix_path, labels_path, tmp_path / "out")
    assert raised.value.code == "artifact_validation_error"


def test_an_unreadable_matrix_artifact_is_reported_as_a_typed_error(tmp_path: Path) -> None:
    """np.load raises OSError or ValueError on a file that is not a valid .npy; the caller
    must receive this package's typed error instead."""
    _, labels_path = _artifacts(tmp_path, ["a", "b"])
    corrupt = tmp_path / "corrupt.npy"
    corrupt.write_bytes(b"not a numpy file")
    with pytest.raises(TreeBuilderError, match="unreadable") as raised:
        build_tree(corrupt, labels_path, tmp_path / "out")
    assert raised.value.code == "artifact_validation_error"


def test_a_non_float64_matrix_artifact_is_rejected(tmp_path: Path) -> None:
    """The in-memory API converts, but a persisted matrix is memory-mapped and used as it
    lies on disk, so its dtype is part of the artifact contract."""
    ids = ["a", "b"]
    matrix_path = tmp_path / "distance.npy"
    np.save(  # pyright: ignore[reportUnknownMemberType]
        matrix_path, np.zeros((2, 2), dtype=np.float32), allow_pickle=False
    )
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps({"schema_version": 1, "object_ids": ids, "labels": ids}), encoding="utf-8"
    )
    with pytest.raises(TreeBuilderError, match="float64") as raised:
        build_tree(matrix_path, labels_path, tmp_path / "out")
    assert raised.value.code == "distance_matrix_validation_error"


# build_tree re-reads both artifacts after writing them, so each row corrupts one of them
# between the write and that read: the check exists to catch a serializer that silently
# dropped content, which is invisible to the in-memory tree it was built from.
def _drop_a_leaf(tree_path: Path, newick_path: Path) -> None:
    payload = json.loads(tree_path.read_text(encoding="utf-8"))
    payload["nodes"] = [node for node in payload["nodes"] if node["kind"] != "leaf"]
    tree_path.write_text(json.dumps(payload), encoding="utf-8")


def _strip_the_newick_terminator(tree_path: Path, newick_path: Path) -> None:
    newick_path.write_text(
        newick_path.read_text(encoding="utf-8").replace(";", ""), encoding="utf-8"
    )


PERSISTED_ARTIFACT_CORRUPTIONS = [
    pytest.param(_drop_a_leaf, "lost leaves", id="tree-json-missing-a-leaf"),
    pytest.param(_strip_the_newick_terminator, "Newick", id="newick-without-terminator"),
]


@pytest.mark.parametrize(("corrupt", "discriminator"), PERSISTED_ARTIFACT_CORRUPTIONS)
def test_a_corrupted_persisted_artifact_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt: Callable[[Path, Path], None],
    discriminator: str,
) -> None:
    matrix_path, labels_path = _artifacts(tmp_path, ["a", "b", "c"])
    real_write = api.write_tree_artifacts

    def corrupting_write(tree: Tree, destination: Path) -> tuple[Path, Path]:
        tree_path, newick_path = real_write(tree, destination)
        corrupt(tree_path, newick_path)
        return tree_path, newick_path

    monkeypatch.setattr(api, "write_tree_artifacts", corrupting_write)
    with pytest.raises(TreeBuilderError, match=discriminator) as raised:
        build_tree(matrix_path, labels_path, tmp_path / "out")
    assert raised.value.code == "artifact_validation_error"


def test_a_failed_tree_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both artifacts go through the same atomic helper, so a failure at the rename must not
    leave the partial file behind."""
    matrix_path, labels_path = _artifacts(tmp_path, ["a", "b", "c"])
    output = tmp_path / "out"

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated failure while committing the tree")

    monkeypatch.setattr(artifacts.os, "replace", failing_replace)
    with pytest.raises(OSError):
        build_tree(matrix_path, labels_path, output)
    assert not list(output.glob(".tree.*"))


# At three remaining nodes every pair has Q = -(d_ij + d_ik + d_jk), so all three are exactly
# equal whatever the distances are. The tie rule then mandates the lexicographically smallest
# ID pair. Row sums are recomputed each round precisely so this equality survives as an exact
# float64 tie: maintained incrementally they drift, and drift, not the rule, picks the pair.
@pytest.mark.parametrize(
    "order",
    [
        pytest.param(["a", "b", "c"], id="already-sorted"),
        pytest.param(["c", "b", "a"], id="reversed"),
        pytest.param(["b", "c", "a"], id="rotated"),
    ],
)
def test_an_exact_q_tie_is_broken_by_the_smallest_id_pair(order: list[str]) -> None:
    distances = {("a", "b"): 7.0, ("a", "c"): 3.0, ("b", "c"): 5.0}

    def distance(left: str, right: str) -> float:
        if left == right:
            return 0.0
        return distances[(left, right)] if (left, right) in distances else distances[(right, left)]

    matrix = np.array(
        [[distance(left, right) for right in order] for left in order], dtype=np.float64
    )
    tree = neighbor_joining(matrix, order)
    joined = {edge.target for edge in tree.edges if edge.source == "nj_000001"}
    assert joined == {"a", "b"}


# With four clusters left, Q(i,j) and Q(k,l) for complementary pairs are algebraically equal
# for every matrix: R_i+R_l and R_j+R_k expand to the same sum, so the two Q values cancel to
# each other. float64 loses that equality on roughly 40% of inputs, which is why the tie rule
# compares within a relative band. Fractions reproduce the rule exactly, so they are the
# authority the implementation is checked against.
# Seeds 11, 16 and 22 are the cases where a later pair scores marginally BELOW the running
# best while staying inside the band -- the branch that keeps the earlier pair and only
# recentres the band, which is the whole reason the band exists.
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7, 11, 16, 22])
def test_the_first_join_follows_the_rule_a_float_comparison_would_lose(seed: int) -> None:
    generator = np.random.default_rng(seed)
    values = generator.random((4, 4)) * 10
    matrix = (values + values.T) / 2
    np.fill_diagonal(matrix, 0.0)  # pyright: ignore[reportUnknownMemberType]
    labels = ["obj_000", "obj_001", "obj_002", "obj_003"]

    distances = {
        (left, right): Fraction(float(matrix[i][j]))
        for i, left in enumerate(labels)
        for j, right in enumerate(labels)
    }
    sums = {
        node: sum(distances[(node, other)] for other in labels if other != node) for node in labels
    }
    scored = sorted(
        (2 * distances[(left, right)] - sums[left] - sums[right], left, right)
        for index, left in enumerate(labels)
        for right in labels[index + 1 :]
    )
    _, expected_left, expected_right = scored[0]

    tree = neighbor_joining(matrix, labels)
    joined = {edge.target for edge in tree.edges if edge.source == "nj_000001"}
    assert joined == {expected_left, expected_right}


def _exact_branch_lengths(
    matrix: npt.NDArray[np.float64], labels: list[str], tree: Tree
) -> dict[tuple[str, str], Fraction]:
    """Replay the joins `tree` made, in Fractions, and return every branch's exact length.

    The joins are read from the tree rather than re-chosen, so this checks the lengths and
    the reduction independently of the tie rule.
    """
    distance = {
        (left, right): Fraction(float(matrix[i][j]))
        for i, left in enumerate(labels)
        for j, right in enumerate(labels)
    }
    children: dict[str, list[str]] = {}
    for edge in tree.edges:
        children.setdefault(edge.source, []).append(edge.target)
    active = list(labels)
    exact: dict[tuple[str, str], Fraction] = {}
    for internal in sorted(node for node in children if node != tree.root_id):
        left, right = children[internal]
        sums = {
            node: sum((distance[(node, other)] for other in active if other != node), Fraction(0))
            for node in active
        }
        pair = distance[(left, right)]
        delta = (sums[left] - sums[right]) / (len(active) - 2)
        exact[(internal, left)] = (pair + delta) / 2
        exact[(internal, right)] = (pair - delta) / 2
        active = [node for node in active if node not in (left, right)]
        for other in active:
            reduced = (distance[(left, other)] + distance[(right, other)] - pair) / 2
            distance[(internal, other)] = distance[(other, internal)] = reduced
        active.append(internal)
    left, right = active
    exact[(tree.root_id, left)] = exact[(tree.root_id, right)] = distance[(left, right)] / 2
    return exact


# Three objects with one distance profile form a star, which a binary tree draws as two joins
# with a branch between them that is zero in exact arithmetic. float64 leaves that branch as
# a residue of either sign, and the residue alone would decide what the clusterizer does with
# it: refuse a 0.0, weaken a negative, or make a positive the strongest edge [adr-0028].
NOISY_STARS = [
    pytest.param((0.66, 0.18, 0.83, 0.54), id="residue-below-zero"),
    pytest.param((0.52, 0.32, 0.53, 0.55), id="residue-above-zero"),
]


@pytest.mark.parametrize("values", NOISY_STARS)
def test_a_branch_is_zero_exactly_when_its_exact_length_is_zero(
    values: tuple[float, float, float, float],
) -> None:
    between, within, from_a, from_b = values
    labels = ["a", "b", "z1", "z2", "z3"]
    matrix = np.array(
        [
            [0.0, between, from_a, from_a, from_a],
            [between, 0.0, from_b, from_b, from_b],
            [from_a, from_b, 0.0, within, within],
            [from_a, from_b, within, 0.0, within],
            [from_a, from_b, within, within, 0.0],
        ],
        dtype=np.float64,
    )
    tree = neighbor_joining(matrix, labels)
    exact = _exact_branch_lengths(matrix, labels, tree)

    assert sum(length == 0 for length in exact.values()) == 1
    for edge in tree.edges:
        expected = exact[(edge.source, edge.target)]
        if expected == 0:
            # Positive zero, so tree.json and tree.nwk never print a signed residue.
            assert (edge.length, np.copysign(1.0, edge.length)) == (0.0, 1.0), edge
        else:
            assert edge.length == pytest.approx(float(expected), rel=1e-12), edge


def test_branches_far_from_zero_are_left_as_computed() -> None:
    """The additive fixture has no branch near zero, so snapping must leave every length
    bit-for-bit as the formulas compute them."""
    tree = neighbor_joining(ADDITIVE_MATRIX, ADDITIVE_LABELS)
    exact = _exact_branch_lengths(ADDITIVE_MATRIX, ADDITIVE_LABELS, tree)
    assert all(length != 0 for length in exact.values())
    assert [edge.length for edge in tree.edges] == [
        float(exact[(edge.source, edge.target)]) for edge in tree.edges
    ]


@pytest.mark.parametrize("q_block_size", [1, 2, 3, 512])
def test_the_q_block_size_does_not_change_the_tree(q_block_size: int) -> None:
    """Blocking bounds how much of the Q scan is in flight, never
    which pair wins. Every existing test uses the default on a matrix smaller than one block,
    so the loop never took a second iteration and a blocking bug would have been invisible."""
    generator = np.random.default_rng(5)
    values = generator.random((8, 8)) * 10
    matrix = (values + values.T) / 2
    np.fill_diagonal(matrix, 0.0)  # pyright: ignore[reportUnknownMemberType]
    labels = [f"obj_{index:03d}" for index in range(8)]

    reference = build_neighbor_joining(
        np.array(matrix, dtype=np.float64, copy=True), list(labels), q_block_size=512
    )
    blocked = build_neighbor_joining(
        np.array(matrix, dtype=np.float64, copy=True), list(labels), q_block_size=q_block_size
    )
    assert blocked.model_dump(mode="json") == reference.model_dump(mode="json")


def test_identifiers_with_underscores_are_quoted() -> None:
    """Newick turns an unquoted underscore into a blank on reading, and every id this package
    emits carries one, so leaving them unquoted hands a conforming viewer `column 000001`."""
    matrix = np.array([[0.0, 5.0, 9.0], [5.0, 0.0, 10.0], [9.0, 10.0, 0.0]], dtype=np.float64)
    newick = to_newick(neighbor_joining(matrix, ["column_000001", "column_000002", "obj"]))
    assert "'column_000001'" in newick
    assert "'nj_root'" in newick
    # A label with no character needing an escape stays bare.
    assert "obj:" in newick and "'obj'" not in newick


def _ladder(depth: int) -> Tree:
    """A caterpillar of nesting depth `depth`: each internal node holds a leaf and the next.

    Neighbor Joining produces exactly this shape whenever a single taxon joins per round, so
    tree depth tracks the object count instead of its logarithm.
    """
    nodes = [TreeNode(id="nj_root", kind="internal")]
    edges: list[TreeEdge] = []
    for level in range(depth):
        parent = "nj_root" if level == 0 else f"internal_{level}"
        leaf = f"leaf_{level}"
        child = f"internal_{level + 1}"
        nodes.append(TreeNode(id=leaf, kind="leaf", label=leaf))
        nodes.append(TreeNode(id=child, kind="internal"))
        edges.append(TreeEdge(source=parent, target=leaf, length=0.5))
        edges.append(TreeEdge(source=parent, target=child, length=0.25))
    last = f"leaf_{depth}"
    nodes.append(TreeNode(id=last, kind="leaf", label=last))
    edges.append(TreeEdge(source=f"internal_{depth}", target=last, length=0.5))
    return Tree(root_id="nj_root", nodes=tuple(nodes), edges=tuple(edges))


def _random_tree(seed: int, size: int = 40) -> Tree:
    """Grow a rooted tree by attaching each new node to an arbitrary existing one."""
    rng = random.Random(seed)
    names = ["nj_root"]
    edges: list[TreeEdge] = []
    for index in range(size):
        parent = rng.choice(names)
        node_id = rng.choice(["obj", "col_", "a;b", "x y", "q'z"]) + str(index)
        names.append(node_id)
        edges.append(TreeEdge(source=parent, target=node_id, length=rng.uniform(-1.0, 5.0)))
    with_children = {edge.source for edge in edges}
    return Tree(
        root_id="nj_root",
        nodes=tuple(
            TreeNode(
                id=name,
                kind="internal" if name in with_children else "leaf",
                label=None if name in with_children else name,
            )
            for name in names
        ),
        edges=tuple(edges),
    )


def _reference_newick(tree: Tree) -> str:
    """The recursive renderer, kept here as the byte-for-byte oracle for the iterative one."""
    children: dict[str, list[tuple[str, float]]] = {}
    for edge in tree.edges:
        children.setdefault(edge.source, []).append((edge.target, edge.length))

    def render(node_id: str, length: float | None = None) -> str:
        descendants = sorted(children.get(node_id, []), key=lambda item: item[0])
        prefix = ""
        if descendants:
            prefix = "(" + ",".join(render(child, branch) for child, branch in descendants) + ")"
        value = prefix + _escape(node_id)
        if length is not None:
            value += ":" + repr(float(length))
        return value

    return render(tree.root_id) + ";"


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_iterative_rendering_is_byte_identical_to_the_recursive_reference(seed: int) -> None:
    """The renderer stopped recursing to survive deep trees; the string it emits is a
    published artifact, so the change has to be provably invisible in the output."""
    tree = _random_tree(seed)
    assert to_newick(tree) == _reference_newick(tree)


def test_a_tree_deeper_than_the_recursion_limit_is_rendered() -> None:
    """601 leaves is well inside the supported object count, and a renderer spending a stack
    frame per level fails on it."""
    newick = to_newick(_ladder(600))
    assert newick.endswith(";")
    assert newick.count("'leaf_") == 601
    # One parenthesised group per internal node: the root plus one per ladder rung.
    assert newick.count("(") == 601
    assert newick.count(")") == 601


def test_a_shallow_ladder_renders_the_expected_nesting() -> None:
    """Pins the shape the deep case can only assert by counting."""
    assert to_newick(_ladder(2)) == (
        "((('leaf_2':0.5)'internal_2':0.25,'leaf_1':0.5)'internal_1':0.25,'leaf_0':0.5)'nj_root';"
    )


def test_a_cycle_in_the_edges_is_rejected_rather_than_looping() -> None:
    """An iterative traversal has no stack limit to end a cycle for it, so it must detect one:
    the recursive renderer failed loudly here and a silent hang would be worse."""
    tree = Tree(
        root_id="nj_root",
        nodes=(TreeNode(id="nj_root", kind="internal"), TreeNode(id="a", kind="internal")),
        edges=(
            TreeEdge(source="nj_root", target="a", length=1.0),
            TreeEdge(source="a", target="nj_root", length=1.0),
        ),
    )
    with pytest.raises(TreeBuilderError, match="cycle") as raised:
        to_newick(tree)
    assert raised.value.code == "artifact_validation_error"


@pytest.mark.parametrize(
    "failing_rename",
    [pytest.param(1, id="tree-json"), pytest.param(2, id="tree-nwk")],
)
def test_a_failed_artifact_rename_leaves_the_directory_as_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_rename: int
) -> None:
    matrix_path, labels_path = _artifacts(tmp_path, ["a", "b", "c"])
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
        build_tree(matrix_path, labels_path, output)

    assert not list(output.glob(".tree.*"))
    # Neither artifact may survive a partial write: the next run refuses a directory holding
    # either one, so a stranded tree.json would need manual cleanup to recover from.
    assert not (output / "tree.json").exists()
    assert not (output / "tree.nwk").exists()
    # The scratch matrix is the same problem one file over: it is not refused by the next run,
    # but it is a full n-by-n float64 copy left in a directory the user owns.
    assert not (output / "tree-work.npy").exists()


def test_a_failure_before_the_artifacts_still_clears_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix_path, labels_path = _artifacts(tmp_path, ["a", "b", "c"])

    def failing_build(*args: object, **kwargs: object) -> Tree:
        raise OSError("simulated failure while joining")

    monkeypatch.setattr(api, "build_neighbor_joining", failing_build)
    output = tmp_path / "out"
    with pytest.raises(OSError):
        build_tree(matrix_path, labels_path, output)
    assert not (output / "tree-work.npy").exists()


def test_a_semicolon_inside_a_label_survives_the_persisted_artifact_check(tmp_path: Path) -> None:
    """The check counted semicolons in the rendered string, which rejected the quoted labels
    this package itself escapes."""
    matrix_path, labels_path = _artifacts(tmp_path, ["a;b", "c;d", "e"])
    result = build_tree(matrix_path, labels_path, tmp_path / "out")
    newick = result.newick_path.read_text(encoding="utf-8")
    assert "'a;b'" in newick
    assert newick.rstrip("\n").endswith(";")


# Staging happens before either rename, so a failure there must also leave nothing behind.
# The index selects which staged write fails: 1 is tree.json, 2 is tree.nwk.
@pytest.mark.parametrize(
    "failing_write",
    [pytest.param(1, id="staging-tree-json"), pytest.param(2, id="staging-tree-nwk")],
)
def test_a_failed_staged_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_write: int
) -> None:
    matrix_path, labels_path = _artifacts(tmp_path, ["a", "b", "c"])
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
        build_tree(matrix_path, labels_path, output)

    assert not list(output.glob(".tree.*"))
    assert not (output / "tree.json").exists()
    assert not (output / "tree.nwk").exists()
    assert not (output / "tree-work.npy").exists()
