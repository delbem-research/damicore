# Orchestration

`damicore` runs the whole pipeline, estimates what a run will cost before it starts, and loads a finished run back into verified Python objects.

## Functions

```{eval-rst}
.. autofunction:: damicore.estimate
.. autofunction:: damicore.load_result
.. autofunction:: damicore.run
```

## Models

```{eval-rst}
.. autoclass:: damicore.ArtifactPaths
   :members:

.. autoclass:: damicore.DamicoreResult
   :members:

.. autoclass:: damicore.ExecutionConfig
   :members:

.. autoclass:: damicore.ResourceEstimate
   :members:

.. autoclass:: damicore.ResourceLimits
   :members:

.. autoclass:: damicore.RunReport
   :members:

```

## Errors

```{eval-rst}
.. autoclass:: damicore.ArtifactValidationError
.. autoclass:: damicore.CheckpointMismatchError
.. autoclass:: damicore.ClusterizationError
.. autoclass:: damicore.CompressionError
.. autoclass:: damicore.ConfigurationError
.. autoclass:: damicore.DamicoreError
.. autoclass:: damicore.DatasetFormatError
.. autoclass:: damicore.DistanceComputationError
.. autoclass:: damicore.DistanceMatrixValidationError
.. autoclass:: damicore.InputValidationError
.. autoclass:: damicore.MaterializationError
.. autoclass:: damicore.NormalizationError
.. autoclass:: damicore.OutputDirectoryConflictError
.. autoclass:: damicore.ResourceLimitError
.. autoclass:: damicore.TreeBuildError
.. autoclass:: damicore.TreeFormatError
```

`DistanceMatrixView` is re-exported here from `damicore_distance`; it is described once, in [distance](distance.md).
