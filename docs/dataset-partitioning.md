# Specification: partition a dataset into high and low subsets by a numeric column

Status: approved by the maintainer on 2026-09-09. The decision is recorded in
[ADR 0014](decisions/0014-dataset-partitioning.md); this document is the
specification the implementation is checked against. Where the two could be
read differently, the ADR states the decision and this document states the
contract.

## Summary

Add one public function to `damicore_normalizer` that takes a dataset, a
numeric target column, and one or more partition methods, and writes a set of
subset datasets: for each requested partition, the file holding the rows with
the highest values and the file holding the rows with the lowest values. Each
emitted file is a complete, valid DAMICORE dataset that re-enters the existing
pipeline unchanged through the delimited source, so the best and worst
segments of a population can be clustered separately and compared.

Three partition methods are in scope: equal-count quantiles, equal-count
percentiles, and Fisher-Jenks natural breaks. The first two are one rule with
two spellings; the third is value-based and exact.

## Motivation

A research workflow that DAMICORE serves is: rank a population by a fitness or
performance score, take the top and bottom segments, cluster each by its
remaining attributes, and compare what characterizes each segment. The
normalizer today turns every row into an object or a contribution to every
column object. There is no way to select rows, so the researcher must prepare
the subsets by hand, which loses the provenance the rest of the pipeline is
built to keep: which file the subset came from, which column and rule chose
the rows, and which bytes were written.

## Scope and non-goals

In scope:

- one public function in `damicore_normalizer`, with a validated result model
  and a versioned manifest schema, exported through the package's `__all__`;
- delimited text and `.xlsx`/`.xlsm` inputs, read through the package's
  existing readers under the existing input contract;
- three partition methods, defined exactly below;
- documentation, tests, and changelog, updated in the implementing change.

Out of scope, deliberately:

- a fourth object source. The emitted files are datasets, not objects. Every
  partition overlaps another (the top quarter is inside the top half), so the
  set of files is not a set of objects for one distance matrix.
- any change to `materialize_objects`, `scan_source`, `NormalizationConfig`,
  or `manifest.json`. The 0.2 schemas are untouched.
- exposure through `damicore.run`, `damicore.estimate`, or the CLI. That is a
  separate decision with its own signature consequences under ADR 0013.
- value-interpolated percentiles. `numpy.percentile` offers nine interpolation
  variants; "the highest p percent of rows" is the rank-based reading, it is
  exact, and it is the one this document defines.
- thousands separators, locale-dependent parsing, resume of an interrupted
  partition, and dropping or transforming any column.

## Definitions

### Input

A dataset D is one delimited-text file or one worksheet, satisfying the
existing input contract in `docs/input-contract.md`: a header H of non-empty
unique names and n data rows r_1..r_n in file order, every row with exactly
len(H) fields. Header validation here is the shared rule for names, non-empty
and unique, with no minimum column count; whether an emitted file has enough
columns or rows for a given `split` is the downstream pipeline's rule and is
not anticipated here.

The target column c is named by its header name, which the contract already
makes a unique key. Spreadsheet cells cross to text through `cell_text_rule`
v1 before any rule below applies, so a `.csv` and an `.xlsx` of the same
table partition identically.

Rows are numbered from 1 in file order, the first data row being row 1. Every
message that points at a row uses this number and calls it a data row, so a
quoted field spanning physical lines cannot make the number ambiguous.

### Numeric grammar

There are two grammars, one per separator, written out as literal patterns
rather than derived from a placeholder, and applied with `re.fullmatch` under
`re.ASCII`:

```
[ \t]*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]{1,6})?[ \t]*   for "."
[ \t]*[+-]?([0-9]+(,[0-9]*)?|,[0-9]+)([eE][+-]?[0-9]{1,6})?[ \t]*     for ","
```

`fullmatch` rather than `^...$`, because `$` also matches before a trailing
newline and a quoted cell holding `1\n` would pass. The value of a matching
cell is `decimal.Decimal` of the text with the matched spaces and tabs
removed and `,` replaced by `.` when that is the separator. The pattern is
the gate and `Decimal` only converts what passed it, because `Decimal` on its
own accepts more than this contract does: Unicode digits, underscores, `NaN`,
`Infinity`, and exponents up to its own internal limit, beyond which it
raises. The grammar makes three properties structural, so no later check can
be forgotten:

- finiteness: `NaN`, `Infinity`, an empty cell, and any letter other than the
  exponent marker do not match;
- determinism: digits and whitespace are ASCII, whitespace is space and tab
  only, so a cell renders the same number on every platform and locale and a
  line break inside a cell is not a number;
- representability: the exponent has at most six digits, so every match is a
  `Decimal` the constructor accepts. Without the bound, `1e1000000000000000000`
  matches a pattern with an unbounded exponent and `Decimal` raises
  `InvalidOperation`, an exception that is not this package's and that the
  failure contract below could not keep.

Six exponent digits reach `1e999999` and `1e-999999`, far beyond any number a
spreadsheet, a `float64`, or a scientific export can hold; within that, the
grammar places no bound on magnitude or precision. Every such decimal is
accepted as written, and the rank-based methods compare `Decimal` values
directly, which is exact at any precision and costs nothing extra for a value
such as `1e999`. The one method that needs integers, `jenks`, states its own
bound below in terms of what is actually infeasible.

