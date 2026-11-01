# DAMICORE

DAMICORE groups objects by how well they compress together. It materializes objects from a
dataset or a directory of files, computes an exact normalized compression distance between
every pair, builds a deterministic Neighbor Joining tree from that matrix, and partitions the
tree with FastGreedy community detection. The result is verified on load, and every run
directory is self-contained.

The library ships as five independently installable distributions. `damicore` orchestrates
the pipeline and is what most readers want; the four stage distributions are there when one
stage is wanted on its own.

```{toctree}
:caption: Start here
:maxdepth: 1

quickstart
input-contract
```

```{toctree}
:caption: Use
:maxdepth: 1

artifacts
dataset-partitioning
scalability
```

```{toctree}
:caption: Reference
:maxdepth: 2

reference/index
```

```{toctree}
:caption: Internals and contributing
:maxdepth: 1

internals/documentation
quality-drivers
releasing
internals/decisions
```
