# damicore-normalizer

damicore-normalizer turns an input source into canonical objects, recording the
size and SHA-256 of every one. Objects come from one of two sources: a dataset
split by column or by row -- delimited text (`.csv`, `.tsv`, `.txt`) or an
`.xlsx`/`.xlsm` worksheet -- or a set of files that already are the objects. It
is the first stage of the DAMICORE pipeline; most users install the aggregate
`damicore` distribution, which runs all four stages end to end. Install this
package alone to materialize objects without the rest of the pipeline.

```bash
pip install damicore-normalizer
```

## Python

```python
from damicore_normalizer import (
    DelimitedSource,
    FileCorpusSource,
    NormalizationConfig,
    SpreadsheetSource,
    materialize_objects,
)

# Split a delimited file. Any single character is a delimiter, so a tab-separated
# `.txt` is this same source with `delimiter="\t"`.
result = materialize_objects(
    "dataset.csv",
    "normalization",
    config=NormalizationConfig(source=DelimitedSource(split="columns"), chunk_rows=50_000),
)

# Split a worksheet. `sheet` is required when the workbook holds more than one.
spreadsheet = materialize_objects(
    "dataset.xlsx",
    "normalization-xlsx",
    config=NormalizationConfig(source=SpreadsheetSource(split="columns")),
)

# Adopt files that already are the objects. No split, delimiter, or encoding.
corpus = materialize_objects(
    "corpus",
    "normalization-files",
    config=NormalizationConfig(source=FileCorpusSource(recursive=True)),
)
```

A delimited source streams through `pandas.read_csv` and a worksheet streams
through `openpyxl` in read-only mode; both bound open column files with an LRU
pool. A file corpus is copied in and hashed, so the run directory stays
self-contained. Every source writes the objects plus a `manifest.json` into the
output directory, and that manifest is the input the sibling `damicore-distance`
distribution consumes to compute the NCD matrix.

The manifest records `object_encoding` -- `json-lines/1` for a split dataset,
`raw-bytes/1` for adopted files -- because an NCD value is only meaningful
relative to the bytes it measured.

## Partitioning a dataset by a numeric column

To cluster the best and the worst segments of a population separately, split
the dataset first. `partition_dataset` ranks the rows by one numeric column
and writes, for each requested partition, the rows with the highest values
and the rows with the lowest values as complete datasets, plus a
`partition.json` recording what chose them.

```python
from damicore_normalizer import partition_dataset

result = partition_dataset("population.csv", "segments", column="fitness")
for item in result.manifest.files:
    print(item.relative_path, item.row_count)
```

By default the halves, quarters, eighths, sixteenths and thirty-seconds
(`quantiles=(2, 4, 8, 16, 32)`), the top and bottom 5, 10 and 20 percent of
rows (`percentiles=(5, 10, 20)`), and Fisher-Jenks natural breaks into 2 to 5
classes (`jenks_classes=(2, 3, 4, 5)`) are emitted; pass an empty sequence to
disable a method. Files are named `{method}_{parameter:02d}_{high|low}.csv`,
hold the input's rows in input order with every cell unchanged, and are
always `,`-delimited UTF-8, so each one feeds `materialize_objects` or
`damicore.run` with no further argument. A spreadsheet is partitioned the
same way with `source_kind="xlsx"`.

The decimal separator of the column is detected by testing `.` and `,`
against every cell, or declared with `decimal=`; a column that fits neither
is refused naming the offending rows, never guessed. Every rule the files
depend on is versioned as `partition_rule` v1 and specified in the
[partitioning specification](https://github.com/Delbem-Research-and-Innovation/damicore/blob/main/docs/dataset-partitioning.md).

## Links

- Repository: <https://github.com/Delbem-Research-and-Innovation/damicore>
- Issues: <https://github.com/Delbem-Research-and-Innovation/damicore/issues>
- Documentation:
  <https://github.com/Delbem-Research-and-Innovation/damicore/blob/main/docs/quickstart.md>

Licensed under Apache-2.0.
