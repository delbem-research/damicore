# Input contract

DAMICORE measures bytes. Objects reach the distance stage from one of two
sources, and the source decides which of the settings below apply at all. A
setting that does not apply is rejected, never ignored.

## Dataset sources

A dataset is one existing regular local file, split into objects by column or by
row. `split="columns"` requires at least two columns and one data row;
`split="rows"` requires at least two data rows. Object IDs are positional
(`column_000001` or `row_000001`) and labels are presentation only.

Header names must be present, non-empty, and unique. Every record must carry
exactly as many fields as the header declares. A record with more or fewer is
rejected as a dataset format error, naming the position and the two counts,
because accepting one would drop a cell value or invent one the file never
contained.

**Delimited text** covers `.csv`, `.tsv`, and `.txt`: the delimiter is any one
Unicode character and the encoding is declared. Parsing is done with pandas in
chunks, strict decoding, string values, blank-line preservation, standard
double-quote rules, and no NA or type inference. A blank line is not a width
mismatch: it is preserved as a row of empty cells.

**Spreadsheets** cover `.xlsx` and `.xlsm`, read through openpyxl row by row.
There is no delimiter and no encoding, so passing either is a configuration
error. A workbook with one worksheet uses it; with more than one, the worksheet
must be named. The table is the smallest rectangle containing every non-blank
cell, so trailing blank rows and columns are trimmed and formatting alone cannot
invent objects. Legacy `.xls` is out of scope and names the conversion instead.

Spreadsheet cells are typed, so they cross to text through `cell_text_rule` v1,
recorded in the manifest and specified in
[ADR 0010](decisions/0010-cell-text-rule.md). Under it a `.csv` and an `.xlsx`
of the same logical table produce identical object bytes. Formulas are stored as
their text and never evaluated.

## The files source

A files source takes file paths, directory paths, or both; each file is one
object and nothing is split, so there is no delimiter, encoding, or split.
Object bytes are the user's bytes unchanged.

Files are ordered by their POSIX path relative to the source root, byte-wise and
independent of locale, since that order fixes the matrix indices. Labels are
those relative paths, which keeps two files sharing a basename distinct. A
corpus is identified by a SHA-256 over its whole ordered set of
`(label, size, digest)` records rather than by one file's digest.

Recursion and hidden-file policy are explicit and recorded in the manifest. An
empty file, a symlink, a non-regular entry, the same file listed twice, and a
corpus of fewer than two files are each rejected with a stable code; none is
skipped silently. Adopted files are copied into the run directory so it stays
self-contained, and preflight gates that cost before anything is written.

## Object bytes

Every split dataset produces `json-lines/1`: each cell is one compact UTF-8 JSON
string followed by LF for a column split, and each row is one compact JSON array
followed by LF for a row split. Values are not stripped, Unicode-normalized, or
otherwise changed, and different chunk sizes produce identical object bytes.
A files source produces `raw-bytes/1`.

The manifest records which of the two it used, because an NCD value is only
meaningful relative to the bytes it measured. See
[ADR 0008](decisions/0008-named-object-encoding.md).

## Partitioning by a numeric column

`partition_dataset` reads a dataset source under the rules above, with no
`split`, and writes subsets of its rows chosen by one numeric column. The
column's cells must satisfy a closed numeric grammar: ASCII digits, an
optional sign, one decimal separator, an optional exponent of at most six
digits, and spaces or tabs around it; nothing else, so `NaN`, an empty cell,
a thousands separator, or a line break inside a cell is refused naming the
data row. The separator is `.` or `,`, declared with `decimal=` or resolved by
testing both against every cell: the one every cell satisfies wins, a column
with no separator at all resolves to `.`, and a column that fits neither is
refused naming the first failing row of each hypothesis. There is no locale
and no sampling.

Rows are ordered by value descending, then file position, so equal values
keep their input order. Three methods cut that order: `quantile` q and
`percentile` p by rank, in exact integer arithmetic; `jenks` k by
Fisher-Jenks natural breaks over the distinct values, in exact rational
arithmetic, so equal values never separate and the result is identical on
every platform. Each partition emits a `high` and a `low` file, and every
emitted file is a pure subset of the input in input order, cells unchanged,
always `,`-delimited UTF-8 with LF, whatever the input was. Everything the
files depend on is versioned as `partition_rule` v1 and specified in
[docs/dataset-partitioning.md](dataset-partitioning.md);
the decision is [ADR 0014](decisions/0014-dataset-partitioning.md).
