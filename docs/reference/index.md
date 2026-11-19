# API Reference

The reference is generated from the installed distributions, and it documents exactly what
each one lists in `__all__` — nothing more. Autodoc can import any implementation module;
`docs/check_reference.py` is what keeps one from being rendered as public API by accident.

The same object can be reachable from more than one import path. `DistanceMatrixView` is
exported by both `damicore` and `damicore_distance`, because a reader holding a result and a
reader driving one stage both need it. Each distribution's surface is verified on its own, so
a shared object is rendered once and referenced twice rather than documented twice.

```{toctree}
:maxdepth: 1

damicore
normalizer
distance
tree-builder
clusterizer
```
