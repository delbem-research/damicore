import itertools
import random
from collections.abc import Sequence
from fractions import Fraction

import pytest

from damicore_normalizer.natural_breaks import natural_breaks

pytestmark = pytest.mark.unit


def _sdcm(rows: Sequence[int], bounds: Sequence[int]) -> Fraction:
    total = Fraction(0)
    for start, stop in itertools.pairwise(bounds):
        block = rows[start:stop]
        mean = Fraction(sum(block), len(block))
        total += sum(((value - mean) ** 2 for value in block), Fraction(0))
    return total


def _brute_force(rows: Sequence[int], classes: int) -> tuple[int, ...]:
    """Every contiguous partition of the sorted rows, ties unconstrained.

    The tie-break is the specification's declarative rule, implemented here without any
    dynamic program: among equal costs, the smallest last inner boundary, then the one
    before it, and so on. Enumerating rows rather than distinct values is what checks the
    claim that the weighted optimum attains the unconstrained one.
    """
    count = len(rows)
    best: tuple[Fraction, tuple[int, ...], tuple[int, ...]] | None = None
    for cuts in itertools.combinations(range(1, count), classes - 1):
        bounds = (0, *cuts, count)
        key = (_sdcm(rows, bounds), tuple(reversed(cuts)), bounds)
        if best is None or key < best:
            best = key
    assert best is not None
    return best[2]


def _weighted(rows: Sequence[int]) -> tuple[list[int], list[int]]:
    distinct: list[int] = []
    weights: list[int] = []
    for value in rows:
        if distinct and distinct[-1] == value:
            weights[-1] += 1
        else:
            distinct.append(value)
            weights.append(1)
    return distinct, weights


def _row_bounds(weights: Sequence[int], bounds: Sequence[int]) -> tuple[int, ...]:
    cumulative = [0]
    for weight in weights:
        cumulative.append(cumulative[-1] + weight)
    return tuple(cumulative[index] for index in bounds)


def _weighted_sdcm(
    values: Sequence[int], weights: Sequence[int], bounds: Sequence[int]
) -> Fraction:
    rows = [value for value, weight in zip(values, weights, strict=True) for _ in range(weight)]
    return _sdcm(rows, _row_bounds(weights, bounds))


def _plain_dynamic_program(
    values: Sequence[int], weights: Sequence[int], classes: int
) -> tuple[int, ...]:
    """The O(K d^2) program over the same exact costs with the same leftmost argmin.

    A different algorithm for the same definition, which is what makes it an oracle for the
    monotone-argmin optimization rather than a second copy of it.
    """
    count = len(values)
    mass = [0] * (count + 1)
    linear = [0] * (count + 1)
    quadratic = [0] * (count + 1)
    for index, (value, weight) in enumerate(zip(values, weights, strict=True), start=1):
        mass[index] = mass[index - 1] + weight
        linear[index] = linear[index - 1] + weight * value
        quadratic[index] = quadratic[index - 1] + weight * value * value

    def cost(start: int, stop: int) -> Fraction:
        span = mass[stop] - mass[start]
        first = linear[stop] - linear[start]
        return Fraction(span * (quadratic[stop] - quadratic[start]) - first * first, span)

    previous = [Fraction(0)] + [cost(0, stop) for stop in range(1, count + 1)]
    argmins: list[list[int]] = []
    for layer in range(2, classes + 1):
        current = [Fraction(0)] * (count + 1)
        argmin = [0] * (count + 1)
        for stop in range(layer, count + 1):
            best_split = layer - 1
            best = previous[best_split] + cost(best_split, stop)
            for split in range(layer, stop):
                candidate = previous[split] + cost(split, stop)
                if candidate < best:
                    best, best_split = candidate, split
            current[stop] = best
            argmin[stop] = best_split
        argmins.append(argmin)
        previous = current
    bounds = [count]
    position = count
    for layer in range(classes, 1, -1):
        position = argmins[layer - 2][position]
        bounds.append(position)
    bounds.append(0)
    return tuple(reversed(bounds))