No `Decimal` arithmetic is ever performed. Construction from text,
comparison, and `as_tuple()` are the only operations used, because they are
exact regardless of the thread's decimal context, while `normalize()`,
`quantize()`, `+`, and `*` round to the context precision of 28 significant
digits and would silently approximate a value with more. Where integers are
needed, they are derived from `as_tuple()` in integer arithmetic.

Values are compared numerically, never textually: `1`, `1.0`, `01`, and
`1e0` are one value. "Equal values" and "distinct values" throughout this
document mean numerically equal and numerically distinct.

There is no thousands separator. A cell such as `1,234` under S = `,` is the
number 1.234, because no data can distinguish it from a decimal; `1.234,56`
matches neither grammar and is refused.

### Decimal separator resolution

`decimal` is declared or resolved. When declared, it must be `.` or `,`, the
grammar for it is tested against every cell of c, and the first cell that
fails it is refused by data row number. When not declared, both hypotheses
are tested against every cell in the same streaming pass, and the outcome is
decided by falsification:

- exactly one hypothesis survives every cell: it is the separator;
- both survive: no cell contains either character, both readings yield the
  same values, and the separator resolves to `.` for determinism;
- neither survives: the column is refused with the data row number of the
  first cell that falsified each hypothesis and an instruction to declare
  `decimal` or fix the data.

There is no locale, no sampling, and no majority vote. A hypothesis survives
only if every cell satisfies it. A typed numeric spreadsheet cell always
renders with `.` under `cell_text_rule` v1, so a worksheet column mixing
typed numbers with text cells written with `,` falsifies both hypotheses and
is refused, which is the correct answer for a mixed column.

### Partition rule v1

`partition_rule` v1 is the one versioned name for everything the emitted
files depend on. One name rather than several, because a reader reproducing
a partition needs to know one thing: which ruleset produced it. It covers:

1. the numeric grammar above and the separator resolution;
2. the total order on rows by (value descending, file position ascending),
   so equal values are ordered by their position in the input, and
   `rank[i]` in 0..n-1, the position of row r_i in that order, rank 0 being
   the highest value;
3. the three method definitions below, boundaries and tie-breaks included;
4. the output form: `,`-delimited UTF-8 with LF, spreadsheet cells crossing
   through `cell_text_rule` v1.

Any change to any of these is `partition_rule` v2, never a silent
redefinition under the same name. Methods carry no version of their own.

### Partitions

A partition is a pair (method, parameter). It produces k+1 boundary ranks
`bounds[0..k]` with `bounds[0] = 0`, `bounds[k] = n`, and non-decreasing
values, dividing the ranks into k contiguous classes `[bounds[j],
bounds[j+1])`. Every partition emits exactly two files: `high`, the class
`[bounds[0], bounds[1])`, and `low`, the class `[bounds[k-1], bounds[k])`.
The first and last classes are never empty; whether a middle class may be
empty is stated per method.

All boundary arithmetic is exact integer arithmetic.

**`quantile`**, parameter q, an integer with q >= 2: k = q and
`bounds[j] = floor(j * n / q)`. Class sizes differ by at most one and every
class is non-empty when n >= q, since floor((j + 1) n / q) - floor(j n / q)
>= floor(n / q) >= 1; `high` has floor(n / q) rows and `low` has ceil(n / q).
Precondition: n >= q.

**`percentile`**, parameter p, an integer with 1 <= p <= 50: k = 3 and
`bounds = [0, floor(p * n / 100), floor((100 - p) * n / 100), n]`. The two
inner bounds coincide whenever the floors meet, always at p = 50 and for
small n at many smaller p (p = 34, n = 3 gives `[0, 1, 1, 3]`), leaving the
middle class empty; that is the one method whose middle class may be empty,
and it is why `bounds` is non-decreasing rather than strictly increasing.
What p <= 50 guarantees is that `high` and `low` are disjoint; p = 50 is the
same cut as q = 2, and p = 25 the same as q = 4. Two spellings may produce
identical files under different names, which is accepted and explicit.
Precondition: floor(p * n / 100) >= 1.

**`jenks`**, parameter k, an integer with k >= 2: Fisher-Jenks natural
breaks, the k-class partition minimizing the sum over classes of the squared
deviations of each row's value from its class mean (SDCM), equivalently the
exact one-dimensional k-means optimum.

*Domain.* It is defined and computed over the d distinct values u_1 > u_2 >
... > u_d, in rank order, with weights w_j equal to the number of rows holding
u_j, not over the n rows. This is not a restriction of the optimum but a
property of it: moving one of two equal values from the class whose mean is
farther to the class whose mean is nearer strictly lowers the SDCM unless
both means already equal that value, which with d >= k never happens at an
optimum that uses every class. So the weighted optimum over distinct values
attains the unconstrained optimum over rows, and a class boundary never falls
between two equal values. A class boundary after distinct value u_j is the
rank `w_1 + ... + w_j`; that is how a boundary in value space becomes a
boundary in `bounds`.

