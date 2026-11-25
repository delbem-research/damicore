from __future__ import annotations

import codecs
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DelimitedSource(BaseModel):
    """Split one delimited-text file. Any single character is a delimiter, so `.csv`,
    `.tsv`, and `.txt` are the same source with different separators."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["delimited"] = "delimited"
    split: Literal["columns", "rows"] = "columns"
    delimiter: str = ","
    encoding: str = "utf-8"

    @field_validator("delimiter")
    @classmethod
    def _validate_delimiter(cls, value: str) -> str:
        if len(value) != 1:
            raise ValueError("delimiter must contain exactly one Unicode character")
        return value

    @field_validator("encoding")
    @classmethod
    def _validate_encoding(cls, value: str) -> str:
        codecs.lookup(value)
        return value


class SpreadsheetSource(BaseModel):
    """Split one worksheet of an `.xlsx`/`.xlsm` workbook.

    ``sheet`` is ``None`` only when the workbook holds exactly one worksheet. A workbook
    with several requires the name, because defaulting to the first would silently decide
    which data was analyzed. There is no delimiter or encoding: a workbook declares neither.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["xlsx"] = "xlsx"
    split: Literal["columns", "rows"] = "columns"
    sheet: str | None = None


class FileCorpusSource(BaseModel):
    """Adopt files that are already the objects. No split, no delimiter, no encoding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["files"] = "files"
    recursive: bool = True
    include_hidden: bool = False


ObjectSource = Annotated[
    DelimitedSource | SpreadsheetSource | FileCorpusSource,
    Field(discriminator="kind"),
]


class NormalizationConfig(BaseModel):
    """What the objects are (``source``) and how the run is executed (everything else)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: ObjectSource = DelimitedSource()
    chunk_rows: int = Field(default=50_000, gt=0)
    # The most object files a column split holds open at once. It bounds descriptors, not
    # speed: a wider table writes its other columns from a fixed buffer, opening each file once
    # per flush rather than once per cell (issue #134).
    max_open_files: int = Field(default=64, gt=0)
