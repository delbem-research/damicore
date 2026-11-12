# Changelog

Each released version is one `## X.Y.Z` section. That heading format is a contract, not a
style: `.github/scripts/version_guard.py` refuses to tag a version whose section is missing,
and `release.yml` extracts the section verbatim as the GitHub Release body. A heading that
carries anything else, a date included, matches neither and fails the release.

## 0.4.0

### Added

- `docs/roadmap.md` states what this library does not implement yet, in the structure the
  issue tree carries: one group per pipeline layer, the sequence by milestone, and the five
  rules every item is held to. The plan lived only in GitHub issues, so a reader of the
  repository could see what the library does and not what it is missing, which is the half
  that decides whether it fits a problem.
- `docs/method/evidence.md` is where the roadmap's claims come from: an audit of 82 documents
  of published DAMICORE work read against the 0.3 source tree, with the method, the three
  limitations, the capability counts and the scale profile. The two most attested
  capabilities in the corpus -- automatic cluster labeling and cluster validation -- are the
  two the library does not have, and that is the sentence the whole roadmap rests on.
- `docs/method/evidence-corpus.csv` is the per-document data those counts are computed from:
  one row per document with its role, the object count it reports, and one column per
  capability. The table on the page is the file's column sums, so a reader disagrees with a
  number by recomputing it. The corpus is not cited by key and is not in `docs/refs.json`:
  that file records the works this implementation rests on, fetched and cited by the unit
  that implements each, which is a different set under a different rule (ADR 0015).
- `docs/internals/decisions.md` lists every decision record with what it decides in one line.
  It was a bare table of contents, and `AGENTS.md`'s precedence rule -- records rank above
  the implementation -- is not applicable by a reader who has to open sixteen files to find
  which record governs the thing in front of them.

- Every attribution to a published work declares whether anybody read that work against
  this code: `Read: YYYY-MM-DD, primary.`, the same with `secondary.`, or `Not read:
  <why not>.` with a reason that may not be empty.
  `test_every_attribution_declares_whether_its_work_was_read` requires exactly one such
  line per attribution and checks nothing about its content, which is the only thing a
  check can do here — ADR 0016 was a resolved key, a real work and a wrong sentence with
  every gate green. The mark hangs on the attribution rather than the module because
  `natural_breaks` names five works whose states now differ. A `[adr-NNNN]` basis carries
  none: the decision is in this repository and reading it is not a separate act. See
  ADR 0019.
- `test_every_bibliography_entry_is_cited` holds the direction `test_cited_keys_resolve`
  left open. An entry nothing cites asserts a lineage the code does not claim, which is the
  same defect inverted.
- `neighbor_joining` states its basis, and `UNDECLARED` is empty. It leaves the exemption
  list by declaring [saitou1987] and [studier1988], both now read against the code. The
  arithmetic was pinned either way — Neighbor Joining is exact on an additive matrix, so
  the five-taxon fixture fails on any change to the Q criterion, the split or the
  reduction — but equivalence is not attribution, and the reading is what says which paper
  states which formula.
- The reduction `0.5 * (D[i][k] + D[j][k] - D[i][j])` is [studier1988]'s, not
  [saitou1987]'s, and the module had described it as "the published average". It is not an
  average: [saitou1987]'s equation (5) averages the two rows of the joined pair, and
  [studier1988] states the implemented formula as its (3), flagging the difference in the
  paper — "[Formula (3) is not the one given by Saitou and Nei, but it is the correct one
  if D is treelike and i and j are neighbors.]". Its formula (1) is the Q criterion, which
  is the credit ADR 0017 had carried unchecked, and its (4a)/(4b) are the branch-length
  split in the row-sum form the code writes. Neither paper states a tie rule, which is the
  premise ADR 0017 rests on and is now checked rather than assumed. The condition that
  came with the formulas is recorded where it bites: the corrected proof holds for a
  treelike `D`, and nothing here establishes that an NCD matrix is one.
- Six attributions were read against the code on 2026-11-07 and now carry their delta
  sentence: [gronlund2017], [song2020] and [wang2011] in `natural_breaks`, [saitou1987] and
  [studier1988] in `neighbor_joining`, and [sanches2011] on `docs/method/index.md`, which
  states what this implementation takes from the DAMICORE paper and what it leaves out —
  the derived "DNA" representation, the consensus across two representations, and the
  hardware-merge purpose downstream of the clustering.