*Arithmetic.* Exact. Every nonzero value is written as `c * 10^e` with c an
integer not divisible by 10, derived from `as_tuple()` by stripping trailing
zero digits in integer space, never by `normalize()`; e_min is the smallest e
in the column; each value is scaled to the integer `c * 10^(e - e_min)`, and
zero is the integer 0. The integers are as narrow as the data allows and
their width is the column's magnitude span, not any value's absolute size.
The span is computed arithmetically first, as `digits(c) + e - e_min`
maximized over the column, and the precondition below is checked on that
number before any scaled integer is built, so a column such as
`{1e-999999, 1e999999}` is refused without ever attempting a two-million-digit
integer. Class costs, each `sum(w * v^2) - sum(w * v)^2 / sum(w)`, are
compared as exact rationals. Scaling and the span check are pure functions in
the numeric-column module, so the refusal is testable without any I/O.

*Tie-break.* When several partitions attain the minimum SDCM, the one chosen
has the smallest `bounds[k-1]`; among those, the smallest `bounds[k-2]`; and
so on down to `bounds[1]`. This is a declarative rule about the result, and
it is what a layer-by-layer dynamic program that keeps the leftmost argmin
produces, so an implementation is checked against the rule, not the other
way round. A class may hold a single row; an isolated outlier forming its own
class is a result, not an error.

*Preconditions.* d >= k, and the magnitude span of the column fits in 1000
decimal digits, that is, the widest scaled integer has at most 1000 digits.
The bound is what exact arithmetic can afford, not a statement about the
data: a column holding both the smallest positive `float64`, the subnormal
`4.9406564584124654e-324`, and the largest, `1.7976931348623157e308`, spans
649 digits, so any column that a spreadsheet or a scientific export can
produce is inside it, and a column outside it is refused by naming its span
and the bound rather than approximated. References are listed at the end of
this document.

## Outputs

`output_dir` must be absent or an empty directory. On success it holds one
delimited file per (partition, end) plus `partition.json`:

```
quantile_02_high.csv    quantile_02_low.csv
quantile_04_high.csv    quantile_04_low.csv
percentile_05_high.csv  percentile_05_low.csv
jenks_04_high.csv       jenks_04_low.csv
partition.json
```

File names are `{method}_{parameter:02d}_{high|low}.csv`, with the method
spelled out so a reader of the directory knows what chose the rows without
opening the manifest. Parameters are zero-padded to at least two digits so
the directory lists in order; a parameter of three or more digits is written
as is. Names are fixed by method and parameter and never derived from data.

### Emitted file contents

Each file is a pure subset of the input: the header H as its first record,
then exactly the rows of its class, in input order, with every cell
unchanged and every column kept, including the target column. Sorting is the
selection rule, not a transformation of the output; the emitted file preserves
the input's order between rows, so a `split="columns"` run over a subset
measures the same column bytes the full dataset would have contributed for
those rows.

Format: always the library's default dataset form, whatever the input was.
The bytes of each file are the UTF-8 encoding, with no byte order mark, of
what `csv.writer` renders with `delimiter=","`, `quotechar='"'`,
`doublequote=True`, `escapechar=None`, `quoting=csv.QUOTE_MINIMAL`, and
`lineterminator="\n"`. One output form means an emitted file feeds
`damicore.run` with no delimiter or encoding argument, and the choice loses
nothing the pipeline measures: `json-lines/1` objects are UTF-8 JSON of cell
text regardless of the source encoding, so re-encoding and re-delimiting
change container bytes and never object bytes. The only transformation any
cell undergoes is the one the pipeline would apply anyway: a spreadsheet cell
crosses to text through `cell_text_rule` v1, and a delimited cell is not
changed at all. The invariant that makes this checkable is stated under
"Invariants and evidence".

### `partition.json`

A versioned, strict Pydantic schema written last and atomically. Its presence
is the completion marker: a directory without it is an incomplete partition.
Boundaries and counts are ranks and integers, never cell values, so the
manifest carries no dataset content, like every other artifact. JSON is
written as the package's other manifest is: UTF-8, two-space indent, sorted
keys, `allow_nan=False`, LF terminated.

```json
{
  "schema_version": 1,
  "partition_rule": "v1",
  "input": {
    "kind": "delimited",
    "path": "/abs/dataset.csv",
    "sha256": "<64 hex>",
    "size_bytes": 12345,
    "delimiter": ",",
    "encoding": "utf-8"
  },
  "column": "score",
  "decimal": ".",
  "row_count": 1000,
  "files": [
    {
      "method": "quantile",
      "parameter": 2,
      "end": "high",
      "rank_start": 0,
      "rank_end": 500,
      "size_bytes": 6000,
      "sha256": "<64 hex>"
    }
  ]
}
```

Models, with the field order the package tests pin by equality. All are
frozen, `extra="forbid"`, `strict=True`:

- `PartitionManifest`: `schema_version` (`Literal[1]`), `partition_rule`
  (`Literal["v1"]`), `input`, `column` (`str`), `decimal` (`Literal[".",
  ","]`), `row_count` (`int >= 2`, since every method needs at least two
  rows), `files` (`tuple[PartitionFile, ...]`).
