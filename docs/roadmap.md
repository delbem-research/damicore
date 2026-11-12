# Roadmap

DAMICORE 0.3 implements the canonical 2011 pipeline -- exact NCD over `zlib`/`gzip`,
deterministic Neighbor Joining, FastGreedy community detection -- with engineering the
published work on the method never had: content-addressed runs, a manifest that records input,
configuration and artifact hashes, resumable shards, preflight resource gates, decision
records, strict typing.

What it does not implement is what the literature added to the method after [sanches2011]. A
run returns `membership`, `clusters`, `tree_newick`, the distance matrix and a modularity
value: a partition with no meaning attached and no measure of how stable it is.

This page is the plan for closing that gap. The evidence it rests on -- which capability is
attested by how many published works, and the data behind each count -- is
[the evidence page](method/evidence.md). Live status lives on the issue tree it links to;
this page carries the structure and the rules, which do not change when an item closes.

## Rules that apply to every item

1. **Cite what you implement.** Every method, index, criterion or encoding added here comes
   from a specific published work. The implementing pull request records the work in
   `docs/refs.json`, cites the key on the `docs/method/` page for its stage, and names it in
   the docstring of the unit that computes it, with what this implementation does *not* take
   from that work. An implementation whose source cannot be named does not belong here.
   See ADR 0015 and ADR 0019.
2. **A result carries its own provenance.** When a change alters what is computed, the run
   manifest records the named, versioned rule that produced it -- the pattern ADR 0008 set for
   `object_encoding`, ADR 0014 for `partition_rule` and ADR 0018 for the `method` block. A run
   directory from a year ago must still say which variant of the method produced it.
3. **Deferred decisions need a record.** `AGENTS.md` names the decisions this project has
   deferred -- alternative compressors, approximate NCD, GPU, a remote service, DataFrame and
   stream input among them. An item that reopens one carries a decision record in its
   acceptance criteria, and that record states the measurement, not the intention.
4. **The quality-driver order settles trade-offs.** Correctness above bounded memory above
   reproducibility, down to portability ([quality drivers](quality-drivers.md)). Correctness
   items come first because they are correctness, not features.
5. **No silent sampling, ever.** Scale is bought with incremental computation, declared
   parallelism and measured limits -- never by quietly reducing the problem.

## How the tree is organised

One group per layer of the pipeline, matching the package boundaries of ADR 0001. Milestones
carry the sequence (0.4 to 1.0); groups carry the architecture. Every item states its own
scientific basis and its own documentation obligations.