- `tests/architecture/test_citations.py` asserts that every module in `CRITICAL_MODULES`
  states the basis of what it computes, except those named in a declared exemption list.
  The list starts with the modules that do not yet declare, so the gate is green from the
  first commit; shrinking it is the work, and nothing mechanical stops a future change from
  adding to it, which is a reviewer's job. The check measures presence at module
  granularity, and a `References` section carrying a citation is the whole of it. Whether
  the work it names supports the claim beside it is checked by a reader; ADR 0015 records
  that no mechanism here attempts that.
- `serializer` states its basis and is the only module outside the exemption list. Its
  encoding is this project's own, so it cites ADR 0001 rather than a published work; it
  went first because that was the shortest path to a gate that is not vacuous, and
  `tree_graph` and `neighbor_joining` can follow on ADRs 0016 and 0017.
- ADR 0016 records how branch lengths become the edge weights FastGreedy is given, and ADR
  0017 records the Neighbor Joining tie rule and its relative band. Both decide what a run
  reports and neither has a published source, so each is now a basis a unit can cite as
  `[adr-NNNN]`. The reasoning was already in the two functions' docstrings; these records
  give it the authority rank and the citable name that a docstring cannot carry.
- `docs/refs.json` records igraph (2006) and Jenks (1977), the two works this project rests
  on for which neither resolver holds a record. ADR 0015 names them and is what replaces
  the resolver.
- `tree_graph` and `compressor` join `CRITICAL_MODULES`, which is both the 95% coverage floor
  and the repository's census of scientific code. `tree_graph` decides the graph FastGreedy
  partitions; `compressor` owns the window every NCD value is a function of.
- `docs/refs.json` records every published work this repository cites, and nothing with a
  DOI in it is typed: `make ref DOI=…` copies each field from a Crossref or OpenAlex
  response, derives
  the citation key from the fetched metadata, and regenerates `docs/references.bib`, which
  is generated and never edited. `tests/architecture/test_citations.py` asserts that every
  key cited from a docstring or a Markdown file resolves, that an entry with no DOI names an
  existing ADR, and that the committed bibliography matches its source. A pull request that
  changes an entry re-fetches exactly that entry in CI, which is what catches an entry
  written by hand. A unit whose basis is a decision rather than a published work cites
  `[adr-NNNN]`. See ADR 0015.
- `docs/refs.json` records the normalized-information-distance lineage NCD descends from:
  the information distance of Bennett et al. (1998) and its normalization by Li et al.
  (2004), cited on `docs/method/ncd.md` with what this implementation does not inherit from
  either. `pinto2017`, named in issue #55, is left out: two real works by that author match
  the fields the issue lists and neither carries a DOI, so naming it is the maintainer's.
- Every DOI `CITATION.cff` names is recorded in the bibliography. The file lists what this
  software builds on, and a work named there and absent from `docs/refs.json` meant citing
  the software and citing the method named different sets of works. The DOI is what is
  compared: it identifies the work, where a title can differ in punctuation and still be
  the same paper. `version_guard.py` already covered the version.
- `bibtexparser` joins the development group, and the bibliography is read back with it.
  `test_generated_bibliography_matches_source` compares the renderer with its own output,
  so a value that renders into something a reader takes differently is invisible to it; the
  new check reads `docs/references.bib` with a parser this project did not write and
  compares every field with `docs/refs.json`.

### Removed

- The `slow` pytest marker. It was registered and applied to no test, so the
  `minimum-versions` job's `pytest -m "not slow"` filtered nothing while reading as a fast
  subset. The job now runs `pytest`, which is what it was doing.
- `.readthedocs.yaml`. It was added beside the Pages workflow and never built once: no
  project existed on Read the Docs, so its Python version, its `fail_on_warning`, and its
  installation of the five distributions by `pip` -- a resolution path nothing in CI covers
  -- were all unverified. A configuration nobody runs claims a capability the project does
  not have. The argument for keeping it was a site per release, which this repository
  already has twice over: every page is ordinary Markdown, so GitHub renders any tag's
  documentation, and the API reference is generated from docstrings that ship in the wheel.
  GitHub Pages remains the published build.
