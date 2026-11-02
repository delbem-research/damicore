# ADR 0015: The bibliography is fetched, never written

`docs/refs.json` records every published work this repository cites, and nothing in it is
typed. The only input is a DOI; every field is copied from a resolver response.
`docs/references.bib` is generated from it. This records why the input is that narrow,
because the obvious alternative — writing entries by hand, as the methodology paper
[sanches2011] would be written in any other project — is the one that fails here.

## The maintainer is the threat model

An assistant asked to add a reference produces a plausible, confidently worded and entirely
false citation at no cost. The cost asymmetry is the whole problem: inventing a citation is
instant, and verifying one is slow or impossible. An entry that lands in the bibliography
then becomes the source of truth for every later session, which cites it without doubt, so
one fabricated entry is not a local defect but a permanent one.

Narrowing the input to a DOI inverts that asymmetry. A fabricated DOI either fails to
resolve or resolves to a visibly different work, and `make ref` prints the title it fetched
so the mismatch is seen at the moment it happens rather than discovered years later.

## Crossref alone is not enough, and the gap is not where it looks

Crossref answers first. It returns no author list at all for both Neighbor Joining records,
which are old Oxford deposits, and no year for the thesis deposits this project's own
corpus is largely made of. OpenAlex supplies both. The entry records which resolver
supplied its title, so a re-fetch starts where the entry came from.

A venue is deliberately not required. A thesis has none in either resolver, and a rule that
is red on the works this repository exists to cite is a rule someone switches off within a
month — which is how a citation gate stops being a gate.

## A base with no DOI is a decision, not an exception

Some works have no DOI at all, and some units implement no published work: the canonical
serialization is this project's own, and so is the tie-break the Neighbor Joining
implementation applies when two Q scores are algebraically equal. Both are bases, and both
are recorded here rather than left silent. An entry may therefore carry `source: "adr:NNNN"`
instead of a resolver, and a unit may cite `[adr-NNNN]` with no bibliography entry at all.
The contract is total: there is no fourth value and no empty one, so "we decided this
ourselves" is an answer rather than an absence.

An `adr:` entry is the one path where metadata is written rather than fetched, and that is
not an exemption from the rule but the limit of it: the rule exists to stop a work that
*could* have been fetched from being typed instead, and there is nothing to fetch here. What
replaces the resolver is this record. A work only enters the bibliography this way by being
named below, and `tests/architecture/test_citations.py` refuses an entry whose ADR does not
exist.

### Works recorded here because no DOI exists for them

- **Csárdi, G., & Nepusz, T. (2006). The igraph software package for complex network
  research. *InterJournal, Complex Systems*, 1695.** Key `csardi2006`. `fastgreedy.py` does
  not implement greedy modularity maximization; it calls igraph. Documenting that stage
  therefore has to cite the implementation that runs alongside the method it implements, and
  InterJournal registered no DOI for this article. Checked on 2026-11-02: Crossref returns
  no record of it, and OpenAlex holds the work with `doi: null`, so there is nothing for
  `make ref` to resolve.
- **Jenks, G. F. (1977). *Optimal data classification for choropleth maps*. Department of
  Geography, University of Kansas.** Key `jenks1977`. The cartographic formulation of the
  grouping objective `natural_breaks` computes, already cited in prose by
  `docs/dataset-partitioning.md`. Checked on 2026-11-02: neither Crossref nor OpenAlex holds
  a record of it at all, which is what a departmental occasional paper from 1977 looks like
  to both.

## The generated file is committed

`docs/references.bib` is derived, and a derived file in version control can drift. It is
committed anyway, because #55 requires a bibliography that a standard BibTeX tool parses and
a reader can browse, and the drift is closed by regenerating it offline in the test suite
and comparing — the same arrangement `ruff format --check` already has in this repository. A
hand edit becomes a failing check rather than a silent divergence.

## What this does not do

A resolved entry proves the work exists. It never proves the work supports the claim beside
it, and no mechanism here attempts that: quotation errors run at around one in six in
fields with peer review. What an implementation does not take from its source is stated in
the unit, in prose, and is checked by a reader. Nothing in this ADR should be described as
verification.
