# ADR 0017: The Neighbor Joining tie rule is this project's, with a relative band

Neighbor Joining joins the pair with the smallest Q score. When two pairs tie, something has
to choose, and neither [saitou1987] nor the Q formulation in [studier1988] says what. This
records what `damicore_tree_builder` does, because the choice changes the tree a run reports
from the same matrix, so recording "Saitou and Nei 1987" alone does not identify that tree.

## Ties are not an edge case here

With four clusters left, `Q(i,j)` and `Q(k,l)` for complementary pairs are algebraically
equal for *every* matrix. A tie is therefore reached on the way to the end of essentially
every run, not occasionally.

Exact float64 comparison does not see it. Measured, it detects that equality about 60% of the
time, and no summation strategy fixes it: the cancellation happens in the final subtraction
rather than in the sums. An exact comparison also asserts a precision the input never had —
every distance here is a ratio of integer compressed sizes, so its meaningful digits run out
long before the sixteenth.

## The rule

Two scores within a relative band are one tie. The band is
`1e-9 * max(1.0, abs(best_score))`, so it scales with the magnitude of the better score
rather than being absolute.

Among pairs in a tie, the one reached first wins: a later pair replaces the best only when
it is better by more than the band, and a better score inside the band tightens the band's
centre without changing the pair.

What "first" means is the part worth recording, because it is not the input. The active set
is sorted at the start of every round, so pairs are visited in **lexicographic label
order**: the running pair is always the lexicographically earliest one seen so far.
Relabelling can therefore change which pair wins a tie; reordering the rows of the same
matrix cannot. Internal nodes are named `nj_%06d`, zero-padded so that their lexicographic
order is also the order they were created in. The tree is a function of the matrix and its
labels and of nothing else, which
`damicore_tree_builder`'s `test_an_exact_q_tie_is_broken_by_the_smallest_id_pair` holds by
running the same tie through three permutations of the input and requiring the same join.

The band and this ordering are both this project's. The opening of this record says
[saitou1987] states no tie rule, and nothing here walks that back: what is claimed is that
resolving a tie by a fixed, label-derived order is a choice the method leaves open, not a
departure from it.

## What the band buys, and what it costs

It buys determinism across platforms. Without it the tree depends on whether a particular
float64 subtraction happened to cancel exactly on the machine that ran it, which makes a
result irreproducible for a reason that has nothing to do with the data.

It costs a threshold with a magnitude, and a threshold is a decision: two genuinely distinct
pairs closer than one part in a billion are treated as tied. Against distances that are
ratios of integer compressed sizes, that is far below the resolution of the input.

## Status

The band and the index-order preference are this project's and are versioned by this record.
A change to either is a new record, because it changes which tree a run reports.

`validate_matrix` rests on the same decision: it is the gate that makes the rule meaningful,
by refusing a matrix whose shape, dtype or labels would let the pair loop run on input the
rule was never defined over.