- `input` is a union discriminated on `kind`. `DelimitedPartitionInput`:
  `kind` (`Literal["delimited"]`), `path`, `sha256` (64 hex), `size_bytes`
  (`>= 0`), `delimiter` (one character), `encoding`.
  `SpreadsheetPartitionInput`: `kind` (`Literal["xlsx"]`), `path`, `sha256`,
  `size_bytes`, `sheet` (the resolved worksheet name), `cell_text_rule`
  (`Literal["v1"]`). `path` is
  the resolved absolute input path as a string. Neither model is exported;
  the normalization manifest's input variants are not either.
- `PartitionFile`: `method` (`Literal["quantile", "percentile", "jenks"]`),
  `parameter` (`int`), `end` (`Literal["high", "low"]`), `rank_start`
  (`>= 0`), `rank_end`, `size_bytes` (`>= 0`), `sha256` (64 hex). Exported,
  as `ObjectDescriptor` is, so a typed consumer of `manifest.files` sees a
  public type. Two derived values are properties, not fields:
  `relative_path`, equal to `f"{method}_{parameter:02d}_{end}.csv"`, and
  `row_count`, equal to `rank_end - rank_start`. The file name is fixed by the
  naming rule and persisting it would be a second copy of that rule.

Validators, so a corrupted or hand-edited manifest is refused rather than
trusted. On `PartitionFile`: the parameter range is the method's, q >= 2,
1 <= p <= 50, k >= 2; and `rank_start < rank_end`. On `PartitionManifest`:

- every file has `rank_end <= row_count`;
- a `high` file has `rank_start == 0`; a `low` file has
  `rank_end == row_count`;
- files come in pairs: every (method, parameter) present has exactly one
  `high` and one `low` entry;
- `files` is ordered by method in the order quantile, percentile, jenks; then
  by `parameter` ascending; then `high` before `low`.

The formula of each rank-based method is not re-checked by the model. It has
one owner, the pure function that computes bounds from (method, parameter,
n), which the partition pass calls and the tests check against the
definitions above; a validator restating the formula would be a second copy.

The input blocks deliberately do not reuse `DelimitedDatasetInput` or
`SpreadsheetDatasetInput` from the normalization manifest: those carry
`split`, which does not apply here, and the input contract says a setting
that does not apply is rejected, never carried.

Every field answers a question nothing else in the file answers. `input`,
`column`, `decimal`, and `partition_rule` reproduce the partition; `decimal`
is the resolved separator whether declared or detected, so a detected run is
reproduced by declaring the recorded value. `row_count` is n. Each file entry
carries the triple that identifies it and names its file, the rank interval
that says which rows it holds, and the size and digest that let it be
verified.

Fields considered and left out, each because it is derivable or has no
consumer yet and can be added compatibly later, whereas removing a field from
a strict schema needs a breaking release: the file name (fixed by the naming
rule), the per-file row count (a difference of two fields), the full boundary
list of each partition (the middle classes are recomputable by re-running,
which is deterministic), the goodness of variance fit of a Jenks partition,
the number of distinct values, whether the separator was declared or
detected, and the output form, which `partition_rule` v1 fixes.

The link between a partition and a later clustering run is the file digest:
`damicore.run` records the SHA-256 of its input file, which equals the
`sha256` of the corresponding entry in `files`. No coupling between the two
manifests is needed.

## Public API

Added to `damicore_normalizer` and to its `__all__`, in this order after the
existing entries: `partition_dataset`, `PartitionResult`, `PartitionManifest`,
`PartitionFile`.

```python
def partition_dataset(
    source: str | Path,
    output_dir: str | Path,
    *,
    column: str,
    quantiles: Sequence[int] = (2, 4, 8, 16, 32),
    percentiles: Sequence[int] = (5, 10, 20),
    jenks_classes: Sequence[int] = (2, 3, 4, 5),
    decimal: str | None = None,
    source_kind: str = "delimited",
    delimiter: str = ",",
    encoding: str = "utf-8",
    sheet: str | None = None,
    chunk_rows: int = 50_000,
) -> PartitionResult: ...
```

Argument contract:

- `source` is one path, string or `Path`; a sequence is rejected. It is
  resolved and must be an existing regular file.
- `column` is matched against header names exactly, as text, after the
  reader's own header handling (a UTF-8 byte order mark is not part of the
  first name, as today).
- `quantiles`, `percentiles`, and `jenks_classes` each take a `list` or
  `tuple` whose elements are `int`; a set, a generator, a NumPy array, a
  NumPy integer, or a `bool` is rejected, `bool` although it subclasses
  `int`. The function sorts each sequence ascending, so the caller's order is
  irrelevant; a repeated value is rejected; the ranges are q >= 2,
  1 <= p <= 50, k >= 2. An empty sequence disables that method. At least one
  method must be requested.
- `decimal` is `None`, `.`, or `,`.
- `source_kind` is `delimited` or `xlsx`. `delimiter` and `encoding` apply
  only to `delimited`, `sheet` only to `xlsx`, and a setting that does not
  apply is rejected when it differs from its default, exactly as
  `damicore.run` rejects one. The two applicability sets are derived from the
  field names of `DelimitedSource` and `SpreadsheetSource` minus `kind` and
  `split`, as the aggregate derives its own, so a field added to a source
  model cannot go unthreaded silently. `delimiter` is one Unicode character
  and `encoding` a codec Python knows, validated as `DelimitedSource`
  validates them today. `sheet` is `None` only for a single-sheet workbook,
  as the spreadsheet reader already requires.
