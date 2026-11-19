# Normalizer

`damicore_normalizer` turns an input into objects, and is the only producer of the versioned normalization manifest. It also partitions a dataset, upstream of that.

## Functions

```{eval-rst}
.. autofunction:: damicore_normalizer.materialize_objects
.. autofunction:: damicore_normalizer.normalize_csv
.. autofunction:: damicore_normalizer.partition_dataset
```

## Models

```{eval-rst}
.. autoclass:: damicore_normalizer.DelimitedSource
   :members:

.. autoclass:: damicore_normalizer.FileCorpusSource
   :members:

.. autoclass:: damicore_normalizer.NormalizationConfig
   :members:

.. autoclass:: damicore_normalizer.NormalizationManifest
   :members:

.. autoclass:: damicore_normalizer.NormalizationResult
   :members:

.. autoclass:: damicore_normalizer.ObjectDescriptor
   :members:

.. autoclass:: damicore_normalizer.PartitionFile
   :members:

.. autoclass:: damicore_normalizer.PartitionManifest
   :members:

.. autoclass:: damicore_normalizer.PartitionResult
   :members:

.. autoclass:: damicore_normalizer.SpreadsheetSource
   :members:

```

## Errors

```{eval-rst}
.. autoclass:: damicore_normalizer.NormalizerError
```
