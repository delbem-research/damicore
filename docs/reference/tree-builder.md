# Tree builder

`damicore_tree_builder` builds a deterministic Neighbor Joining tree from a distance matrix and writes it in Newick form.

## Functions

```{eval-rst}
.. autofunction:: damicore_tree_builder.build_tree
.. autofunction:: damicore_tree_builder.neighbor_joining
```

## Models

```{eval-rst}
.. autoclass:: damicore_tree_builder.Tree
   :members:

.. autoclass:: damicore_tree_builder.TreeBuildConfig
   :members:

.. autoclass:: damicore_tree_builder.TreeBuildResult
   :members:

.. autoclass:: damicore_tree_builder.TreeEdge
   :members:

.. autoclass:: damicore_tree_builder.TreeNode
   :members:

```

## Errors

```{eval-rst}
.. autoclass:: damicore_tree_builder.TreeBuilderError
```
