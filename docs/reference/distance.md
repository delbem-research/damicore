# Distance

`damicore_distance` computes the exact normalized compression distance between every pair of objects, in bounded memory, resumably.

## Functions

```{eval-rst}
.. autofunction:: damicore_distance.compute_distance_matrix

.. autofunction:: damicore_distance.codec_fingerprint
```

## Models

```{eval-rst}
.. autoclass:: damicore_distance.DistanceConfig
   :members:

.. autoclass:: damicore_distance.PpmdCompressor
   :members:

.. autoclass:: damicore_distance.DeflateCompressor
   :members:

.. autoclass:: damicore_distance.Bzip2Compressor
   :members:

.. autoclass:: damicore_distance.LzmaCompressor
   :members:

.. autoclass:: damicore_distance.DistanceMatrixView
   :members:

.. autoclass:: damicore_distance.DistanceResult
   :members:

```

## Errors

```{eval-rst}
.. autoclass:: damicore_distance.DistanceError
```
