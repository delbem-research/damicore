# ADR 0016: Branch lengths become edge weights by a rule this project owns

`damicore_clusterizer` hands FastGreedy a weighted graph built from the tree's branch
lengths. The mapping is `tree_graph._edge_weights`, and it is this project's own: no
published work specifies it, and it decides the partition, so it is recorded here rather
than left as an implementation detail of one function.

The reasoning is already in that function's docstring. This record exists because a
docstring is implementation, the lowest rank in the precedence order `AGENTS.md` states, and
because a unit whose basis is a decision rather than a published work has to cite that
decision by name.

## What it computes, and why each step is there

A shorter branch joins a closer pair, so a weight starts as the reciprocal of the length.
The reciprocals are then shifted so the smallest sits at zero, **divided by their standard
deviation**, and offset by one. A weight is therefore one plus the number of standard
deviations its reciprocal lies above the smallest, and the shortest branch weighs exactly
one.

The shift and the division do different work, and the division is the one that bounds the
result.

Modularity is a weighted sum, so a single unbounded weight would decide the partition by
itself, and a reciprocal is unbounded as the length approaches zero. Shifting alone does not
contain that: one branch of 1e-12 among five gives a largest weight of about 1e12. Dividing
by the standard deviation does, because the same branch that sends a reciprocal to infinity
sends the standard deviation there with it. Measured, the largest weight then grows with the
number of branches rather than with how short the shortest one is: about 3.5 for five
branches and about 8.1 for fifty, with the same 1e-12 branch among them. That is what stops
one edge from dominating the objective [clauset2004] maximizes.

The division is also what makes the weights independent of the unit the lengths are measured
in. Doubling every branch halves every reciprocal and halves their standard deviation with
it, so the weights do not move. Under the shift alone they do, so the same tree measured in
different units would hand FastGreedy a different graph.

When every branch has the same length there is no spread to divide by, and every weight is
one. The graph is then unweighted, which is the honest reading: the lengths carry no
ordering to express.

It also settles what a negative branch means. Neighbor Joining emits one where the matrix
contradicts the split it is making, and the reciprocal of a negative length sorts below every
positive one, so such a branch becomes the weakest edge rather than the strongest. That is
the reading this project adopts; nothing in the method requires it.

The result is at least one everywhere and is unchanged by rescaling every length -- the
second of those is a consequence of the division, not of the shift.

## A zero length fails rather than being repaired

A zero length has no reciprocal, and a length small enough for its reciprocal to overflow has
none that is usable. Both raise rather than being clamped, substituted or dropped.

The reason is the same one that motivates the division: any repair would introduce the
unbounded weight this function exists to avoid, and would do it silently, inside the step
that decides the clusters. `AGENTS.md` forbids a silent fallback or clamp for exactly this
class of case.

## Status

This mapping is this project's own and is versioned by this record. A change to it is a new
record, never a silent redefinition, because it changes which clusters a run reports from the
same tree.

No published work is cited as its source. [clauset2004] is cited above for what FastGreedy
maximizes, not for how the weights it is given are built.
