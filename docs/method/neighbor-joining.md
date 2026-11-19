# The tree: deterministic Neighbor Joining

## What this stage computes

Over a square float64 distance matrix with `r` active clusters, each round:

```
row_sum(i) = sum of D[i][j] over active j != i
Q(i, j)    = (r - 2) * D[i][j] - row_sum(i) - row_sum(j)
```

The pair minimising `Q` is joined. Its branch lengths:

```
delta  = (row_sum(i) - row_sum(j)) / (r - 2)
len(i) = 0.5 * (D[i][j] + delta)
len(j) = D[i][j] - len(i)
```

and the remaining distances are reduced, for every other active `k`:

```
D[new][k] = 0.5 * (D[i][k] + D[j][k] - D[i][j])
```

written into both triangles. `row_sum` is recomputed from the matrix every round rather than
carried forward, so no drift accumulates across iterations.

The loop runs while more than two clusters remain, so exactly `n - 2` joins for `n` labels.
The final two are attached to a synthetic node `nj_root` with equal lengths `D[L][R] / 2`.
Internal nodes are named `nj_%06d` from 1, zero-padded so their lexicographic order is their
creation order.

Checked against the implementation on a five-label matrix, re-deriving every join
independently from the formulas above: joins, order, node names and every branch length
agree exactly. Round one by hand, with `r = 5`: `row_sum(a) = 31`, `row_sum(b) = 34`,
`Q(a,b) = 3*5 - 31 - 34 = -50`; `delta = (31-34)/3 = -1`, `len(a) = 0.5*(5-1) = 2`,
`len(b) = 5 - 2 = 3` — which is what the code produces.

**Complexity.** `n - 2` rounds; per round the row sums and the `Q` scan are each `O(r^2)`
and the reduction is `O(r)`, so `O(n^3)` time. The matrix is copied once at entry and slots
are reused rather than the array being shrunk, so `O(n^2)` memory.

## Determinism, and the choice it rests on

Neighbor Joining joins the pair with the smallest `Q`. With four clusters left, `Q(i,j)` and
`Q(k,l)` for complementary pairs are algebraically equal for *every* matrix, so a tie is
reached on the way to the end of essentially every run — and float64 detects that equality
only about 60% of the time.

Two scores within a relative band are therefore treated as one tie, the band being
`1e-9 * max(1.0, abs(best_score))`. Among tied pairs the one reached first wins, and the
active set is sorted at the start of every round, so pairs are visited in **lexicographic
label order**. Relabelling the same matrix can change which pair wins a tie; permuting its
rows cannot. [adr-0017] records the rule, the band and what each buys, and
`test_an_exact_q_tie_is_broken_by_the_smallest_id_pair` holds the permutation half of it.

## What the method comes from

The method is [saitou1987]: pairs are chosen so as to "minimize the total branch length at
each stage of clustering of OTUs starting with a starlike tree". The `Q` criterion in the
form implemented here — the `(r - 2) * D[i][j] - row_sum(i) - row_sum(j)` above — is the
reformulation of [studier1988], its formula (1).

Both works were read against this code on 2026-11-07, and the reading moved one of the
three formulas above from one paper to the other. The reduction
`D[new][k] = 0.5 * (D[i][k] + D[j][k] - D[i][j])` is not [saitou1987]'s. That paper's
equation (5) averages the two rows of the joined pair,
`D((1-2),j) = (D(1,j) + D(2,j)) / 2`, and [studier1988] states the formula used here as its
(3), with the difference marked in the paper itself: "[Formula (3) is not the one given by
Saitou and Nei, but it is the correct one if D is treelike and i and j are neighbors.]"
The branch-length split is in both: [saitou1987]'s equation (6a) writes it with the mean
distance from each joined OTU to the rest, [studier1988]'s (4a) and (4b) write the same
quantity with row sums, which is the form implemented — `len(i)` is (4a) and `len(j)` is
(4b) as the complement.

What the two papers do not settle is the tie, and what the tests do not settle is the
attribution. Neighbor Joining is exact on an additive matrix, so the five-taxon fixture in
`test_additive_matrix_yields_the_expected_topology_and_branch_lengths` and the independent
path-sum check beside it fail on any change to the `Q` criterion, to the branch-length
split or to the reduction — but equivalence is not attribution, and which work states
which formula is the reading recorded above, not something those tests can hold.

One condition travels with the formulas and is not met here. [studier1988] gives the
corrected proof that the true tree is recovered *if `D` is treelike*; an NCD matrix is not
known to be, and nothing in this implementation establishes or checks it. What the pipeline
produces is the tree the formulas yield for the matrix it was given.

## What this implementation does not do

- **It does not clamp a negative branch length.** Neighbor Joining emits one where the
  matrix contradicts the split being made. The length is written as computed and the count
  is reported on the result; [adr-0016] records that such a branch becomes the weakest edge
  downstream rather than the strongest.
- **It produces one tree, not a distribution.** No resampling, no consensus, no support
  values.
- **The root is synthetic.** `nj_root` is a degree-2 node at the midpoint of the last
  remaining distance, not an inferred root. The clustering stage drops it again.

## Status in this implementation

| | |
|---|---|
| Available | exact Neighbor Joining with the Q reformulation, deterministic under a relative tie band |
| Not available | UPGMA or linkage baselines; resampled or consensus trees; congruence or support measures |
| Guarded | a non-square matrix, a side that disagrees with the labels, fewer than two labels, duplicate labels, and a dtype other than float64, each raising `TreeBuilderError` with code `distance_matrix_validation_error` |
| Serialisation | Newick, with every identifier quoted, written by `newick.py` |
