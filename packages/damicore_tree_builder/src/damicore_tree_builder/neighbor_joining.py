"""Deterministic Neighbor Joining over a distance matrix.

Each round joins the pair with the smallest Q score, gives the joined pair its two branch
lengths, and replaces it with one cluster whose distance to every remaining node is the
published average. The split is written as one length plus its complement,
``right = D(i,j) - left``, which is the symmetric published form rearranged; the reduction
is ``0.5 * (D[i][k] + D[j][k] - D[i][j])``.

Both formulas are pinned numerically rather than by prose: Neighbor Joining is exact on an
additive matrix, so the five-taxon fixture in ``tests/test_neighbor_joining.py`` fails on
any change to the Q criterion, the split or the reduction. What is *not* pinned there is
which published work states each in which form, and that is a reading rather than a test.

References
----------
.. [saitou1987] The neighbor-joining method: a new method for reconstructing phylogenetic
   trees. The method this module implements -- pair selection, the branch-length split,
   and the reduction that replaces a joined pair with one cluster.
   Not read: free at the publisher, reachable by no automated route here; the delta is owed.
.. [studier1988] A note on the neighbor-joining algorithm of Saitou and Nei. [adr-0017]
   credits it with the Q reformulation computed below,
   ``(r - 2) * D[i][j] - row_sum(i) - row_sum(j)``; that credit itself has never been
   checked against the work.
   Not read: same access as [saitou1987]; the credit above is unchecked against the work.
.. [adr-0017] Which pair joins when two Q scores tie, and the relative band that makes the
   tie visible in float64. Neither work above states a tie rule, so this one is this
   project's and changes the tree a run reports from the same matrix.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

from damicore_tree_builder.errors import TreeBuilderError
from damicore_tree_builder.models import Tree, TreeEdge, TreeNode

# Two Q scores closer than this, relative to the magnitude of the better one, are one tie
# for the lexicographic rule to break.
#
# An exact float64 comparison asserts a precision the input never had: every distance here is
# a ratio of integer compressed sizes, so its meaningful digits run out long before the
# 16th. Worse, the ties the rule exists to break are precisely the ones rounding destroys --
# with four clusters left, Q(i,j) and Q(k,l) for complementary pairs are algebraically equal
# for *every* matrix, yet float64 detects that only about 60% of the time, and no summation
# strategy fixes it because the cancellation happens in the final subtraction rather than in
# the sums. A relative band restores the rule and makes the choice stable across platforms.
_TIE_RELATIVE_TOLERANCE = 1e-9


def _tie_band(score: float) -> float:
    """Half-width of the tie band around `score`, scaled to its magnitude."""
    return _TIE_RELATIVE_TOLERANCE * max(1.0, abs(score))


def validate_matrix(
    matrix: np.ndarray[Any, Any],
    labels: Sequence[str],
    block_size: int = 512,
) -> None:
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.shape[0] != len(labels):
        raise TreeBuilderError(
            "Distance matrix shape does not match labels", code="distance_matrix_validation_error"
        )
    if len(labels) < 2 or len(set(labels)) != len(labels):
        raise TreeBuilderError(
            "At least two unique labels are required", code="distance_matrix_validation_error"
        )
    if matrix.dtype != np.float64:
        raise TreeBuilderError(
            "Distance matrix must be float64",
            code="distance_matrix_validation_error",
        )
    size = len(labels)
    for row_start in range(0, size, block_size):
        row_stop = min(row_start + block_size, size)
        if not np.isfinite(matrix[row_start:row_stop]).all():
            raise TreeBuilderError(
                "Distance matrix must be finite",
                code="distance_matrix_validation_error",
            )
        for row in range(row_start, row_stop):
            if float(matrix[row, row]) != 0.0:
                raise TreeBuilderError(
                    "Distance matrix diagonal must be exactly zero",
                    code="distance_matrix_validation_error",
                )
            # pyright: ignore is on np.array_equal, whose numpy stub is partially
            # unknown under strict mode; the arrays themselves are fully typed.
            symmetric = np.array_equal(  # pyright: ignore[reportUnknownMemberType]
                matrix[row, :], matrix[:, row]
            )
            if not bool(symmetric):
                raise TreeBuilderError(
                    "Distance matrix must be bitwise symmetric",
                    code="distance_matrix_validation_error",
                )


def neighbor_joining(matrix: npt.NDArray[np.floating[Any]], labels: Sequence[str]) -> Tree:
    """Build a deterministic Neighbor Joining tree, copying the input matrix."""
    copied = np.array(matrix, dtype=np.float64, copy=True, order="C")
    validate_matrix(copied, labels)
    return build_neighbor_joining(copied, list(labels), q_block_size=512)


def build_neighbor_joining(
    work: np.ndarray[Any, Any],
    labels: list[str],
    display_labels: list[str] | None = None,
    *,
    q_block_size: int,
) -> Tree:
    active = list(labels)
    slots = {label: index for index, label in enumerate(labels)}
    rendered_labels = display_labels or labels
    nodes = [
        TreeNode(id=identifier, kind="leaf", label=rendered_labels[index])
        for index, identifier in enumerate(labels)
    ]
    edges: list[TreeEdge] = []
    internal_index = 1

    while len(active) > 2:
        active.sort()
        count = len(active)
        # Recomputed from the matrix each round rather than carried forward, so no drift
        # accumulates across iterations. The cost is O(r^2), which the Q scan below already
        # pays, so nothing changes asymptotically.
        row_sums = {
            node: float(sum(work[slots[node], slots[other]] for other in active if other != node))
            for node in active
        }
        # The smallest Q wins, and pairs within the tie band are resolved by the
        # smallest pair of IDs. `active` is sorted and pairs are visited in that order, so the
        # running pair is always the lexicographically earliest one seen so far; a later pair
        # therefore replaces it only by winning outright, never by tying with it.
        best_score = math.inf
        best_pair: tuple[str, str] | None = None
        for block_start in range(0, count, q_block_size):
            block_stop = min(block_start + q_block_size, count)
            for left_index in range(block_start, block_stop):
                left = active[left_index]
                for right in active[left_index + 1 :]:
                    score = (
                        (count - 2) * float(work[slots[left], slots[right]])
                        - row_sums[left]
                        - row_sums[right]
                    )
                    if best_pair is None or score < best_score - _tie_band(best_score):
                        best_pair = (left, right)
                        best_score = score
                    elif score < best_score:
                        # Inside the band: keep the earlier pair, tighten the band's centre.
                        best_score = score
        # `active` holds at least three labels here, so the pair loop above always ran and
        # `best_pair` is always set; asserting it is how the type narrows without a dead branch.
        assert best_pair is not None
        left, right = best_pair
        left_slot, right_slot = slots[left], slots[right]
        distance = float(work[left_slot, right_slot])
        delta = (row_sums[left] - row_sums[right]) / (count - 2)
        left_length = 0.5 * (distance + delta)
        right_length = distance - left_length
        internal = f"nj_{internal_index:06d}"
        internal_index += 1
        nodes.append(TreeNode(id=internal, kind="internal"))
        edges.extend(
            [
                TreeEdge(source=internal, target=left, length=left_length),
                TreeEdge(source=internal, target=right, length=right_length),
            ]
        )
        remaining = [node for node in active if node not in (left, right)]
        for other in remaining:
            other_slot = slots[other]
            left_distance = float(work[left_slot, other_slot])
            right_distance = float(work[right_slot, other_slot])
            updated = 0.5 * (left_distance + right_distance - distance)
            work[left_slot, other_slot] = updated
            work[other_slot, left_slot] = updated
        work[left_slot, left_slot] = 0.0
        slots.pop(left)
        slots.pop(right)
        slots[internal] = left_slot
        active = [*remaining, internal]

    active.sort()
    left, right = active
    final_length = float(work[slots[left], slots[right]]) / 2.0
    root = "nj_root"
    nodes.append(TreeNode(id=root, kind="internal"))
    edges.extend(
        [
            TreeEdge(source=root, target=left, length=final_length),
            TreeEdge(source=root, target=right, length=final_length),
        ]
    )
    ordered_edges = tuple(sorted(edges, key=lambda edge: (edge.source, edge.target)))
    return Tree(root_id=root, nodes=tuple(nodes), edges=ordered_edges)