def test_natural_breaks_match_brute_force_over_rows_including_ties() -> None:
    rng = random.Random(11)
    checked = 0
    for _ in range(300):
        rows = sorted((rng.randint(0, 4) for _ in range(rng.randint(3, 10))), reverse=True)
        distinct, weights = _weighted(rows)
        largest = min(len(distinct), 4)
        if largest < 2:
            continue
        results = natural_breaks(distinct, weights, largest)
        for classes in range(2, largest + 1):
            expected = _brute_force(rows, classes)
            assert _row_bounds(weights, results[classes - 2]) == expected
            checked += 1
    assert checked > 200


@pytest.mark.parametrize(
    ("values", "classes", "expected"),
    [
        # {10}|{5,0} and {10,5}|{0} cost the same; the rule takes the smallest last boundary.
        pytest.param([10, 5, 0], 2, (0, 1, 3), id="two-way-tie"),
        # Three optimal partitions of cost 1/2; the smallest bounds[2] is 2, so (0, 1, 2, 4).
        pytest.param([3, 2, 1, 0], 3, (0, 1, 2, 4), id="three-way-tie"),
    ],
)
def test_ties_between_optimal_partitions_break_toward_the_smallest_last_boundary(
    values: list[int], classes: int, expected: tuple[int, ...]
) -> None:
    weights = [1] * len(values)
    assert natural_breaks(values, weights, classes)[classes - 2] == expected
    assert _brute_force(values, classes) == expected


def test_natural_breaks_agree_with_the_plain_dynamic_program_at_hundreds_of_values() -> None:
    rng = random.Random(5)
    for _ in range(3):
        values = sorted(rng.sample(range(-1_000_000, 1_000_000), 150), reverse=True)
        weights = [rng.randint(1, 3) for _ in values]
        results = natural_breaks(values, weights, 4)
        for classes in range(2, 5):
            assert results[classes - 2] == _plain_dynamic_program(values, weights, classes)


def test_sdcm_never_increases_with_the_class_count() -> None:
    values = [90, 71, 70, 55, 40, 12, 11, 3]
    weights = [1, 2, 1, 3, 1, 1, 2, 1]
    results = natural_breaks(values, weights, 6)
    costs = [_weighted_sdcm(values, weights, bounds) for bounds in results]
    assert costs == sorted(costs, reverse=True)
    assert _weighted_sdcm(values, weights, natural_breaks(values, weights, 8)[6]) == 0


def test_the_partition_is_invariant_under_increasing_and_mirrored_under_decreasing_maps() -> None:
    values = [40, 31, 30, 29, 10, 1]
    weights = [1, 1, 1, 1, 1, 1]
    bounds = natural_breaks(values, weights, 3)[1]
    assert bounds == natural_breaks([3 * v + 7 for v in values], weights, 3)[1]
    mirrored = natural_breaks([-v for v in reversed(values)], weights, 3)[1]
    assert mirrored == tuple(len(values) - b for b in reversed(bounds))


def test_an_isolated_outlier_forms_its_own_class() -> None:
    assert natural_breaks([100, 3, 2, 1], [1, 1, 1, 1], 2)[0] == (0, 1, 4)


def test_as_many_classes_as_values_puts_each_value_alone() -> None:
    assert natural_breaks([5, 4, 3], [2, 1, 1], 3)[1] == (0, 1, 2, 3)


@pytest.mark.parametrize(
    ("values", "weights", "classes"),
    [
        pytest.param([3, 2, 1], [1, 1, 1], 1, id="one-class"),
        pytest.param([3, 2], [1, 1], 3, id="more-classes-than-values"),
        pytest.param([3, 2, 1], [1, 1], 2, id="weights-mismatch"),
        pytest.param([3, 2, 1], [1, 0, 1], 2, id="zero-weight"),
    ],
)
def test_a_violated_precondition_is_a_value_error(
    values: list[int], weights: list[int], classes: int
) -> None:
    with pytest.raises(ValueError):
        natural_breaks(values, weights, classes)