- `docs/references.md`, generated from `docs/refs.json` beside `docs/references.bib` and
  published with the site. The bibliography existed, was fetched, was gated and reached no
  reader: nothing rendered it, so `[clauset2004]` and five other keys appeared on the
  published pages as plain text with nothing behind them. The `.bib` serves a reader writing
  a paper; this page serves one who met a key and wants the work. Both are held to the same
  source by `test_generated_bibliography_matches_source`, so neither can drift by hand.

- Three rules `AGENTS.md` states and nothing held now fail on their own.
  `tests/architecture/test_boundaries.py` reads every `np.load` and `np.save` under
  `packages/*/src` from the syntax tree and requires `allow_pickle=False`, so a call that
  omits the flag fails as loudly as one that sets it to `True` -- a `.npy` loaded with
  pickling enabled runs whatever the file says, which is what would turn a run directory
  into a code path. Ruff's `ERA` and `FIX` rules are selected in all seven configurations,
  so commented-out code and a `TODO` stop entering. A local `pygrep` hook holds the commit
  subject to Conventional Commits, and `default_install_hook_types` wires the `commit-msg`
  stage that `pre-commit install` would otherwise skip.

- ADR 0016 cited [clauset2004] for the quantity FastGreedy maximizes. `fastgreedy.py`
  calls `community_fastgreedy(weights="weight")`, so the quantity is modularity on a
  weighted graph, and that paper defines modularity with `A_vw` equal to 1 or 0 -- saying
  of it, "This assumes the graph is unweighted; weighted graphs are a simple
  generalization", and deferring that to a different work. `newman2004` is now recorded and
  cited for the weighted objective, leaving [clauset2004] cited for the greedy agglomerative
  procedure, which is what it does give. The key resolved and the work was real; it did not
  support the claim beside it, which is the failure ADR 0015 says no mechanism here can
  catch. It was found by reading the paper.

- `docs/method/`, one page per pipeline stage plus an index, stating what the stage
  computes, where the definition comes from, and what this implementation does not take
  from its source. Every formula on them was checked by running the function it describes
  and reproducing the numbers by hand, and the pages show those inputs and results. Where a
  page states what a published work says, it quotes it; where an attribution has not been
  read against the primary work, the page says so rather than implying it has.
  `test_every_method_page_names_a_basis` refuses a method page that cites nothing.

- `natural_breaks` states its basis. Its five works were already attributed in the
  References section of `docs/dataset-partitioning.md`, in prose the maintainer wrote
  saying what each one contributes; the module carries those forward and the page now cites
  by key instead of repeating author, year and title that `docs/refs.json` already holds.
  Three of the five were later read against the code and carry their delta sentence; the
  other two carry the mark that says they were not, and why.
- `newick` is infrastructure, not scientific code, and is classified as such. It
  serialises a tree that `tree.json` already holds authoritatively -- the clusterizer reads
  the JSON -- so it decides nothing a run reports, even though `DamicoreResult.tree_newick`
  is public. It leaves the census and the 95% floor with it; the global floor still covers
  it, and it is at 100% today.
- Nine of the thirteen census modules that stated no basis now state one, taking the
  exemption list from thirteen to four. `ncd` cites [cilibrasi2005] and says what it does
  not take from it: the paper proves its results for a compressor that is *normal*, and
  nothing here establishes that `zlib` -- a member of the family the paper calls "possibly
  not precisely symmetric" -- is. `fastgreedy` cites the procedure, the weighted objective
  and igraph separately, because they are three different works and only one of them runs.
  `tree_graph` and the six normalizer modules cite the records that decide what they
  compute. `ncd.py`, `fastgreedy.py`, `tree_graph.py` and the six had no module docstring
  at all; these are their first.
- `compressor`, `newick`, `natural_breaks` and `neighbor_joining` were exempt at that
  point, and the reason was the point of the number: the first two needed a decision record
  that did not exist, and the last two rested on published work nobody had read against the
  code. A list that said zero then would have been saying something false. All four are
  resolved later in this cycle — `newick` left the census, `compressor` and the rest state
  a basis, and the two Neighbor Joining works were read — so the list is empty now, which
  is a different claim from the one it would have been making then.

