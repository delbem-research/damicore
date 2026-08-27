# damicore

DAMICORE clusters local files, or the rows or columns of a local dataset, without
asking you to pick a feature representation or a number of clusters. It serializes
each object canonically, measures every pair with exact Normalized Compression
Distance (NCD), builds a deterministic Neighbor Joining tree, and cuts communities
out of that tree with FastGreedy.

What one object is comes from `source_kind`, and nothing else changes with it:

| `source_kind` | Input | One object is |
|---|---|---|
| `delimited` (default) | `.csv`, `.tsv`, `.txt` — any single-character delimiter | one column, or one row |
| `xlsx` | an `.xlsx`/`.xlsm` worksheet | one column, or one row |
| `files` | files, a directory, or both | one file, bytes unchanged |

A setting that belongs to another source is rejected rather than ignored: `--split`
on a file corpus, or `--delimiter` on a worksheet, is a configuration error.

This distribution is the complete pipeline and the only one with a command line.
The four stage distributions — `damicore-normalizer`, `damicore-distance`,
`damicore-tree-builder`, `damicore-clusterizer` — are Python APIs that can be
installed and used on their own.

```bash
pip install damicore
```

## Python

```python
from damicore import ExecutionConfig, ResourceLimits, estimate, load_result, run

# `workers="auto"` opens a process pool whose workers re-import the calling module, so in a
# `.py` script this call must sit under the guard below. A notebook or REPL satisfies it too.
if __name__ == "__main__":
    preview = estimate("dataset.csv", split="columns")
    result = run(
        "dataset.csv",
        split="columns",
        execution=ExecutionConfig(workers="auto", limits=ResourceLimits()),
    )
    result.membership
    result.distance_matrix.head()
    result.close()

    restored = load_result(result.artifacts.run_dir)
    restored.close()

    # A worksheet is the same split read from a workbook. `sheet` names the worksheet and
    # is required when the workbook holds more than one.
    worksheet = run("dataset.xlsx", source_kind="xlsx", split="columns")
    worksheet.close()

    # Files that already are the objects. Nothing is split, so no delimiter or encoding
    # applies; the bytes are adopted as they are, text or binary.
    corpus = run("corpus", source_kind="files")
    corpus.close()
```

`estimate` reports the exact cost of a run — objects, pairs, matrix bytes, working
memory, free disk — without creating anything. Call it before raising a limit.

## Command line

```bash
damicore estimate dataset.csv --json
damicore run dataset.csv --split columns --output-dir ./results
damicore run dataset.xlsx --source xlsx --sheet measurements --split rows
damicore run ./corpus --source files --no-recursive
damicore run one.bin two.bin three.bin --source files
damicore --version
```

Progress and the artifact paths go to stderr. Only `estimate --json` writes to
stdout, so a shell pipeline reads one JSON document and nothing else.

### Exit codes

A failure is also one JSON line on stderr carrying a stable `code`, so a script can
branch on the status and log the reason.

| Status | Meaning |
|---:|---|
| 0 | Completed |
| 2 | Configuration or input rejected: a setting that does not apply to the source, a malformed dataset, or an unusable file corpus |
| 3 | A resource limit would be exceeded |
| 4 | A stage or an artifact failed; the `code` on stderr names which |
| 5 | The output directory conflicts, or a checkpoint does not match |
| 130 | Interrupted; the run is resumable |
| 141 | Terminated by a broken pipe (the SIGPIPE convention), e.g. when piping to `head` |

## Results

A run writes a versioned, hash-verified directory: the distance matrix as a
`float64` `.npy` memory map, the tree as JSON and Newick, cluster membership as CSV
and JSON, plus a manifest and a report. `load_result` reopens it, and an
interrupted run resumes from its checkpoints to the same bytes a fresh run would
have produced.

## Scale

The exact algorithm accepts at most 1,000 objects, 500,000 pairs and 512 MiB per
matrix by default. What decides feasibility is how many objects the source
produces, not how many bytes it holds: a multi-gigabyte dataset with tens of
columns is feasible, the same file split into millions of rows is not, and a file
corpus is bounded by its file count. Either way the run is rejected during
preflight rather than after hours of work.

Call `estimate` before raising a limit — it reports the exact cost without
creating anything. The complexity behind the gates is documented in
[scalability](https://github.com/Delbem-Research-and-Innovation/damicore/blob/main/docs/scalability.md).

## Links

- Source, issues and full documentation:
  <https://github.com/Delbem-Research-and-Innovation/damicore>
- Licensed under Apache-2.0.
