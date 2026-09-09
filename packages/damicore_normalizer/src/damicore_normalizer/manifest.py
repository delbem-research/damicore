from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ObjectDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    object_id: str
    label: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("relative_path")
    @classmethod
    def _relative_path_is_contained(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError("relative_path must be a contained POSIX path")
        return value


class DelimitedDatasetInput(BaseModel):
    """One delimited-text file split into objects. `.csv`, `.tsv`, and `.txt` are this."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["delimited"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    delimiter: str = Field(min_length=1, max_length=1)
    encoding: str
    split: Literal["columns", "rows"]


class SpreadsheetDatasetInput(BaseModel):
    """One worksheet of an `.xlsx`/`.xlsm` workbook split into objects.

    `sheet` is resolved rather than defaulted: it names the worksheet actually read, so a
    manifest never leaves which sheet was analyzed to be inferred. `cell_text_rule` names
    the rule that turned typed cells into text, because that rule -- not the parsing
    library -- is what the object bytes depend on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["xlsx"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    sheet: str
    split: Literal["columns", "rows"]
    cell_text_rule: Literal["v1"]


class FileCorpusInput(BaseModel):
    """A set of files that are already the objects.

    `sha256` is a digest over the whole set rather than over one file, because no single
    input file exists to identify the run. `root` is the directory the labels are relative
    to, which is what makes them unique.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["files"]
    root: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=2)
    recursive: bool
    include_hidden: bool


# Discriminated on `kind`, so an input block is parsed into exactly one variant and a field
# belonging to another variant is rejected rather than ignored. The union is what makes
# `split`, `delimiter`, and `encoding` conditional on the source instead of universal.
NormalizationInput = Annotated[
    DelimitedDatasetInput | SpreadsheetDatasetInput | FileCorpusInput,
    Field(discriminator="kind"),
]

# The encoding that produced the object bytes. An NCD value is only meaningful relative to
# it, so it is recorded rather than assumed. `raw-bytes/1` is the honest name for adopted
# files, whose objects are the user's bytes unchanged.
ObjectEncoding = Literal["json-lines/1", "raw-bytes/1"]


class NormalizationManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[2]
    object_encoding: ObjectEncoding
    input: NormalizationInput
    objects: tuple[ObjectDescriptor, ...]

    @model_validator(mode="after")
    def _encoding_matches_the_source(self) -> Self:
        expected = "raw-bytes/1" if self.input.kind == "files" else "json-lines/1"
        if self.object_encoding != expected:
            raise ValueError(f"{self.input.kind} objects must carry object_encoding {expected}")
        return self


class NormalizationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    manifest_path: Path
    object_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    objects: tuple[ObjectDescriptor, ...]


PartitionMethod = Literal["quantile", "percentile", "jenks"]
PartitionEnd = Literal["high", "low"]

# The order files take in a manifest, and the parameter range each method admits. Both live
# beside the schema they validate so a hand-edited manifest is refused by the same rule the
# writer followed.
PARTITION_METHOD_ORDER: tuple[PartitionMethod, ...] = ("quantile", "percentile", "jenks")
PARTITION_PARAMETER_RANGES: dict[PartitionMethod, tuple[int, int | None]] = {
    "quantile": (2, None),
    "percentile": (1, 50),
    "jenks": (2, None),
}


class DelimitedPartitionInput(BaseModel):
    """The delimited file a partition read. Not the normalization manifest's variant: that
    one carries `split`, which does not apply to a partition and could not be carried
    without being ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["delimited"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    delimiter: str = Field(min_length=1, max_length=1)
    encoding: str


class SpreadsheetPartitionInput(BaseModel):
    """The worksheet a partition read, with the resolved sheet name and the cell-text rule
    that turned its typed cells into the text the numeric grammar saw."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["xlsx"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    sheet: str
    cell_text_rule: Literal["v1"]


PartitionInput = Annotated[
    DelimitedPartitionInput | SpreadsheetPartitionInput,
    Field(discriminator="kind"),
]


class PartitionFile(BaseModel):
    """One emitted subset: which partition end it is, which ranks it holds, and its bytes.

    The file name and the row count are derived, not persisted: the name is fixed by the
    naming rule and the count is a difference of two fields, so storing either would be a
    second copy of a truth already in the record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    method: PartitionMethod
    parameter: int
    end: PartitionEnd
    rank_start: int = Field(ge=0)
    rank_end: int
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _parameter_in_range_and_interval_non_empty(self) -> Self:
        low, high = PARTITION_PARAMETER_RANGES[self.method]
        if self.parameter < low or (high is not None and self.parameter > high):
            raise ValueError(f"{self.method} parameter {self.parameter} is out of range")
        if self.rank_start >= self.rank_end:
            raise ValueError("a partition file holds at least one rank")
        return self

    @property
    def relative_path(self) -> str:
        return f"{self.method}_{self.parameter:02d}_{self.end}.csv"

    @property
    def row_count(self) -> int:
        return self.rank_end - self.rank_start


class PartitionManifest(BaseModel):
    """The schema of partition.json: what reproduces the partition and what verifies it."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    partition_rule: Literal["v1"]
    input: PartitionInput
    column: str
    decimal: Literal[".", ","]
    row_count: int = Field(ge=2)
    files: tuple[PartitionFile, ...]

    @model_validator(mode="after")
    def _files_are_paired_ordered_and_within_the_rows(self) -> Self:
        ends: dict[tuple[PartitionMethod, int], set[PartitionEnd]] = {}
        for item in self.files:
            if item.rank_end > self.row_count:
                raise ValueError("a partition file ends past the last rank")
            if item.end == "high" and item.rank_start != 0:
                raise ValueError("a high file starts at rank 0")
            if item.end == "low" and item.rank_end != self.row_count:
                raise ValueError("a low file ends at the last rank")
            ends.setdefault((item.method, item.parameter), set()).add(item.end)
        if any(present != {"high", "low"} for present in ends.values()) or len(self.files) != (
            2 * len(ends)
        ):
            raise ValueError("every partition has exactly one high and one low file")
        keys = [
            (PARTITION_METHOD_ORDER.index(item.method), item.parameter, item.end == "low")
            for item in self.files
        ]
        if keys != sorted(keys):
            raise ValueError("partition files are ordered by method, parameter, then end")
        return self


class PartitionResult(BaseModel):
    """What partition_dataset returns: where the manifest is, and the manifest itself, so
    the result cannot disagree with the artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    manifest_path: Path
    manifest: PartitionManifest
