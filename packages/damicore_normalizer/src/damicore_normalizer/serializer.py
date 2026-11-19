"""Turn a cell or a row into the exact bytes a compressor sees.

An object's bytes decide every NCD value computed from it, so this encoding is fixed rather
than incidental: one compact JSON string per cell, one compact JSON array per row, UTF-8,
LF-terminated. That is what makes the bytes independent of chunking and of the platform,
and therefore the objects reproducible and hashable.

References
----------
.. [adr-0001] Canonical CSV serialization. No published work defines this encoding; it is
   this project's own and the record is its basis. What the record fixes is the form of the
   bytes, not these two functions, so a change here that keeps the form is free and one
   that alters it is a new record.
"""

from __future__ import annotations

import json
from collections.abc import Sequence


def serialize_cell(value: str) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def serialize_row(values: Sequence[str]) -> bytes:
    return (json.dumps(list(values), ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
