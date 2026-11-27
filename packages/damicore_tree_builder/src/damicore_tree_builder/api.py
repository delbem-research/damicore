from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, ValidationError

from damicore_tree_builder.artifacts import write_tree_artifacts
from damicore_tree_builder.config import TreeBuildConfig
from damicore_tree_builder.errors import TreeBuilderError
from damicore_tree_builder.models import Tree, TreeBuildResult
from damicore_tree_builder.neighbor_joining import build_neighbor_joining, validate_matrix


class _LabelsArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    object_ids: tuple[str, ...]
    labels: tuple[str, ...]


def _load_labels(path: Path) -> tuple[list[str], list[str]]:
    try:
        artifact = _LabelsArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        object_ids = list(artifact.object_ids)
        labels = list(artifact.labels)
    except (OSError, ValidationError) as exc:
        raise TreeBuilderError("Invalid labels artifact", code="artifact_validation_error") from exc
    if (
        len(object_ids) != len(labels)
        or len(set(object_ids)) != len(object_ids)
        or len(set(labels)) != len(labels)
    ):
        raise TreeBuilderError(
            "labels and object_ids must have equal length and unique values",
            code="artifact_validation_error",
        )
    return object_ids, labels


def build_tree(
    matrix_path: str | Path,
    labels_path: str | Path,
    output_dir: str | Path,
    *,
    config: TreeBuildConfig | None = None,
) -> TreeBuildResult:
    """Build and persist a deterministic Neighbor Joining tree.

    Takes ``distance.npy`` and ``labels.json`` as the distance stage writes them, and writes
    ``tree.json`` and ``tree.nwk`` into ``output_dir``; both must be absent, since a tree is
    never overwritten. ``output_dir`` is created before that check, so even a rejected call
    leaves the directory behind. Joining runs against a private ``tree-work.npy`` memory map
    inside ``output_dir``, so the input matrix is left untouched and peak RAM does not grow
    with the matrix; once that workspace exists it is removed on every exit path, but a
    failure while creating it, a full disk being the realistic one, can leave it behind. The
    two artifacts are re-read and validated after writing, and deleted again if that fails, so
    a rejected tree never survives to block the next run. Ties in the Q criterion are broken
    by label order, which makes the output a function of the matrix and its labels alone.
    Negative branch lengths are written as computed and counted in the result, never
    clamped. The one exception is rounding residue of a zero, which is written as exactly
    ``0.0`` (ADR 0028).
    ``tree.json`` is the input :func:`damicore_clusterizer.cluster_tree` expects.

    Raises
    ------
    TreeBuilderError
        ``tree.json`` or ``tree.nwk`` already exists (``output_directory_conflict_error``);
        the labels artifact or the persisted tree is invalid, or the edges form a cycle
        (``artifact_validation_error``); the matrix is unreadable, mis-shaped, non-finite, or
        not symmetric with a zero diagonal (``distance_matrix_validation_error``).
    """
    started = time.monotonic()
    settings = config or TreeBuildConfig()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "tree.json").exists() or (destination / "tree.nwk").exists():
        raise TreeBuilderError(
            "Tree outputs already exist in the output directory",
            code="output_directory_conflict_error",
        )
    labels, display_labels = _load_labels(Path(labels_path).resolve())
    try:
        original = np.load(Path(matrix_path).resolve(), mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise TreeBuilderError(
            "Distance matrix artifact is unreadable",
            code="artifact_validation_error",
        ) from exc
    validate_matrix(original, labels, settings.q_block_size)
    workspace_path = destination / "tree-work.npy"
    workspace = np.lib.format.open_memmap(
        workspace_path, mode="w+", dtype=np.float64, shape=original.shape
    )
    try:
        for start in range(0, original.shape[0], settings.q_block_size):
            stop = min(start + settings.q_block_size, original.shape[0])
            workspace[start:stop] = original[start:stop]
        workspace.flush()
        tree = build_neighbor_joining(
            workspace,
            labels,
            display_labels,
            q_block_size=settings.q_block_size,
        )
        tree_path, newick_path = write_tree_artifacts(tree, destination)
        try:
            validated = Tree.model_validate_json(tree_path.read_text(encoding="utf-8"))
            if len([node for node in validated.nodes if node.kind == "leaf"]) != len(labels):
                raise TreeBuilderError(
                    "Persisted tree lost leaves", code="artifact_validation_error"
                )
            newick = newick_path.read_text(encoding="utf-8")
            # Only the terminator is checked. A semicolon inside a quoted label is data, so
            # counting them rejects valid output this package itself produces.
            if not newick.rstrip("\n").endswith(";"):
                raise TreeBuilderError(
                    "Persisted Newick artifact is malformed",
                    code="artifact_validation_error",
                )
        except BaseException:
            # The next run refuses a directory holding either artifact, so a rejected write
            # must not survive as a file that only manual cleanup can clear.
            tree_path.unlink(missing_ok=True)
            newick_path.unlink(missing_ok=True)
            raise
        negative = sum(edge.length < 0 for edge in tree.edges)
    finally:
        del workspace
        workspace_path.unlink(missing_ok=True)
    return TreeBuildResult(
        tree_path=tree_path,
        newick_path=newick_path,
        leaf_count=len(labels),
        negative_branch_count=negative,
        timing=time.monotonic() - started,
    )
