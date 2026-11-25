from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NormalizationObject(BaseModel):
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
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["delimited"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    delimiter: str = Field(min_length=1, max_length=1)
    encoding: str
    split: Literal["columns", "rows"]


class SpreadsheetDatasetInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["xlsx"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    sheet: str
    split: Literal["columns", "rows"]
    cell_text_rule: Literal["v1"]


class FileCorpusInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["files"]
    root: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=2)
    recursive: bool
    include_hidden: bool


# Mirrors damicore_normalizer's manifest models rather than importing them: the stage
# packages exchange versioned artifacts and never each other's code. This stage reads bytes,
# so it needs the input block only to confirm the manifest is one it understands -- which is
# exactly why the block must be able to describe a corpus. A single dataset-shaped model
# made an honest file-set manifest unrepresentable and rejected it.
NormalizationInput = Annotated[
    DelimitedDatasetInput | SpreadsheetDatasetInput | FileCorpusInput,
    Field(discriminator="kind"),
]


class NormalizationManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[2]
    object_encoding: Literal["json-lines/1", "raw-bytes/1"]
    input: NormalizationInput
    objects: tuple[NormalizationObject, ...]


class CompressedSizesCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    identity: dict[str, object]
    # Strictly positive: a compressor always emits a header, so no object compresses to
    # nothing. Without the bound a tampered checkpoint could carry a negative size, which
    # makes max(cx, cy) negative -- slipping past the zero-denominator guard in ncd.py and
    # yielding finite negative distances that every matrix validator accepts.
    sizes: tuple[Annotated[int, Field(gt=0)], ...]


class DistanceShardsCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    identity: dict[str, object]
    pair_count: int = Field(ge=0)
    shard_count: int = Field(ge=0)
    completed: tuple[int, ...]
    digests: dict[str, str]
    # Completed shards measured with the PPMd model check lifted. `allow_model_restart` is not
    # checkpoint identity, so a resume that applies the check recomputes these rather than
    # inheriting values it never checked [adr-0025].
    unchecked: tuple[int, ...] = ()

    @field_validator("digests")
    @classmethod
    def _digests_are_sha256(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
            for digest in value.values()
        ):
            raise ValueError("shard digests must be lowercase SHA-256 values")
        return value


class LabelsArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    object_ids: tuple[str, ...]
    labels: tuple[str, ...]