- `manifest.json` carries a `method` block naming the rule in force at each stage --
  `canonical-json-lines/v1`, `ncd-max/v1`, `neighbor-joining-q/v1`,
  `fastgreedy-modularity/v1` -- each with the citation keys that are its basis. A run
  directory now says which variant of the method produced it without the release that
  wrote it being available to ask, which is the point: every open roadmap issue turns one
  of those constants into a variant. Parameters stay in `config`, where `config_hash`
  already covers them; the rule names the method a choice was made inside.
  **The block is additive and the schema version did not change**, so no existing run
  directory was invalidated: a manifest written before it reads back with `method` absent,
  which states that the run does not say which rules produced it rather than guessing.
  Every key in the block is asserted to resolve, because this is the one place a citation
  is written in Python and the scanner that holds every other one does not read it. See
  ADR 0018.

- Every multi-valued string `Literal` a public model declares is classified: mapped to the
  one documentation page that must name each of its values, or declared not a choice with
  a reason the suite asserts is not empty. A field in neither fails by name. This is the
  gap the first feature would have walked into -- adding a compressor leaves
  `docs/method/ncd.md` saying "Not available: any other compressor" with every gate green,
  verified by adding one and watching nothing break. The models are found by walking every
  module of the five packages, not by reading each package's `__all__`: a model reaches a
  user through an artifact it serializes as much as through an export, and `RunConfig`,
  the config a caller fills, is not exported at all.
- A value counts as named only where it stands alone in an inline code span, or is the
  right-hand side of one (`` `split="columns"` ``). A word-boundary match had passed
  `delimited` on the sentence "always `,`-delimited UTF-8" and `xlsx` on the file
  extension `.xlsx`, so the one mapped field whose values are ordinary vocabulary was
  green on three accidents and documented nowhere; `.` and `,`, were they ever mapped,
  would have matched every page in the repository.
- `docs/input-contract.md` names `delimited`, `xlsx`, and `files` as the values a caller
  passes. The page described all three sources at length and never gave the spellings.
- `AGENTS.md` and `docs/internals/documentation.md` state that an attribution nobody has
  read against the code says so in its own entry. Nothing can check whether a work supports
  the sentence beside it, so that sentence is the only thing separating a checked citation
  from an assumed one -- and without it the two are indistinguishable, which is how ADR 0016
  carried a wrong attribution for weeks with every gate green.

### Fixed

- The distance stage ran on one worker whatever `workers` said whenever a run had at most
  10,000 pairs -- 141 objects or fewer -- because the default `pairs_per_shard=10000` made
  the whole run one shard, and one shard occupies one worker. A checkpoint then covered all
  of the run or none of it. Few, large objects is the shape of large-data runs, so this hit
  them hardest: 16 objects of 0.8 MB took 6.2 s on four workers against 5.5 s on one.
  `pairs_per_shard` now defaults to `"auto"` in both `DistanceConfig` and `ExecutionConfig`,
  resolved by `DistanceConfig.effective_pairs_per_shard` to at least 64 shards, or one per
  pair below that, and never more than 10,000 pairs per shard. It depends on the pair count
  alone, so it never changes a run's identity from one machine to another. The manifest
  and `report.json`, the failure report included, record the resolved size, and preflight
  projects checkpoint and memory cost from it. A default run therefore hashes to a new run directory, and a 0.3 run directory is
  recomputed rather than reused; an explicit integer behaves exactly as before.
- `AGENTS.md` described itself as the contract for "DAMICORE 0.2" and qualified four of its
  rules with that version. 0.3.0 shipped and 0.4.0 is in this changelog, so the file
  governed two releases it did not name. The qualifier is removed rather than bumped,
  because a version in prose goes stale and nothing checks it. That widens those four from
  release-scoped to unconditional -- the closed product scope, the ban on a shared
  `damicore-core`, the interpreter range for new code, and `argparse` for the CLI now bind
  without a release named beside them. Each already held; naming the widening is the point.
- `references.py` said nothing in `docs/refs.json` is ever typed. Two of its seventeen
  entries are: a work no resolver holds is written by hand once an ADR names it, which ADR
  0015 permits and `source: adr:NNNN` records. The docstring now says which kind is which.
