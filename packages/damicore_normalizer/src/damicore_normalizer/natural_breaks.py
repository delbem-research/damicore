"""Exact Fisher-Jenks natural breaks over a sorted one-dimensional sample.

The grouping minimises the within-class sum of squares exactly, by dynamic programming with
a monotone-argmin divide-and-conquer inner solve, over distinct values carrying counts.

References
----------
.. [fisher1958] On Grouping for Maximum Homogeneity. The exact dynamic-programming grouping
   this module computes, carried forward from `docs/dataset-partitioning.md`.
   Not read: closed access and no open copy, so what this module leaves out is owed (#112).
.. [jenks1977] Optimal data classification for choropleth maps. Cited for one thing only:
   the natural-breaks formulation of the same objective, and the name a caller writes as
   `method="jenks"`.
   Not read: 1977 monograph, no DOI and no obtainable copy; cited for the name, not the rule.
.. [gronlund2017] Fast Exact k-Means, k-Medians and Bregman Divergence Clustering in 1D.
   Its Monge-concavity result, "CC(v, b) + CC(u, a) <= CC(u, b) + CC(v, a)", is what makes
   the monotone-argmin inner solve exact rather than a heuristic. What this module does not
   take is the speedup that result is used for there: the paper reduces the recurrence to
   O(kn) with a SMAWK-style row search, and this module keeps the divide-and-conquer solve
   at O(kn log n). Its k-medians and Bregman-divergence generalisations are not implemented
   either; only the squared-deviation case is.
   Read: 2026-11-07, primary.
.. [song2020] Efficient weighted univariate clustering. The weighted formulation -- sorted
   values with non-negative weights -- is what lets this module work over distinct values
   carrying counts instead of over every row. What it does not take: the paper selects the
   number of clusters by BIC under a Gaussian-mixture assumption, and this module returns
   the optimal grouping for every class count up to `max_classes` and selects none.
   Read: 2026-11-07, primary.
.. [wang2011] Ckmeans.1d.dp. The prior implementation of the same objective: an exact
   dynamic-programming solution to 1-D k-means, stated there at O(n^2 k) over n samples and
   written in C++ behind an R package. This module shares no code with it, is not a
   wrapper, and does not inherit that runtime, because its recurrence runs over distinct
   weighted values rather than over samples.
   Read: 2026-11-07, primary.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from fractions import Fraction


def natural_breaks(
    values: Sequence[int],
    weights: Sequence[int],
    max_classes: int,
) -> tuple[tuple[int, ...], ...]:
    """Fisher-Jenks natural breaks over weighted distinct values, exact, for every class count.

    ``values`` are distinct integers in the order the classes must respect and ``weights`` are
    their multiplicities. The objective is the sum over classes of the squared deviations of
    every weighted value from its class mean (SDCM), which is the same objective over the
    rows the weights stand for. Costs are exact rationals, so the result is identical on every
    platform. Among partitions attaining the minimum, the one returned has the smallest last
    boundary, then the smallest one before it, and so on: the leftmost argmin at every layer
    of the dynamic program realizes exactly that rule.

    Returns ``result[k - 2]`` for each ``k`` in ``2..max_classes``: the ``k + 1`` boundary
    indices into ``values``, from 0 to ``len(values)``, so class ``j`` is
    ``values[result[j]:result[j + 1]]``. One dynamic program to ``max_classes`` yields the
    optimum for every smaller ``k`` on the way.

    The divide-and-conquer over each layer costs ``O(d log d)`` rather than ``O(d^2)``. It is
    exact because the SDCM cost satisfies the quadrangle inequality, under which the leftmost
    argmin is non-decreasing in the prefix length.

    Raises
    ------
    ValueError
        Fewer than two classes, fewer distinct values than classes, a weight that is not
        positive, or mismatched lengths. Pure preconditions; the caller has already refused
        them with its own error class.
    """
    count = len(values)
    if max_classes < 2:
        raise ValueError("natural breaks need at least two classes")
    if count < max_classes:
        raise ValueError("natural breaks need at least as many distinct values as classes")
    if len(weights) != count or any(weight <= 0 for weight in weights):
        raise ValueError("every distinct value needs one positive weight")

    total = [0] * (count + 1)
    first = [0] * (count + 1)
    second = [0] * (count + 1)
    for index, (value, weight) in enumerate(zip(values, weights, strict=True), start=1):
        total[index] = total[index - 1] + weight
        first[index] = first[index - 1] + weight * value
        second[index] = second[index - 1] + weight * value * value

    def cost(start: int, stop: int) -> Fraction:
        # SDCM of the class values[start:stop] from its weighted mean, as an exact rational.
        mass = total[stop] - total[start]
        linear = first[stop] - first[start]
        quadratic = second[stop] - second[start]
        return Fraction(mass * quadratic - linear * linear, mass)

    previous = [Fraction(0)] + [cost(0, stop) for stop in range(1, count + 1)]
    argmins: list[list[int]] = []
    for layer in range(2, max_classes + 1):
        current = [Fraction(0)] * (count + 1)
        argmin = [0] * (count + 1)
        _solve_layer(layer, count, layer - 1, count - 1, previous, current, argmin, cost)
        argmins.append(argmin)
        previous = current

    results: list[tuple[int, ...]] = []
    for classes in range(2, max_classes + 1):
        bounds = [count]
        position = count
        for layer in range(classes, 1, -1):
            position = argmins[layer - 2][position]
            bounds.append(position)
        bounds.append(0)
        results.append(tuple(reversed(bounds)))
    return tuple(results)


def _solve_layer(
    low: int,
    high: int,
    option_low: int,
    option_high: int,
    previous: Sequence[Fraction],
    current: list[Fraction],
    argmin: list[int],
    cost: Callable[[int, int], Fraction],
) -> None:
    """Fill ``current[low..high]``, every optimal split lying in ``[option_low, option_high]``.

    Recursion depth is logarithmic in the range. The strict ``<`` keeps the first, smallest,
    split among equal costs, which is both the tie-break rule and what makes the argmin
    monotone under the quadrangle inequality.
    """
    if low > high:
        return
    middle = (low + high) // 2
    # option_low <= low - 1 <= middle - 1 holds on every call, so the first candidate exists.
    best_split = option_low
    best = previous[best_split] + cost(best_split, middle)
    for split in range(option_low + 1, min(middle - 1, option_high) + 1):
        candidate = previous[split] + cost(split, middle)
        if candidate < best:
            best, best_split = candidate, split
    current[middle] = best
    argmin[middle] = best_split
    _solve_layer(low, middle - 1, option_low, best_split, previous, current, argmin, cost)
    _solve_layer(middle + 1, high, best_split, option_high, previous, current, argmin, cost)
