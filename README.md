# DAMICORE

[![PyPI](https://img.shields.io/pypi/v/damicore)](https://pypi.org/project/damicore/)
[![Python versions](https://img.shields.io/pypi/pyversions/damicore)](https://pypi.org/project/damicore/)
[![License](https://img.shields.io/pypi/l/damicore)](LICENSE)
[![CI](https://github.com/Delbem-Research-and-Innovation/damicore/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Delbem-Research-and-Innovation/damicore/actions/workflows/ci.yml)

DAMICORE clusters local files, or the rows or columns of a local dataset,
through canonical serialization, exact Normalized Compression Distance (NCD),
deterministic Neighbor Joining, and FastGreedy community detection.

Objects come from one of two sources. A **dataset** is split by column or by
row: delimited text (`.csv`, `.tsv`, `.txt`, any single-character delimiter) or
an `.xlsx`/`.xlsm` worksheet. A **files** source takes files or a directory of
files that already are the objects, text or binary, with nothing to split.

```bash
pip install damicore
```

```python
from damicore import estimate, run

# The default worker count opens a process pool, and each worker re-imports the calling
# module. In a `.py` script the call must therefore sit under this guard. A notebook or REPL
# also satisfies it, so the guard is safe everywhere; `ExecutionConfig(workers=1)` avoids the
# pool entirely.
if __name__ == "__main__":
    preview = estimate("dataset.csv", split="columns")
    print(preview.model_dump())

    result = run("dataset.csv", split="columns")
    print(result.membership)
    print(result.clusters)
    print(result.tree_newick)
    print(result.distance_matrix.head())
    result.close()

    # A worksheet. `sheet=` is required when the workbook holds more than one.
    sheet_result = run("dataset.xlsx", source_kind="xlsx", split="columns")
    sheet_result.close()

    # A corpus: every file is an object, so there is no split, delimiter, or encoding.
    corpus_result = run("corpus", source_kind="files")
    print(corpus_result.membership)
    corpus_result.close()
```

```bash
damicore run corpus --source files
damicore run dataset.xlsx --source xlsx --sheet Sheet1
```

The default exact algorithm accepts at most 1,000 objects, 500,000 pairs, and
512 MiB per matrix. What decides feasibility is how many objects the source
produces, not how many bytes it holds, so a multi-gigabyte dataset with tens of
columns is feasible while the same file split into millions of rows is rejected
during preflight. See [scalability](docs/scalability.md) before raising a limit.

Runs are content-addressed, checkpointed, resumable, and verified before they
become `completed`. Internal paths are contained in the run directory, JSON
writes are atomic, and completed artifacts are hash-checked by `load_result`.
See [quickstart](docs/quickstart.md), [input contract](docs/input-contract.md),
[artifact contract](docs/artifacts.md), and [scalability](docs/scalability.md).

The five public distributions are `damicore`, `damicore-normalizer`,
`damicore-distance`, `damicore-tree-builder`, and `damicore-clusterizer`.
Stage packages do not import one another. `synthetic-data` is workspace-only
and is never published.

For Colab, process and checkpoint on local `/content`, then use `result.save`
to copy completed artifacts to a mounted Drive destination. DAMICORE never
imports `google.colab`, accesses the network, or uploads data.

## Citation

If you use this code in scientific works, please cite:

> Lopes, E. P., Tokuda, E. K., & Delbem, A. C. B. (2026). *DAMICORE*
> (Version 0.2.0) [Computer software].
> https://pypi.org/project/damicore/

The DAMICORE methodology was originally introduced in:

> Sanches, A. K., Cardoso, J. M. P., & Delbem, A. C. B. (2011).
> “Identifying Merge-Beneficial Software Kernels for Hardware Implementation.”
> *2011 International Conference on Reconfigurable Computing and FPGAs
> (ReConFig)*, 74–79.
> https://doi.org/10.1109/ReConFig.2011.51

Complete machine-readable citation metadata, including author ORCIDs,
project metadata, the original methodological publication, and references to
earlier implementations that contributed to or served as methodological
references for the current software, is available in
[`CITATION.cff`](CITATION.cff).

## Development

```bash
make install
make check
make test
make build
```

Python 3.11–3.14 is supported. Contributor rules live in [AGENTS.md](AGENTS.md) and the
behavior they govern is fixed by the test suite; the reasoning behind the design is
recorded in [docs/decisions/](docs/decisions). Publishing a version is documented in
[docs/releasing.md](docs/releasing.md).
