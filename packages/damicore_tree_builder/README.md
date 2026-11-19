# damicore-tree-builder

damicore-tree-builder builds a deterministic Neighbor Joining tree from a
distance matrix and its labels. It is the third stage of the DAMICORE pipeline;
most users install the aggregate `damicore` distribution, which runs all four
stages end to end. Install this package alone to build trees without the rest of
the pipeline.

```bash
pip install damicore-tree-builder
```

## Python

```python
from damicore_tree_builder import build_tree

result = build_tree("run/distance.npy", "run/labels.json", "run")
```

`distance.npy` and `labels.json` are produced by the sibling `damicore-distance`
distribution; the call writes `tree.json` plus Newick, and `tree.json` is the
input the sibling `damicore-clusterizer` distribution consumes. The path API
copies into a temporary memory map, reuses slots, evaluates Q pairwise without
materializing it, and preserves negative branches. `neighbor_joining(matrix,
labels)` is available for small in-memory matrices.

## Links

- Repository: <https://github.com/delbem-research/damicore>
- Issues: <https://github.com/delbem-research/damicore/issues>
- Documentation:
  <https://github.com/delbem-research/damicore/blob/main/docs/quickstart.md>

Licensed under Apache-2.0.
