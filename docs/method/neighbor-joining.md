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

The method is [saitou1987]. The `Q` criterion in the form implemented here — the
`(r - 2) * D[i][j] - row_sum(i) - row_sum(j)` above — is the reformulation of [studier1988].

**This section is not yet checked against the primary sources.** The attribution is carried
forward from [adr-0017], which records that neither work states what to do when two pairs
tie. What this implementation takes from each, and what it does not, is owed and tracked by
issue #93: a resolved citation proves the work exists, never that it supports the sentence
beside it.

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
