from damicore_normalizer.api import materialize_objects, normalize_csv, partition_dataset
from damicore_normalizer.config import (
    DelimitedSource,
    FileCorpusSource,
    NormalizationConfig,
    SpreadsheetSource,
)
from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.manifest import (
    NormalizationManifest,
    NormalizationResult,
    ObjectDescriptor,
    PartitionFile,
    PartitionManifest,
    PartitionResult,
)

__all__ = [
    "materialize_objects",
    "normalize_csv",
    "NormalizationConfig",
    "DelimitedSource",
    "SpreadsheetSource",
    "FileCorpusSource",
    "NormalizationResult",
    # The schema of manifest.json, which is the artifact the next stage reads. Exported
    # because it is already the documented contract between stages, so a consumer validating
    # one should not have to reach past this package's public surface to do it.
    "NormalizationManifest",
    "ObjectDescriptor",
    "NormalizerError",
    # Dataset partitioning, upstream of the source axis (ADR 0014). The manifest and file
    # models are exported for the same reason the normalization ones are: they are the
    # documented contract of partition.json, and a typed consumer of `manifest.files` should
    # see a public type.
    "partition_dataset",
    "PartitionResult",
    "PartitionManifest",
    "PartitionFile",
]