- `chunk_rows` is an `int >= 1`. Its default is the same number as
  `NormalizationConfig.chunk_rows`, and a test asserts the two are equal so
  they cannot drift.

The signature is flat, following ADR 0013, because `split` does not apply
here and reusing `DelimitedSource` or `SpreadsheetSource` would mean accepting
and ignoring a field. Internally the flat arguments are turned into a
`DelimitedSource` or `SpreadsheetSource` with its default `split`, because
that is what the reader functions take; the seam functions read only
`delimiter` and `encoding`, or only `sheet`, and never `split`, which is why
the internal default is harmless where a public one would not be.

`PartitionResult` is a frozen strict Pydantic model with two fields, in this
order: `manifest_path` (`Path`) and `manifest` (`PartitionManifest`, the
validated manifest that was written). Nothing is restated beside the
manifest, so the result cannot disagree with the artifact. `PartitionManifest`
is exported for the same reason `NormalizationManifest` is: it is the
documented contract of the artifact.

There is no `max_open_files`: the number of output files is twice the number
of partitions the caller asked for, so all of them stay open for the duration
of the routing pass and the operating system's descriptor limit is the only
bound, reached only by a caller asking for hundreds of partitions.

### Failures

Every failure is a `NormalizerError` with one of the package's existing
codes. No new code is introduced. No message carries a cell value, a whole
row, or a value threshold; messages name positions, counts, names, and paths.
Where a table row says a message names something, that is part of the
contract and is asserted by the tests.

| Condition | Code |
|---|---|
| `source` is a sequence, or not one existing regular file | `input_validation_error` |
| `quantiles`, `percentiles`, and `jenks_classes` all empty | `input_validation_error` |
| a parameter sequence not a `list` or `tuple`, an element not an `int`, out of range, or repeated within its method | `input_validation_error` |
| `decimal` given as anything other than `None`, `.` or `,` | `input_validation_error` |
| `source_kind` not `delimited` or `xlsx` | `input_validation_error` |
| a non-default setting that does not apply to `source_kind` (message names it) | `input_validation_error` |
| `delimiter` not one character, `encoding` unknown, or `chunk_rows < 1` | `input_validation_error` |
| `output_dir` exists and is not an empty directory | `output_conflict_error` |
| header invalid under the input contract, or a workbook or worksheet problem the readers already refuse | `dataset_format_error` |
| `column` not in the header (message lists the available names; the same code an unknown `sheet` already uses, because the name is checked against the data) | `dataset_format_error` |
| a cell of `column` fails the declared grammar (message names the data row); this includes an empty cell and an exponent of seven or more digits, each with its own fixture | `dataset_format_error` |
| neither separator survives detection (message names one data row per hypothesis) | `dataset_format_error` |
| n < q, or floor(p * n / 100) < 1, or d < k (message names both numbers and the argument that disables the method) | `dataset_format_error` |
| `jenks` requested and the column's magnitude span exceeds 1000 digits (message names span and bound) | `dataset_format_error` |
| input size or mtime changed between passes | `input_drift` |
| an emitted file does not match its recorded size or digest | `artifact_validation_error` |

Every data-dependent refusal happens before any file is created, and the
argument-level refusals happen before the input is opened. Operating-system
errors while writing propagate as they do elsewhere in the package.

## Execution design

Three sequential passes over the input, each with one role, and no in-memory
structure beyond two vectors of length n.

| Pass | Reads | Does | Memory | Can fail |
|---|---|---|---|---|
| A: validate and resolve | the target column only | tests the grammar under each live hypothesis, resolves the separator, counts n | O(1) | yes: grammar, detection, n-based preconditions |
| B: rank | the target column only | builds the key vector, sorts once, derives `rank` and d; runs Jenks if requested | O(n) + O(K·d) | only the two Jenks preconditions, d < k and the span bound; parsing cannot fail after A |
| C: route | the whole table, streaming | appends each row to every file whose rank interval contains `rank[i]` | O(open files) | only on I/O |

The order of operations is fixed:

1. validate arguments; resolve `source`; check `output_dir` is absent or an
   empty directory;
2. record the input fingerprint (size and `st_mtime_ns`) before anything is
   read, then compute the SHA-256 in one streaming read;
3. read the header through the reader, validate names, locate `column`; for
   a delimited file, validate every record width once, here, as
   `scan_delimited` does before it iterates;
4. pass A, then re-check the fingerprint; refuse on the n-based
   preconditions;
5. pass B, then re-check the fingerprint; refuse on the Jenks preconditions;
6. create `output_dir` (refusing with `output_conflict_error` if it has
   appeared non-empty meanwhile), open all output files, write the header to
   each, run pass C, close the files, re-check the fingerprint;
7. re-read every emitted file and compare size and SHA-256 with what was
   recorded while writing;
8. build and validate `PartitionManifest`, write it atomically, return.

A refusal at steps 1 to 5 leaves no directory behind. A failure at steps 6 to
8 leaves a directory without `partition.json`, which is the unambiguous
incomplete state.

