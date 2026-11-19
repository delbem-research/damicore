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
        config=DistanceConfig(workers="auto"),
    )
```

`compressor` is one settings model per codec, holding only what that codec reads. The
default, `PpmdCompressor()`, is PPMd variant I at model order 6 with 64 MiB of model
memory (`ppmd_order`, `ppmd_memory_bytes`), which keeps a shared distribution across
large objects. `LzmaCompressor` (raw LZMA2, `lzma_dictionary_bytes`, default 8 MiB)
finds shared content at any size its dictionary holds; `Bzip2Compressor`
(`bzip2_block_size_100k`) sees both within its 900 kB block; `DeflateCompressor`, with
`name="zlib"` or `"gzip"` and `compression_level`, is DEFLATE, whose 32 KiB window makes
objects larger than that look unrelated. Every NCD value is a function of these settings:

```python
from damicore_distance import DistanceConfig, LzmaCompressor

config = DistanceConfig(compressor=LzmaCompressor(lzma_dictionary_bytes=67_108_864))
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
