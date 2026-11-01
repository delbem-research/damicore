# damicore-distance

damicore-distance computes an exact, unclamped, `float64` Normalized Compression
Distance (NCD) matrix from a normalization manifest. It is the second stage of
the DAMICORE pipeline; most users install the aggregate `damicore` distribution,
which runs all four stages end to end. Install this package alone to compute NCD
matrices without the rest of the pipeline.

```bash
pip install damicore-distance
```

## Python

```python
from damicore_distance import DistanceConfig, compute_distance_matrix

# `workers="auto"` opens a process pool whose workers re-import the calling module, so in a
# `.py` script this call must sit under the guard below. A notebook or REPL satisfies it too.
if __name__ == "__main__":
    result = compute_distance_matrix(
        "normalization/manifest.json",
        "run",
        config=DistanceConfig(compressor="zlib", workers="auto"),
    )
```

The manifest is produced by the sibling `damicore-normalizer` distribution; the
call writes `distance.npy` and `labels.json`, the inputs the sibling
`damicore-tree-builder` distribution consumes. Compression is incremental, pairs
are lexicographically sharded, only the coordinator writes the memory map, and
compatible completed shards can resume.

`DistanceMatrixView` reads the matrix through NumPy slicing and `shape` with no
extra dependency. Its two pandas conveniences, `head()` and `to_pandas()`, raise
a `DistanceError` (`missing_dependency_error`) unless pandas is present; install
it with `pip install "damicore-distance[pandas]"`.

## Links

- Repository: <https://github.com/delbem-research/damicore>
- Issues: <https://github.com/delbem-research/damicore/issues>
- Documentation:
  <https://github.com/delbem-research/damicore/blob/main/docs/quickstart.md>

Licensed under Apache-2.0.