- `make help` said `clean` removes build artifacts and the `.venv`. The target prints a
  sentence and deletes nothing, which is deliberate -- `AGENTS.md` forbids a recursive
  delete of a user directory -- so the help now describes that.

- Two of the six authors of `gronlund2017` were published with their given name as their
  surname: `Stefan, S.` for Schneider and `Mingzhou, S.` for Song. OpenAlex mixes two name
  forms in one response -- `Allan Gronlund` beside `Schneider, Stefan` -- and the rule that
  took the last whitespace-separated token read the second form backwards. A comma now
  decides the form. Nothing could see this: `docs/refs.json` and `docs/references.bib`
  agreed because both carried the transform's output, and `references.py verify` re-fetched
  through the same transform and reported a match. The four strings OpenAlex actually
  returned are now a test.
- Eight statements across the documentation said something the tree does not support, all
  found by the same audit and all of the same kind -- a rule stated more strongly than it is
  held. ADR 0015 opened by saying nothing in `docs/refs.json` is ever typed, two sections
  above the two entries it authorises to be written by hand, and closed a paragraph on
  unsourced claims with an unsourced rate; `docs/internals/documentation.md` told an editor
  never to write an entry at all; this changelog repeated the same overstatement. The
  `Makefile` comment said the citation test enforces a basis for every module of the census,
  where it enforces one for every module its exemption list does not name -- six of seven
  are still named. `docs/releasing.md` told a releaser to rename a `## Unreleased` heading
  that does not exist. `damicore-clusterizer`'s README said it clusters every node of the
  tree; the root is dropped and its two edges become one joining its children.
  `docs/dataset-partitioning.md` named the private `synthetic_data` package on a page the
  site publishes, which `AGENTS.md` forbids.
- ADR 0017 described a tie order the code does not use and credited the paper it had just
  said states none. `active` is sorted at the start of every Neighbor Joining round, so
  pairs are visited in lexicographic label order, not in the order the input supplied --
  measured, relabelling the same matrix changes the topology and permuting its rows does
  not. The record said the opposite. It also closed with "choosing the first in index order
  is also what [saitou1987] does", twelve lines after opening with the statement that
  neither [saitou1987] nor [studier1988] says what to do on a tie; that sentence is gone.
  The record now names the order the code uses and points at the test that holds it.
- ADR 0016 and `_edge_weights`' docstring described a computation the function does not
  perform. Both said the reciprocals are "translated so the smallest is exactly one" and
  omitted the division by their standard deviation, which is the step that sets the scale of
  every weight and therefore the partition FastGreedy returns. The record also claimed the
  result is unchanged by rescaling every length, which is true of the code and false of the
  procedure the record described: under the shift alone, doubling the lengths halves the
  weights. Both now state all three steps, what the division buys that the shift does not,
  and that every weight is one when the branches are equal.

- The citation in `README.md` named version 0.2.0, two releases behind, and nothing checked
  it. That file is the repository's landing page, beside the button that offers the same
  citation from `CITATION.cff`, so the two disagreed where a reader goes to cite this work.
  `version_guard.py` now holds it to the released version, to the release date
  `CITATION.cff` declares, and to the authors that file lists — it checks the surnames are
  present, not their order or initials, which is what catches an author added to one file
  and not the other. The guard's own comment claimed the citation file was the only copy no
  packaging tool would notice drifting; the README was a second one, and it is the one that
  had drifted.
- A field whose braces do not balance is refused instead of written. BibTeX delimits a
  value by matching braces, so an unbalanced one does not fail the parse: the reader stops
  early and hands back a truncated string. A title ending in `}` was written, parsed and
  read back shorter, and every check comparing our own files with each other agreed.
- A resolver's strings are HTML-escaped and are now decoded where the two resolvers meet.
  Crossref returned `IET Generation, Transmission &amp; Distribution`, and the entity was
  stored, rendered and published as part of the journal's name.
- `make docs-check` builds with `-E`. Sphinx reused a saved environment, skipped the pages it
  considered current, and did not re-run its consistency checks — so the gate passed or
  failed on whether `docs/_build` happened to be stale. A page in no toctree went undetected
  on a warm build.