Passes A and B read only the target column, by column index: `usecols` with
the column's position for pandas, whose per-chunk header check then compares
against the selected names rather than the whole header; one cell per row for
openpyxl. The three passes are logical, and the seam iterators validate
nothing structural: the record-width pass runs once at step 3, and the
spreadsheet reader bounds the used range once before its first iteration.
Counting the digest, a delimited partition therefore reads the file six times
sequentially: digest, header, widths, A, B, C. A spreadsheet opens the
workbook five times: bounds, header, A, B, C. Every read is streaming and
none holds the table.

Sorting is `sorted(range(n), key=values.__getitem__, reverse=True)`. Python's
sort is stable with `reverse=True`, so the tie order by file position costs
nothing. `rank` is the inverse permutation in an `array('q')`, 8 bytes per
row, and the key vector is released once `rank` exists. Decimal keys and the
sort peaked at 168 MB per million rows when measured; correctness outranks
bounded memory in `docs/quality-drivers.md`, and this n is bounded by what
the downstream pipeline can cluster. An exact optimization (sort by
`float64`, a monotone coarsening of the Decimal order, and break only
equal-float groups with Decimal) is recorded in the ADR as a revision
condition to be triggered by measurement, not adopted now.

Jenks in pass B derives the (distinct value, weight) sequence from the sorted
keys in one sweep, scales to integers through the numeric-column module's
pure functions, which is where the span bound refuses, and runs the Fisher
dynamic program
with the monotone-argmin divide-and-conquer optimization, in
O(K · d · log d) exact rational operations and O(K · d) memory, where K is
the largest requested class count. The optimization is exact because the
SDCM cost satisfies the quadrangle inequality, under which the leftmost
argmin of each layer is non-decreasing in the prefix length; keeping the
leftmost argmin is also what realizes the declarative tie-break. One run to
K yields the optimum for every requested k <= K. A throwaway probe of this
exact algorithm, checked against brute force on 200 random fixtures, measured
with K = 5 in pure Python:

| distinct values d | time |
|---:|---:|
| 1 000 | 0.07 s |
| 10 000 | 1.0 s |
| 100 000 | 13.5 s |

Sorting one million `Decimal` keys peaked at 168 MB in the same probe. The
implementation was then measured end to end with the default methods, on a
three-column CSV, without instrumentation that slows the interpreter:

| rows | distinct values | time | peak RSS delta |
|---:|---:|---:|---:|
| 100 000 | 1 000 | 1.7 s | 36 MB |
| 100 000 | 100 000 | 11.8 s | 31 MB |
| 1 000 000 | 1 000 | 22.5 s | 127 MB |

Both costs are accepted for this release, as the ADR records; no resource
gate is added.

In pass C, membership is arithmetic on `rank[i]` against the 2·P intervals of
the P partitions. All 2·P output files are opened once and held for the pass;
each file's SHA-256 is updated as it is written.

Safety: output names are fixed and never derived from data; the column name
is only compared, never placed in a path; nothing is evaluated; no message or
artifact carries cell contents.

## Invariants and evidence

The primary oracle is metamorphic and exact. For every emitted file F with
rank interval [a, b): running `materialize_objects` over F with
`split="rows"` produces row objects whose bytes equal, one for one and in
order, the row objects that `materialize_objects` over D with `split="rows"`
produces for the rows r_i with a <= rank[i] < b, in input order. This proves
that the emitted subsets contain exactly the selected rows with unchanged
values regardless of quoting differences, for delimited and spreadsheet
inputs alike. Fixtures for it include cells with the delimiter, the quote
character, embedded LF and CR, leading and trailing spaces, an empty cell in
a non-target column, and non-ASCII text, and they give every emitted file at
least two rows, since `split="rows"` refuses a one-row dataset.

Structural invariants, asserted by equality:

- for each partition, `bounds` is non-decreasing from 0 to n with non-empty
  first and last classes, every `quantile` class is non-empty, and the
  class sizes of `quantile` and `percentile` follow their formulas,
  including p = 50 coinciding with q = 2 for even and odd n;
- classes of the same method nest: `high(2q)` is a subset of `high(q)`, and
  `high(2)` together with `low(2)` is D;
- `high` and `low` of one partition are disjoint;
- two runs, and runs with different `chunk_rows`, produce byte-identical
  files and manifests;
- a detected separator and the same value declared produce byte-identical
  files and manifests;
- `files` in the manifest follows the stated order, and every manifest
  validator refuses a manifest edited to violate it.

Jenks, on the chain definition -> representation -> invariant -> oracle:

- exact independent oracle: brute-force enumeration of all C(n-1, k-1)
  contiguous partitions of the sorted rows, unconstrained by ties, for
  n <= 12, choosing among equal minima by the declarative tie-break rule
  implemented independently of any dynamic program, and comparing both the
  SDCM and the chosen partition. Enumerating rows rather than distinct values
  is what checks the claim that the weighted optimum attains the
  unconstrained one. Fixtures include symmetric data with several optimal
  partitions, so the tie-break is exercised, not assumed;
