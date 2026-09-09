# ADR 0014: Dataset partitioning is upstream of the source axis and emits datasets

`damicore_normalizer` gains one public function that splits a dataset into
high and low subsets by a numeric column. This records where that capability
lives, what its artifact is, and the rules that fix its results, because the
product map in `AGENTS.md` is closed and adding a domain to it needs a
decision, not a pull request. The specification the implementation is checked
against is `docs/dataset-partitioning.md`; this ADR records only
what would be reopened by accident without it.

## Placement

The capability lives in `damicore_normalizer`, upstream of the object source
axis of ADR 0006, and emits datasets, not objects. Three alternatives were
weighed. A fourth object source fails on semantics: the emitted subsets
overlap by construction, the top quarter being inside the top half, so they
cannot be objects of one distance matrix. A script under `scripts/` has no
public contract, no error class, no manifest, and would re-implement the
readers. A separate distribution meets the packaging economics ADR 0012
measured and adds a sixth lockstep version for one function. The normalizer
already owns reading both dataset formats, the input contract, the error
class, and the atomic writer, so the capability reuses every one of them and
adds none. Each emitted file re-enters the pipeline through the existing
delimited source; the 0.2 manifests and schemas are unchanged.

## The artifact is user-facing, not inter-stage

No stage consumes `partition.json`; the researcher does. It therefore carries
its own schema and input models rather than reusing the normalization
manifest's, whose `split` field does not apply here and could not be carried
without being ignored, which the input contract forbids. The link to a later
run is the emitted file's SHA-256, which the run manifest already records, so
no cross-manifest field exists to drift. The schema persists only what
reproduces or verifies the partition. Every derived or diagnostic field was
left out, because a field in a strict schema is cheap to add and expensive
to remove, as ADR 0011 measured.

## One seam, two consumers

The readers' validated text rows are the seam between what a file says and
what is done with it. `split_table` and the partition are its two consumers;
neither parses a file, and a third consumer would meet the same seam. Each
reader's row iterator therefore becomes a package-internal function rather
than a private detail of one caller.

## The rules are one versioned name

Everything an emitted file depends on is `partition_rule` v1: the numeric
grammar, separator resolution by falsification over the whole column against
the closed hypothesis space `{".", ","}`, the total order by value descending
then file position, the three method definitions with their tie-breaks, and
the one output form. Any change is v2 under a new name, never a silent
redefinition. Methods carry no version of their own.

Quantile and percentile are two spellings of one rank-based rule on exact
fractions. Jenks is value-based, computed over weighted distinct values with
exact rational arithmetic and a declarative tie-break, so equal values never
separate and the result is identical on every platform. The grammar bounds
form and the exponent width `Decimal` can construct, six digits, so that the
pattern is a total gate and no library exception can escape the package's
failure contract; within that, every decimal is accepted as written, no
`Decimal` arithmetic is ever performed because it rounds to context
precision, and only Jenks carries a magnitude bound, stated as the span exact
arithmetic can afford.

Emitted files are always the library's default dataset form, `,` and UTF-8
with LF, whatever the input was. The pipeline measures cell text through
UTF-8 JSON, so container encoding and delimiter never reach object bytes.

## Defaults

Quantiles `(2, 4, 8, 16, 32)` are the maintainer's request. Percentiles
`(5, 10, 20)` are the canonical top and bottom cuts of descriptive statistics
and portfolio sorting, chosen so that any n satisfying the quantile default
satisfies them. Jenks `(2, 3, 4, 5)` spans the single natural break, the
high-middle-low split, and the class count cartographic practice defaults
to. A default a column cannot satisfy is refused with the disabling argument
named, never skipped.

## Accepted costs

Exact arithmetic over performance. Measured on the implementation with the
default methods: one hundred thousand rows partition in 1.7 s when the column
holds a thousand distinct values and in 11.8 s when every value is distinct,
the difference being Jenks; one million rows with a thousand distinct values
partition in 22.5 s with a 127 MB peak. Both are dominated by reading the
input at the sizes the downstream pipeline can cluster, and are accepted for
this release.

## Revision condition

Reopen the exact `float64` coarsening of the sort key if the key memory is
measured as a limiting cost, or a resource gate for Jenks if its time is
measured as infeasible at a realistic number of distinct values. Reopen the
reader seam's contract if a third consumer needs different validation. A
request to expose partitioning through `damicore.run` reopens ADR 0013's
signature question for that surface only.