- The bibliography renders a venue into the field its entry type actually carries: a thesis
  into `school`, a book into `publisher`, a report into `institution`. Everything that was
  not an article rendered as `booktitle`, which parses and says something false about the
  work.
- An unresolved `[adr-NNNN]` citation is told to write the decision record. It was told to
  run `make ref DOI=…`, which is the one place the answer for a project decision cannot be.

- Every module under `packages/*/src` is now classified: it is in `CRITICAL_MODULES`, the
  census of code that decides what a run reports, or in `NOT_SCIENTIFIC` beside the test
  that enforces the split. A module in neither fails the suite by name. Nothing asked that
  question before -- the census was read and never compared with the tree, so a scientific
  module nobody listed owed no basis and sat below no coverage floor with every gate green.
  Verified by adding one and watching the suite pass, then fail.
- `CRITICAL_MODULES` grows from seven modules to thirteen: `numeric_column`,
  `delimited_reader`, `spreadsheet_reader`, `table_split`, `file_corpus` and `partition`
  join it. Each decides what a run reports -- the bytes an object is made of, the rule that
  reads a number, the subsets a dataset is cut into, the format the tree is recorded in --
  and each was already above the 95% floor, so the census grew at no cost in test work. The
  exemption list grows with it, because a module entering the census without a basis has to
  be exempt or the gate is red on arrival; what must never fall is the number that declare.
- The census parser joins a `\` continuation the way Make does. The list no longer fits on
  one line, and scanning raw lines would have read seven of the fourteen and called it the
  whole census.

### Changed

- Every distribution's `Documentation` URL now points at the published site,
  `https://delbem-research.github.io/damicore/`, rather than at one Markdown file in the
  repository. The README links it too.
- Every GitHub URL names the current organisation, `delbem-research`. The published 0.3.0
  metadata still carries the former name in `Homepage`, `Repository`, `Issues` and
  `Changelog`, so those five PyPI pages link through whatever redirect GitHub keeps for a
  renamed organisation rather than directly.

- `run()` refuses a completed run in `output_dir` that a different runtime produced, instead
  of returning it. The input and configuration hashes say what was asked for, never which
  code answered, so a release that redefines a stage used to hand back the earlier release's
  result under its own name — and the cross-artifact verification still passed, because the
  artifacts were internally consistent, just not that build's. Resume has always been gated
  this way; reuse now shares the same gate. The error is `OutputDirectoryConflictError`,
  which already exits 5, so no exit code changed. Point the run at a fresh directory to
  recompute.

## 0.3.0

### Added

- `damicore_normalizer.partition_dataset`: split a dataset into high and low subset files by
  a numeric column, by equal-count quantiles, equal-count percentiles, or exact Fisher-Jenks
  natural breaks. Each emitted file is a pure subset of the input in input order, always
  `,`-delimited UTF-8, so it re-enters the pipeline with no further argument, and
  `partition.json` records the input digest, the column, the resolved decimal separator and
  each file's rank interval, size and SHA-256. The decimal separator is detected by
  falsification against every cell or declared with `decimal=`. `PartitionResult`,
  `PartitionManifest` and `PartitionFile` join the public surface. Rules are versioned as
  `partition_rule` v1; see ADR 0014 and `docs/dataset-partitioning.md`.

### Changed

- A run no longer writes `checkpoints/pipeline.json`. Stage receipts live in
  `manifest.json` under `stages`, which already carried them, so resume reads one file
  instead of two copies of the same state. A completed manifest no longer declares that
  artifact. Runs produced by 0.2.0 still load, and an incomplete one still resumes,
  because the receipts it resumes from were always in the manifest too.
- `DistanceResult` reports `ncd_min`, `ncd_max` and `ncd_out_of_range_count`. They are
  measured during the matrix validation pass the distance stage already performs, so the
  orchestrator no longer walks the matrix a second time to fill `report.json`. The
  reported values are unchanged.

### Fixed

