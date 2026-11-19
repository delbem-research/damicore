"""Resolve files into an ordered corpus whose objects are the user's bytes unchanged.

References
----------
.. [adr-0006] The object source is an axis owned by the normalizer. This unit is the half
   where the files already are the objects, so nothing is split and nothing is re-encoded.
.. [adr-0007] A run directory is self-contained: adopted files are copied into it rather
   than referenced where they live. What this unit takes from the record is the copy and
   the containment it protects, which `load_result`, `DamicoreResult.save` and every hash
   re-verification assume. What it does not take is any claim about the bytes themselves --
   they are the user's, and `raw-bytes/1` is the honest name for that.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from damicore_normalizer.config import FileCorpusSource
from damicore_normalizer.errors import NormalizerError
from damicore_normalizer.manifest import ObjectDescriptor

_READ_CHUNK_BYTES = 4_194_304


@dataclass(frozen=True)
class CorpusScan:
    objects: tuple[ObjectDescriptor, ...]
    total_bytes: int
    largest_file_bytes: int
    root: Path
    set_digest: str
    # (path, size_bytes, mtime_ns) per adopted file, so the caller can re-stat the whole
    # corpus afterwards. A single input hash cannot express drift across a set.
    stats: tuple[tuple[Path, int, int], ...]


def _reject(message: str, code: str = "corpus_validation_error") -> NormalizerError:
    return NormalizerError(message, code=code)


def _walk(directory: Path, *, recursive: bool) -> Iterable[Path]:
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    for entry in entries:
        if entry.is_symlink():
            raise _reject(f"Corpus entry is a symlink: {entry}")
        if entry.is_dir():
            if recursive:
                yield from _walk(entry, recursive=recursive)
            continue
        yield entry


def _collect(sources: Sequence[Path], source: FileCorpusSource) -> tuple[Path, list[Path]]:
    """Resolve the requested paths into an ordered corpus and the root labels are relative to.

    The root is the given directory when a single directory was requested, so labels read the
    way the user asked for them. For any other shape it is the common ancestor of the files
    themselves, which is the only choice that keeps every label distinct.

    ``sources`` arrives already resolved, so a source path never reaches here as a symlink and
    the symlink rule applies to the entries found beneath it, in :func:`_walk`.
    """
    files: list[Path] = []
    for entry in sources:
        if entry.is_dir():
            files.extend(_walk(entry, recursive=source.recursive))
        elif entry.exists():
            files.append(entry)
        else:
            raise _reject(f"Corpus path does not exist: {entry}", code="input_validation_error")

    if len(sources) == 1 and sources[0].is_dir():
        root = sources[0]
    elif len(files) > 1:
        root = Path(os.path.commonpath([str(path) for path in files]))
    else:
        root = files[0].parent if files else Path.cwd()

    if not source.include_hidden:
        files = [
            path
            for path in files
            if not any(part.startswith(".") for part in path.relative_to(root).parts)
        ]

    seen: set[Path] = set()
    for path in files:
        if path in seen:
            raise _reject(f"Corpus contains the same file twice: {path}")
        seen.add(path)
        if not path.is_file():
            raise _reject(f"Corpus entry is not a regular file: {path}")
        if path.stat().st_size == 0:
            # An empty object compresses to a bare header, so every empty file measures as
            # distance 0 from every other one and they form a cluster that means nothing.
            raise _reject(f"Corpus entry is empty: {path}")

    # Sorted on the POSIX relative path. Python compares strings by code point and UTF-8
    # preserves code point order, so this is the byte-wise order the manifest promises and
    # it does not vary with the machine's locale.
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    if len(files) < 2:
        raise _reject(f"A corpus needs at least two files; found {len(files)}")
    return root, files


def _digest_file(path: Path, target: Path | None) -> tuple[str, int]:
    """Hash a corpus file, copying it in the same pass when a destination is given."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            if target is None:
                while chunk := stream.read(_READ_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
            else:
                with target.open("wb") as output:
                    while chunk := stream.read(_READ_CHUNK_BYTES):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                shutil.copystat(path, target)
    except OSError as exc:
        raise _reject(f"Could not read corpus file: {path}", code="input_validation_error") from exc
    return digest.hexdigest(), size


def _set_digest(records: Iterable[tuple[str, int, str]]) -> str:
    """Identify the corpus by its whole contents, in order.

    Each record is encoded as compact JSON so a label containing a separator cannot be
    confused with a record boundary; a digest over a delimiter-joined string could not tell
    two different corpora apart.
    """
    digest = hashlib.sha256()
    for label, size, sha in records:
        line = json.dumps([label, size, sha], ensure_ascii=False, separators=(",", ":"))
        digest.update(line.encode("utf-8") + b"\n")
    return digest.hexdigest()


def scan_corpus(
    sources: Sequence[Path],
    source: FileCorpusSource,
    *,
    objects_dir: Path | None = None,
) -> CorpusScan:
    """Adopt files as objects, copying them into ``objects_dir`` when one is given.

    Object bytes are the user's bytes; nothing is serialized, which is why the manifest
    records ``raw-bytes/1``. Object identifiers stay positional and the label carries the
    relative path, so a corpus never depends on a filename surviving another filesystem's
    case folding, length limits, or reserved names.
    """
    root, files = _collect(sources, source)
    if objects_dir is not None:
        objects_dir.mkdir(parents=True, exist_ok=False)

    objects: list[ObjectDescriptor] = []
    stats: list[tuple[Path, int, int]] = []
    records: list[tuple[str, int, str]] = []
    largest = 0
    for index, path in enumerate(files, start=1):
        object_id = f"file_{index:06d}"
        label = path.relative_to(root).as_posix()
        target = objects_dir / object_id if objects_dir is not None else None
        sha256, size = _digest_file(path, target)
        objects.append(
            ObjectDescriptor(
                object_id=object_id,
                label=label,
                relative_path=f"objects/{object_id}",
                size_bytes=size,
                sha256=sha256,
            )
        )
        stats.append((path, size, path.stat().st_mtime_ns))
        records.append((label, size, sha256))
        largest = max(largest, size)

    return CorpusScan(
        objects=tuple(objects),
        total_bytes=sum(item.size_bytes for item in objects),
        largest_file_bytes=largest,
        root=root,
        set_digest=_set_digest(records),
        stats=tuple(stats),
    )
