# damicore-clusterizer

damicore-clusterizer clusters every node of a validated DAMICORE tree with
igraph FastGreedy and projects the resulting communities onto leaves. It is the
fourth and final stage of the DAMICORE pipeline; most users install the
aggregate `damicore` distribution, which runs all four stages end to end.
Install this package alone to cluster trees without the rest of the pipeline.

```bash
pip install damicore-clusterizer
```

## Python

```python
from damicore_clusterizer import ClusterConfig, cluster_tree

result = cluster_tree("run/tree.json", "run", config=ClusterConfig(num_clusters=None))
```

`tree.json` is produced by the sibling `damicore-tree-builder` distribution; the
call writes cluster membership as `membership.csv` and `clusters.json`. Edge
weights are branch-length reciprocals, shifted and scaled so the smallest is
one, which makes a negative branch the weakest edge rather than the strongest;
output cluster IDs and leaf ordering are deterministic.

## Links

- Repository: <https://github.com/delbem-research/damicore>
- Issues: <https://github.com/delbem-research/damicore/issues>
- Documentation:
  <https://github.com/delbem-research/damicore/blob/main/docs/quickstart.md>

Licensed under Apache-2.0.