- The public attributes of the published classes declare their types. `DamicoreError.code`
  and `.context`, the same two on each stage's error base, and `DistanceMatrixView.path`
  and `.labels` were left to inference. That resolves correctly in this workspace, but a
  consumer type-checks the wheel with their own checker, which may resolve an inferred
  attribute another way; `DamicoreError` is the base of every public error class, so the
  whole hierarchy was reported as partially unknown. Runtime behaviour is unchanged --
  the types were always these, and are now declared.

## 0.2.0

### Breaking

- The input contract is a source axis rather than one CSV. `run` and `estimate` take
  `source_kind` (`delimited`, `xlsx`, or `files`), and `split`, `delimiter`, `encoding`,
  `sheet`, `recursive` and `include_hidden` apply only to the sources that define them. A
  setting that does not apply raises `ConfigurationError` instead of being ignored.
- `CSVFormatError` is now `DatasetFormatError`, with code `dataset_format_error`; it covers
  spreadsheets as well as delimited text.
- The normalization manifest and the run manifest are schema version 2. Their `input` block
  is a union discriminated on `kind`, and the run configuration hash changed, so existing
  run directories are not reused. Completed 0.1 runs cannot be loaded by 0.2.
- `damicore_normalizer.normalize_csv` remains for delimited datasets, but
  `materialize_objects` is the entry point for every source. `NormalizationConfig` now
  carries a `source` (`DelimitedSource`, `SpreadsheetSource`, or `FileCorpusSource`)
  instead of top-level `split`, `delimiter` and `encoding`.
- `ResourceEstimate` reports `source_kind` and `source_paths` in place of `csv_path`, and
  `split` is `None` for a files source.

### Added

- A `files` object source: pass files, a directory, or both, and each file becomes one
  object with its bytes unchanged. Objects may be binary. Files are ordered by relative
  POSIX path, labelled by it so basenames may repeat, identified by a digest over the whole
  ordered set, and copied into the run directory so it stays self-contained. An empty file,
  a symlink, a non-regular entry, a duplicate path, and a corpus of fewer than two files
  are each refused with a stable code.
- An `xlsx` dataset source reading `.xlsx` and `.xlsm` through openpyxl's read-only row
  iterator, so the bounded-memory invariant holds. The used range is trimmed to the real
  data rectangle, a workbook with several worksheets requires the name, and formulas are
  stored as text and never evaluated.
- `cell_text_rule` v1, recorded in the manifest: the total, engine-independent rule that
  turns typed cells into text. A `.csv` and an `.xlsx` of the same logical table produce
  identical object bytes.
- `object_encoding` in the normalization manifest (`json-lines/1` or `raw-bytes/1`), so a
  distance is attributable to the bytes it measured.
- `NormalizationManifest` in `damicore_normalizer`'s public surface. It was already the
  documented contract between stages, so validating `manifest.json` no longer means reaching
  past the package's public API to do it.
- `openpyxl>=3.1,<4` as a runtime dependency of `damicore-normalizer`.
- ADRs 0006-0010 recording the source axis, run self-containment, the named object
  encoding, the spreadsheet engine, and the cell-text rule.

### Fixed

- Declare `numpy` in `damicore`, which imports it directly, and drop it from
  `damicore-clusterizer`, which never did. Installing `damicore-clusterizer` alone no
  longer pulls NumPy.
- Fix the stage examples on PyPI: the tree-builder and clusterizer examples now read the
  paths the previous stage actually writes, and the distance example carries the
  `if __name__ == "__main__":` guard its process pool requires.
- Correct the documented meaning of CLI exit status 4: it covers any failed stage, not
  only artifact validation.
- Document the public API. Every exported symbol now carries a docstring, and `run`,
  `estimate` and `load_result` document their parameters, returns and failure modes.

### Unchanged

- Delimited object bytes are identical to 0.1, verified differentially across quoted cells,
  embedded newlines and tabs, Unicode, blank lines, and both splits. Delimited text already
  accepted any single-character delimiter, so `.tsv` and `.txt` are documented rather than
  added.
- Legacy `.xls` stays out of scope and raises a typed error naming the conversion.

## 0.1.0

- Define the first public API for estimate, execution, loading, and results.
- Add canonical CSV normalization, exact resumable NCD, deterministic Neighbor
  Joining, and FastGreedy leaf clustering.
- Add versioned, hashed artifacts; resource gates; CLI; wheel and release gates.
