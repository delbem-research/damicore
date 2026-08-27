from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from damicore_distance.errors import DistanceError

if TYPE_CHECKING:
    import pandas as pd

# pandas backs only these two convenience views, so it is an extra rather than a dependency:
# the rest of this package is NumPy, and a caller who installs it alone should not carry
# pandas for two methods. The message is the single place that names the remedy.
_PANDAS_REQUIRED = (
    "pandas is required by this method and is not installed. Install it with "
    "`pip install damicore-distance[pandas]`, or read the matrix through NumPy slicing "
    "and shape, which need no extra."
)


class DistanceResult(BaseModel):
    """What one distance stage produced, including the range of the matrix it wrote.

    The three ``ncd_*`` fields are measured during the validation pass this stage already
    performs over every cell, so reporting the range costs no additional traversal. They
    describe the whole matrix, diagonal included, which is why ``ncd_min`` is ``0.0`` for
    any matrix whose values are all non-negative.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    matrix_path: Path
    labels_path: Path
    object_count: int = Field(ge=0)
    pair_count: int = Field(ge=0)
    timing: float = Field(ge=0)
    ncd_min: float = Field(default=0.0, allow_inf_nan=False)
    ncd_max: float = Field(default=0.0, allow_inf_nan=False)
    ncd_out_of_range_count: int = Field(default=0, ge=0)


class DistanceMatrixView:
    """A read-only view of a persisted ``distance.npy``, backed by a memory map.

    The array is never loaded whole. ``shape``, ``dtype``, and indexing read through NumPy
    slicing, so a submatrix costs only the pages it touches; the file is opened with
    ``allow_pickle=False``, so an artifact cannot execute code. The map stays open until
    ``close()``, after which every accessor raises ``ValueError`` -- except ``head`` and
    ``to_pandas``, which test for pandas before the closed check and so report the missing
    extra first when it is absent.

    ``head`` and ``to_pandas`` are the only members that need pandas, which is an extra of
    this distribution rather than a dependency. Without it they raise ``DistanceError`` with
    code ``missing_dependency_error``. ``to_pandas`` additionally refuses to materialize a
    matrix larger than ``materialization_limit_bytes`` unless ``force=True``, raising the
    injected ``materialization_error`` -- ``ValueError`` by default, and
    ``damicore.MaterializationError`` for a view built by ``damicore.load_result``.
    """

    def __init__(
        self,
        path: str | Path,
        labels: list[str],
        *,
        materialization_limit_bytes: int = 268_435_456,
        materialization_error: Callable[[str], Exception] = ValueError,
    ) -> None:
        self.path = Path(path)
        self.labels = tuple(labels)
        self._limit = materialization_limit_bytes
        self._materialization_error = materialization_error
        self._matrix: np.memmap[Any, Any] | None = np.load(
            self.path, mmap_mode="r", allow_pickle=False
        )

    @property
    def shape(self) -> tuple[int, ...]:
        return self._require_open().shape

    @property
    def dtype(self) -> np.dtype[Any]:
        return self._require_open().dtype

    def __getitem__(self, key: int | slice | tuple[int | slice, int | slice]) -> object:
        return self._require_open()[key]

    def head(self, n: int = 5) -> pd.DataFrame:
        try:
            import pandas as pd
        except ImportError as exc:
            raise DistanceError(_PANDAS_REQUIRED, code="missing_dependency_error") from exc

        size = min(max(n, 0), len(self.labels))
        values = np.asarray(self._require_open()[:size, :size])
        labels = list(self.labels[:size])
        return pd.DataFrame(values, index=labels, columns=labels)

    def to_pandas(self, force: bool = False) -> pd.DataFrame:
        try:
            import pandas as pd
        except ImportError as exc:
            raise DistanceError(_PANDAS_REQUIRED, code="missing_dependency_error") from exc

        matrix = self._require_open()
        if matrix.nbytes > self._limit and not force:
            raise self._materialization_error(
                f"Matrix materialization requires {matrix.nbytes} bytes; "
                "use head(), NumPy slicing, or to_pandas(force=True)"
            )
        return pd.DataFrame(np.asarray(matrix), index=self.labels, columns=self.labels)

    def close(self) -> None:
        # `_mmap` is the only handle numpy exposes for releasing a memory map deterministically,
        # and it is private. Its absence is tolerated so close() never raises out of a caller's
        # finally block -- but that tolerance would otherwise be silent, leaving a leaked map
        # behind a view that reports itself closed, so a test pins the attribute's existence
        # rather than leaving this branch to speak for it.
        if self._matrix is not None:
            mmap = getattr(self._matrix, "_mmap", None)
            if mmap is not None:
                mmap.close()
            self._matrix = None

    def _require_open(self) -> np.memmap[Any, Any]:
        if self._matrix is None:
            raise ValueError("Distance matrix is closed; reload it with load_result")
        return self._matrix