- differential oracle for the monotone-argmin optimization: a plain
  O(K · d²) dynamic program over the same exact costs and the same leftmost
  argmin, on randomized fixtures with d in the hundreds, must return the
  same bounds. It is a different algorithm for the same definition, not a
  second copy of the optimized one;
- equal values never separate; SDCM is non-increasing in k; on fixtures with
  a unique optimum, the partition is invariant under an increasing affine
  transform of the values and mirrored under a decreasing one; a dataset
  with repeated rows yields the same classes as its distinct-weighted form;
- boundaries: d = k; d < k refused; an isolated outlier as a one-row class;
  all values equal with k = 2 refused; a column spanning `1e-300` to `1e300`
  completes with integers narrower than the bound; a column spanning 1001
  digits is refused naming the span, while the same column partitions by
  quantile without complaint; a column of values near `1e999` scales to
  small integers because the minimum exponent, not zero, sets the scale;
  `{1e-999999, 1e999999}` is refused by the span check without building an
  integer; `1`, `1.0` and `1e0` in one column count as one distinct value; a
  column whose values differ only past the 28th significant digit is
  partitioned by those digits, which fails if any `Decimal` arithmetic crept
  in.

Boundary and failure cases for the rest: n not divisible by q; ties straddling
a rank cut resolved by file position; n = max(q) and n = max(q) - 1;
floor(p * n / 100) = 1 and 0; every decimal outcome (integer-only column
resolves `.`, comma column resolves `,`, mixed column refused naming two data
rows, `1.234,56` refused, wrong declared separator refused at the first data
row, `decimal=";"` rejected); an empty target cell refused naming its row; a
seven-digit exponent refused naming its row; a cell holding `1` followed by a
line break refused; scientific notation; space- and tab-padded cells;
a spreadsheet with typed numeric cells and one with text cells; a column name
absent; each argument-level rejection in the table; drift between passes; a
non-empty output directory; a corrupted emitted file; a failed manifest write
leaving no temporary file; a refusal before pass C leaving no directory.

Tests are named `test_<behavior_under_condition>`, use minimal fixtures
constructed in the test, and use `synthetic_data` only where a size or scale
property is the claim.

## Repository consequences

- **Topology.** The package already separates schemas (`manifest.py`), the
  imperative shell (`api.py`: path resolution, fingerprint, digest, drift
  check, atomic write) and what is done with rows (`table_split.py`). The
  partition follows that shape rather than adding a parallel one. Two new
  pure modules and one new row-consumer module, each with one reason to
  change and a local oracle: `numeric_column.py` holds the two grammars, the
  separator resolution, key parsing, and the scaling to integers with the
  span check, all pure; `natural_breaks.py` holds Fisher-Jenks over weighted
  distinct integers, returning boundary indices for every k up to K, pure and
  standard-library only; `partition.py` holds the three passes over the
  reader seam and the pure bounds function of the rank-based methods.
  `PartitionManifest`, `PartitionFile`, and the two input models live in
  `manifest.py` beside the normalization schemas. `partition_dataset` lives
  in `api.py` beside `materialize_objects`, so it uses the shell's existing
  private helpers for resolution, fingerprint, digest, and atomic write
  without importing another module's underscore names and without a second
  copy. The generic-bucket test in `tests/architecture/test_boundaries.py`
  applies to the names.
- **The reader seam.** Both readers already deliver text rows to one
  consumer, `split_table`. The partition is a second consumer of the same
  rows, so each reader's private row iterator becomes a package-internal
  function that yields text rows and takes an optional selection of column
  indices; it validates nothing structural itself, exactly as today, and the
  partition never parses a file. The delimited iterator keeps reading through
  the module's own `pd` binding, which existing tests monkeypatch. The
  header-name rule is factored out of `validate_table_shape` into a function
  the partition calls directly, since the split-specific minimum does not
  apply; `validate_table_shape` calls the same function, so the rule keeps
  one owner. `NormalizerError` and the shell helpers are reused within the
  package; ADR 0012 concerns copies across packages and is not touched.
- **Public surface.** `partition_dataset`, `PartitionResult`,
  `PartitionManifest`, and `PartitionFile` join `__all__`;
  `tests/architecture/test_boundaries.py` pins the list by equality and is
  updated with it. The field lists of `PartitionManifest`, `PartitionFile`,
  and both input models are pinned by equality in the package's tests, as the
  aggregate pins its published schemas, because a field added or renamed is
  a contract change. No aggregate, CLI, or `estimate` exposure. No new
  runtime dependency: pandas and openpyxl already read the inputs, and
  `decimal`, `fractions`, `array`, `re`, and `csv` are standard library; the
  exact-dependency test stays green unchanged.
- **Gates.** The package's 90 percent branch-coverage gate in
  `packages/package.mk` applies to the new modules; Ruff and Pyright strict
  apply as to every module. `natural_breaks` joins `CRITICAL_MODULES` in the
  root `Makefile`, the 95 percent floor that `neighbor_joining` and
  `fastgreedy` already carry, because it is the same class of module: an
  exact algorithm whose wrong answer looks like a right one.
- **Versioning.** Additive: a minor release in lockstep. `CHANGELOG.md`
  gains an `### Added` entry under `Unreleased`.
