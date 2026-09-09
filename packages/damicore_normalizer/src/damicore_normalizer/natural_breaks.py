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
