from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from damicore_normalizer.manifest import (
    NormalizationInput,
    ObjectDescriptor,
    ObjectEncoding,
)


@dataclass(frozen=True)
class ScanResult:
    """Everything a caller can learn about the objects without committing to writing them.

    The same call produces this whether or not ``objects_dir`` was given, so preflight and
    the real run agree by construction rather than by two implementations staying in step.
    ``max_serialized_chunk_bytes`` bounds the peak in-memory payload the source produced;
    for adopted files it is the largest single file.
    """

    objects: tuple[ObjectDescriptor, ...]
    total_bytes: int
    max_serialized_chunk_bytes: int
    manifest_input: NormalizationInput
    object_encoding: ObjectEncoding
    source_paths: tuple[Path, ...]
    # (size_bytes, mtime_ns) observed for each source path while it was being read. The
    # caller re-stats the same paths afterwards, which is how a set of inputs gets the drift
    # check a single input hash used to provide.
    source_fingerprints: tuple[tuple[int, int], ...]