- **Documentation updated in the implementing change.** `AGENTS.md` product
  map (the normalizer owns dataset partitioning, upstream of the source axis,
  emitting datasets rather than objects), the package README,
  `docs/input-contract.md` (numeric grammar and separator resolution), and
  `docs/artifacts.md` (`partition.json`). ADR 0014 already exists.

## Resolved decisions

Three questions were open in earlier drafts. They are settled above and in
the ADR, and repeated here so the issue shows them at a glance.

1. **Defaults.** `quantiles=(2, 4, 8, 16, 32)`, `percentiles=(5, 10, 20)`,
   `jenks_classes=(2, 3, 4, 5)`, all enabled. The percentile set is chosen
   so every n that satisfies the quantile default satisfies it too. The
   default call therefore needs n >= 32 and d >= 5; a column that cannot
   meet a default is refused with the disabling argument named.
2. **File naming.** `{method}_{parameter:02d}_{high|low}.csv`, the method
   spelled out, so the directory reads without the manifest.
3. **Numeric bounds.** The grammar bounds form and the exponent width, six
   digits, which is what keeps every match constructible by `Decimal`;
   within that, every decimal is accepted as written and the rank methods
   have no magnitude limit. Only `jenks` bounds the column's magnitude span,
   at 1000 digits, because exact arithmetic must be finite; the scale is set
   by the column's minimum exponent so absolute size never matters, and any
   `float64` data is inside the bound.

## Acceptance criteria

- [ ] `partition_dataset` exists in `damicore_normalizer` with the signature
      and argument contract above; `__all__` and `test_boundaries.py` agree.
- [ ] All three methods produce the boundaries defined here; the Jenks
      brute-force oracle over unconstrained row partitions, with its own
      implementation of the tie-break and fixtures that have several optima,
      passes for n <= 12; the differential oracle against the plain dynamic
      program passes at d in the hundreds.
- [ ] The readers expose one package-internal row iterator each, consumed
      by both `split_table` and the partition; no parsing exists outside the
      readers; the header-name rule has one owner called by both
      `validate_table_shape` and the partition.
- [ ] `partition_dataset` lives in `api.py`, the models in `manifest.py`,
      and no module imports another module's underscore names.
- [ ] The field lists of `PartitionManifest`, `PartitionFile`, and both input
      models are pinned by equality in the package tests, and every model
      validator listed above is exercised by a manifest edited to violate it.
- [ ] No `Decimal` operation other than construction, comparison, and
      `as_tuple()` appears in the package, and the 28-digit fixture passes.
- [ ] `natural_breaks` is in `CRITICAL_MODULES` and meets the 95 percent
      floor.
- [ ] The metamorphic row-object oracle passes for delimited and spreadsheet
      inputs on the fixtures named above.
- [ ] Every failure in the table raises `NormalizerError` with the stated
      code and names what the table says it names; data-dependent refusals
      leave no directory behind; no message contains a cell value.
- [ ] `partition.json` validates against `PartitionManifest`, is written
      atomically and last, and records exactly the fields in the example; the
      model declares them in the stated order, and the JSON on disk carries
      them with sorted keys like every other artifact.
- [ ] Every emitted file is `,`-delimited UTF-8 with LF and no byte order
      mark, for delimited and spreadsheet inputs alike, and is accepted by
      `damicore.run` with no delimiter or encoding argument.
- [ ] The default call succeeds on a column with n = 32 rows and d = 5
      distinct values, and is refused with an actionable message on d = 4.
- [ ] Two runs, and runs differing only in `chunk_rows`, are byte-identical.
- [ ] Peak memory in pass B is O(n) keys plus O(K · d) for Jenks; nothing
      holds the whole table. Jenks time at representative d is measured and
      reported in the pull request.
- [ ] `make -C packages/damicore_normalizer check` and `test`, then `make
      check` and `make test` from the root, pass with coverage thresholds
      met.
- [ ] `AGENTS.md`, the package README, `docs/input-contract.md`,
      `docs/artifacts.md`, and `CHANGELOG.md` are updated in the same change.

## References

- Fisher, W. D. (1958). On grouping for maximum homogeneity. *Journal of the
  American Statistical Association*, 53(284), 789–798. The exact
  dynamic-programming grouping that `jenks` computes.
- Jenks, G. F. (1977). *Optimal data classification for choropleth maps*.
  Department of Geography, University of Kansas. The natural-breaks
  formulation of the same objective.
- Wang, H., & Song, M. (2011). Ckmeans.1d.dp: optimal k-means clustering in
  one dimension by dynamic programming. *The R Journal*, 3(2), 29–33.
- Grønlund, A., Larsen, K. G., Mathiasen, A., Nielsen, J. S., Schneider, S.,
  & Song, M. (2017). Fast exact k-means, k-medians and Bregman divergence
  clustering in 1D. arXiv:1701.07204. Establishes the Monge property of the
  one-dimensional sum-of-squares cost that makes the monotone-argmin
  divide-and-conquer exact.
- Song, M., & Zhong, H. (2020). Efficient weighted univariate clustering maps
  outliers and other patterns. *Bioinformatics*, 36(20), 5027–5036. The
  weighted extension used to compute over distinct values with counts.
