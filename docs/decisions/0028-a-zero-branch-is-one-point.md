# ADR 0028: A zero branch is one point, and rounding residue around zero is zero

A run of 76 dataset columns under PPMd computed all 2,850 distances and then stopped at the
cluster stage with `tree_format_error: Branch length is zero and has no reciprocal weight`.
Several of the columns held the same value in every row. PPMd codes such a column and its
concatenation with an identical one into the same number of bytes, so their distance is
exactly zero, Neighbor Joining gives the two leaves branches of zero, and [adr-0016] refused
a zero length because the reciprocal it weighs branches by does not exist there.

That refusal was a rule about a weight, applied to a fact about the data: two objects that no
distance separates. This record replaces it, and closes the float64 gap that made the same
fact arrive as a residue of either sign.

## Zero is real, and it arrives two ways

1. **Objects no distance separates.** Identical bytes under a compressor that cannot grow
   their concatenation give a distance of zero, and the two leaves get branches of zero.
2. **A star.** Three or more objects with one distance profile are a star, which a binary
   tree can only draw as two joins with a branch between them. That branch is zero in exact
   arithmetic for every such matrix, and float64 leaves it as a residue: measured on two
   five-object matrices, `-1.39e-17` on one and `+2.78e-17` on the other.

The residue decided the result. At `0.0` the run was refused; below zero the branch became the
weakest edge; above zero its reciprocal, near `4e16`, inflated the standard deviation that
[adr-0016] divides by, and every other weight in the tree collapsed toward one. Measured on 75
texts in five languages with three duplicated, one branch of `2.2e-16` left 99% of the weights
within 0.05 of one, and the adjusted Rand index against the languages fell from 0.541 to 0.504.

## The rules

**Neighbor Joining writes a branch inside the band around zero as exactly zero.** The band is
`1e-9 * max|D|`, where `max|D|` is the largest entry of the input matrix before any join:
[adr-0017]'s relative tolerance, for [adr-0017]'s reason -- every distance is a ratio of
integer compressed sizes and has no sixteenth digit. It has no floor, unlike [adr-0017]'s tie band, because a float64 residue
scales with the distances it was computed from, so the same matrix in any unit has the same
branches at zero; with a floor of one, a matrix measured in units of `1e-10` had every branch
inside it. The zero is positive, so `tree.json` and `tree.nwk` never print a signed residue.
A branch outside the band is left exactly as the formulas compute it, so the two branches of
a join sum to their distance except where one of them was written as zero.

**The cluster stage joins the two nodes of a zero branch into one vertex.** A zero branch
says the nodes are one point, which is neither a weak link nor a strong one but no link: there
is nothing between them to weigh. FastGreedy partitions the points, and every object takes
the community of its point, so objects at one point share a cluster and every object still
appears in `membership.csv`. The rest of [adr-0016] applies unchanged to the branches that
remain. Joining along the edges of a tree never closes a cycle, so the graph stays a tree.

Two consequences are fixed with it:

- A tree whose branches are all zero is one point: one cluster, modularity 0, which is the
  modularity of a single community by definition. igraph would report NaN, which no JSON
  artifact can carry.
- `num_clusters` above the number of distinct points is a `configuration_error` naming that
  number, because a dendrogram over fewer points cannot be cut into more communities.

## What was rejected

- **Weight equal to length**, the legacy implementation's mapping. It completes on a zero, as
  an edge of weight zero, but it reads the shortest branch as the weakest link, the reverse of
  what a distance means. It also stops on any negative branch, because igraph refuses negative
  weights. On the five-language corpus it reached an adjusted Rand index of 0.06 to 0.10 with
  48 to 84 clusters, where [adr-0016] reaches 0.38 to 0.54 with 12 to 19.
- **A small epsilon in place of zero.** It completes, and then flattens every other weight
  toward one for the reason above, so one pair of duplicates decides the whole partition.
- **Refusing duplicate objects before the distance stage.** It fails earlier and more
  clearly, but it still refuses data that is valid. It does not cover objects that differ and
  still measure zero, nor stars of equal profile without equal bytes.
- **A different weighting** (median-based spread, or rank). On the same corpus one reached
  0.520 where [adr-0016] reaches 0.389 on 150 texts. It changes every run's result, the
  evidence is one domain, and it still has no answer for a zero, so it is a separate trade
  for a separate record.
- **Computing distances only for distinct objects.** It saves the pairs of exact copies and
  nothing else. It needs these rules anyway, and its cost is a manifest field and an
  expansion step, so it waits for a measured need.

## What changes, and what does not

The manifest records `neighbor-joining-q/v2` and `fastgreedy-modularity/v2`, each citing this
record. A tree with no branch inside the band, and its clusters, are bit-for-bit what `v1`
produced, because neither rule touches them. A tree with such a branch changes: from a refusal
to a result, or from a partition the residue decided to one that does not depend on it. The
distance rule does not change.

The band is a threshold with a magnitude, and the assumption behind it is that no real branch
is that short. The shortest real branch measured on the five-language corpus was `1.3e-4`,
against a residue of `2.2e-16`, with a largest distance near one. A branch's resolution is
roughly the inverse of a compressed size times the number of objects, relative to the largest
distance. A corpus large enough on both counts to bring that near `1e-9` is the condition to
revise this record.

## Status

Replaces the section "A zero length fails rather than being repaired" of [adr-0016]. A nonzero
length whose reciprocal overflows is still refused. Applies [adr-0017]'s relative tolerance to
branch lengths, scaled to the matrix rather than floored at one; [adr-0017]'s own tie band is
unchanged. A change to either rule is a new record, because each
changes the clusters a run reports.