The whole tree hangs from the epic,
[Roadmap: bring DAMICORE from the 2011 core to the published state of the art](https://github.com/delbem-research/damicore/issues/47).

### G1 — [#48](https://github.com/delbem-research/damicore/issues/48) Distance layer: compression distance beyond a single fixed compressor

| Item | Milestone | What it adds |
|---|---|---|
| [#59](https://github.com/delbem-research/damicore/issues/59) | 0.4 | refuse or flag objects larger than the compressor window |
| [#60](https://github.com/delbem-research/damicore/issues/60) | 0.4 | add bzip2 and LZMA compressors (standard library, no new dependency) |
| [#61](https://github.com/delbem-research/damicore/issues/61) | 0.5 | add PPMd (pyppmd) as the default compressor and a required dependency (ADR 0020) |
| [#62](https://github.com/delbem-research/damicore/issues/62) | 0.5 | interleaved pairing for objects larger than the compressor window |
| [#63](https://github.com/delbem-research/damicore/issues/63) | 0.5 | incremental NCD — extend an existing run with new objects |
| [#64](https://github.com/delbem-research/damicore/issues/64) | 0.9 | pluggable metric interface (Levenshtein, DTW, Euclidean) alongside NCD |
| [#65](https://github.com/delbem-research/damicore/issues/65) | 0.9 | combine several representations of the same object into one matrix |

### G2 — [#49](https://github.com/delbem-research/damicore/issues/49) Tree layer: from one deterministic tree to a defensible one

| Item | Milestone | What it adds |
|---|---|---|
| [#66](https://github.com/delbem-research/damicore/issues/66) | 0.6 | resampling and consensus trees (DAMICORE-2), with a congruence measure |
| [#67](https://github.com/delbem-research/damicore/issues/67) | 0.7 | alternative builders (UPGMA, average/complete linkage) as declared baselines |

### G3 — [#50](https://github.com/delbem-research/damicore/issues/50) Partition layer: how many groups, and by which criterion

| Item | Milestone | What it adds |
|---|---|---|
| [#68](https://github.com/delbem-research/damicore/issues/68) | 0.5 | expose the criterion that chooses the number of groups |
| [#69](https://github.com/delbem-research/damicore/issues/69) | 0.7 | Pareto-efficient group selection (PESA) instead of a heuristic cut |
| [#70](https://github.com/delbem-research/damicore/issues/70) | 0.8 | alternative community detection on the tree graph |

### G4 — [#51](https://github.com/delbem-research/damicore/issues/51) Interpretation layer: what the groups mean and whether they hold

| Item | Milestone | What it adds |
|---|---|---|
| [#71](https://github.com/delbem-research/damicore/issues/71) | 0.6 | attribute-range labels with a non-overlap guarantee |
| [#76](https://github.com/delbem-research/damicore/issues/76) | 0.6 | dendrogram and phylogram rendering with cluster and label coloring |
| [#72](https://github.com/delbem-research/damicore/issues/72) | 0.7 | decision-tree backend |
| [#74](https://github.com/delbem-research/damicore/issues/74) | 0.7 | internal and external cluster indices |
| [#75](https://github.com/delbem-research/damicore/issues/75) | 0.7 | stability by resampling — congruence index and jackknife |
| [#73](https://github.com/delbem-research/damicore/issues/73) | 0.8 | neural relevance backend (MRA/ALM) and labeling of internal tree nodes |

### G5 — [#52](https://github.com/delbem-research/damicore/issues/52) Input layer: how raw data becomes comparable bytes

| Item | Milestone | What it adds |
|---|---|---|
| [#77](https://github.com/delbem-research/damicore/issues/77) | 0.8 | discretization and symbolic encoding for numeric attributes |
| [#78](https://github.com/delbem-research/damicore/issues/78) | 0.8 | binary object source (strings, CFG, lifting) — absorb BinclustRE |

### G6 — [#53](https://github.com/delbem-research/damicore/issues/53) Scale, prediction and the benchmark that closes the loop

| Item | Milestone | What it adds |
|---|---|---|
| [#79](https://github.com/delbem-research/damicore/issues/79) | 0.9 | re-derive the resource limits from measurement, per compressor and stage |
| [#80](https://github.com/delbem-research/damicore/issues/80) | 1.0 | classify a new object against a finished run (quartet test / BCC) |
| [#81](https://github.com/delbem-research/damicore/issues/81) | 1.0 | cold-start sample selection from the phylogram (PhyIL) |
| [#82](https://github.com/delbem-research/damicore/issues/82) | 1.0 | canonical cases and an ablation harness (regression infrastructure) |

### G7 — [#54](https://github.com/delbem-research/damicore/issues/54) Scientific record: make docs/ carry the method, the references and the memory

| Item | Milestone | What it adds |
|---|---|---|
| [#55](https://github.com/delbem-research/damicore/issues/55) | 0.4 | create docs/references.bib and make citation an enforced convention |
| [#56](https://github.com/delbem-research/damicore/issues/56) | 0.4 | add docs/method/ — one page per pipeline stage, stating what it computes and citing it |
| [#57](https://github.com/delbem-research/damicore/issues/57) | 0.4 | the manifest records which method variant produced a run |
| [#58](https://github.com/delbem-research/damicore/issues/58) | 0.4 | publish the roadmap and the evidence behind it in the repository |
| [#98](https://github.com/delbem-research/damicore/issues/98) | 0.4 | give a basis that is a project decision somewhere to point |
| [#99](https://github.com/delbem-research/damicore/issues/99) | 0.4 | a ratchet, so an undeclared basis cannot regress |
| [#100](https://github.com/delbem-research/damicore/issues/100) | 0.4 | twelve internal types appear in public signatures and a reader cannot follow them |
| [#101](https://github.com/delbem-research/damicore/issues/101) | 0.4 | exercise Read the Docs or delete its configuration |
| [#92](https://github.com/delbem-research/damicore/issues/92) | — | the bibliography is fetched, never written (mechanism) |
| [#93](https://github.com/delbem-research/damicore/issues/93) | — | every critical module declares its basis and its delta |

## Not in scope here

Writing the methodological paper. The benchmark and ablation harness is in scope as regression
infrastructure; the study it enables is deferred until this tree is implemented.
