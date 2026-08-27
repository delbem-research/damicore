import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pandas as pd
import pytest
from pydantic import ValidationError

import damicore_normalizer.api as api
import damicore_normalizer.delimited_reader as delimited_reader
import damicore_normalizer.table_split as table_split
from damicore_normalizer import (
    DelimitedSource,
    FileCorpusSource,
    NormalizationConfig,
    NormalizerError,
    ObjectDescriptor,
    materialize_objects,
    normalize_csv,
)
from damicore_normalizer.manifest import NormalizationManifest
from damicore_normalizer.scan import ScanResult

pytestmark = pytest.mark.unit


# The two input blocks the encoding rule discriminates on, kept minimal: only `kind` and the
# fields each variant requires, since the rule reads nothing else.
_MANIFEST_INPUTS: dict[str, dict[str, object]] = {
    "delimited": {
        "kind": "delimited",
        "path": "/tmp/dataset.csv",
        "sha256": "b" * 64,
        "size_bytes": 10,
        "delimiter": ",",
        "encoding": "utf-8",
        "split": "columns",
    },
    "files": {
        "kind": "files",
        "root": "/tmp/corpus",
        "sha256": "c" * 64,
        "size_bytes": 10,
        "file_count": 2,
        "recursive": True,
        "include_hidden": False,
    },
}


def _split(value: str) -> Literal["columns", "rows"]:
    """Narrow a parametrized string to the literal the config contract declares."""
    return "rows" if value == "rows" else "columns"


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "input.csv"
    path.write_text('name,note\nAna,"a,b"\nBia,""\n', encoding="utf-8")
    return path


def test_columns_are_canonical_and_chunk_independent(tmp_path: Path) -> None:
    source = _csv(tmp_path)
    first = normalize_csv(
        source,
        tmp_path / "one",
        config=NormalizationConfig(chunk_rows=1),
    )
    second = normalize_csv(
        source,
        tmp_path / "two",
        config=NormalizationConfig(chunk_rows=50),
    )
    assert second.total_bytes == first.total_bytes

    expected = [b'"Ana"\n"Bia"\n', b'"a,b"\n""\n']
    assert first.object_count == 2
    for index, payload in enumerate(expected, 1):
        left = tmp_path / "one" / "objects" / f"column_{index:06d}.jsonl"
        right = tmp_path / "two" / "objects" / f"column_{index:06d}.jsonl"
        assert left.read_bytes() == right.read_bytes() == payload
        assert hashlib.sha256(payload).hexdigest() == first.objects[index - 1].sha256
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["input"]["size_bytes"] == source.stat().st_size


def test_rows_use_positional_ids_and_arrays(tmp_path: Path) -> None:
    result = normalize_csv(
        _csv(tmp_path),
        tmp_path / "rows",
        config=NormalizationConfig(source=DelimitedSource(split="rows"), chunk_rows=1),
    )
    assert [item.object_id for item in result.objects] == ["row_000001", "row_000002"]
    assert (tmp_path / "rows/objects/row_000001.jsonl").read_bytes() == b'["Ana","a,b"]\n'


# Each row is one way the input contract can be violated: the CSV text (None for a path that
# is not a file), the config overrides that make it a violation, the stable public code,
# and the message fragment separating it from the other violations
# that share that code. Adding a violation is adding a row, and it fails under its own name.
INPUT_CONTRACT_VIOLATIONS = [
    pytest.param(
        "a,a\n1,2\n", "columns", "dataset_format_error", "unique", id="duplicate-header-names"
    ),
    pytest.param(
        ",b\n1,2\n", "columns", "dataset_format_error", "non-empty", id="empty-header-name"
    ),
    pytest.param("a,b\n", "columns", "dataset_format_error", "enough data rows", id="no-data-rows"),
    pytest.param(
        "a\n1\n2\n",
        "columns",
        "dataset_format_error",
        "two columns",
        id="one-column-columns-split",
    ),
    pytest.param(
        "a,b\n1,2\n",
        "rows",
        "dataset_format_error",
        "enough data rows",
        id="one-row-rows-split",
    ),
    pytest.param(None, "columns", "input_validation_error", "regular file", id="missing-file"),
]


@pytest.mark.parametrize(("text", "split", "code", "discriminator"), INPUT_CONTRACT_VIOLATIONS)
def test_input_contract_violation_reports_its_code_and_cause(
    tmp_path: Path,
    text: str | None,
    split: str,
    code: str,
    discriminator: str,
) -> None:
    source = tmp_path / "input.csv"
    if text is not None:
        source.write_text(text, encoding="utf-8")
    with pytest.raises(NormalizerError, match=discriminator) as raised:
        normalize_csv(
            source,
            tmp_path / "out",
            config=NormalizationConfig(source=DelimitedSource(split=_split(split))),
        )
    assert raised.value.code == code


def _no_paths(tmp_path: Path) -> None:
    materialize_objects([], tmp_path / "out")


def _two_paths_for_one_dataset(tmp_path: Path) -> None:
    for name in ("one.csv", "two.csv"):
        (tmp_path / name).write_text("a,b\n1,2\n2,3\n", encoding="utf-8")
    materialize_objects([tmp_path / "one.csv", tmp_path / "two.csv"], tmp_path / "out")


def _a_bytes_path(tmp_path: Path) -> None:
    # bytes satisfies Sequence, so without a check each byte is taken for a path of its own.
    materialize_objects(b"/tmp/dataset.csv", tmp_path / "out")  # pyright: ignore[reportArgumentType]


def _a_corpus_through_the_delimited_wrapper(tmp_path: Path) -> None:
    normalize_csv(
        _csv(tmp_path),
        tmp_path / "out",
        config=NormalizationConfig(source=FileCorpusSource()),
    )


# The refusals the two public entry points document but no other row reaches: what a caller
# gets for naming no input at all, for handing a dataset source several files -- the mistake
# the files source invites -- and for asking the 0.1-compatible wrapper to do what only
# materialize_objects does. Each is a message a user is meant to act on, so each fails here
# under its own name rather than being reachable only by reading the source.
@pytest.mark.parametrize(
    ("call", "discriminator"),
    [
        pytest.param(_no_paths, "No input path was given", id="no-paths-at-all"),
        pytest.param(
            _two_paths_for_one_dataset, "takes exactly one file", id="several-paths-one-dataset"
        ),
        pytest.param(_a_bytes_path, "must be a string or a path", id="bytes-instead-of-a-path"),
        pytest.param(
            _a_corpus_through_the_delimited_wrapper,
            "only accepts a delimited source",
            id="corpus-through-normalize-csv",
        ),
    ],
)
def test_a_documented_entry_point_refusal_reports_its_code_and_cause(
    tmp_path: Path, call: Callable[[Path], None], discriminator: str
) -> None:
    with pytest.raises(NormalizerError, match=discriminator) as raised:
        call(tmp_path)
    assert raised.value.code == "input_validation_error"


def test_an_input_that_disappeared_during_normalization_is_reported_as_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complement of the mutation case below: there the two stats disagree, here the second
    one cannot be taken at all. Both mean the manifest would describe bytes the run did not
    read, so both are drift rather than a bare OSError escaping the public API."""
    source = _csv(tmp_path)
    real_scan = api.scan_source

    def vanishing_scan(
        path: str | Path, config: NormalizationConfig, *, objects_dir: Path
    ) -> ScanResult:
        result = real_scan(path, config, objects_dir=objects_dir)
        source.unlink()
        return result

    monkeypatch.setattr(api, "scan_source", vanishing_scan)
    with pytest.raises(NormalizerError, match="disappeared during normalization") as raised:
        normalize_csv(source, tmp_path / "out")
    assert raised.value.code == "input_drift"


def test_scanning_rejects_a_missing_path_on_its_own(tmp_path: Path) -> None:
    """scan_source reads from the filesystem, so it is a file boundary in its own right
    (AGENTS.md: validate at file boundaries) and must reject a missing path even when called
    directly, not only through materialize_objects' own pre-check."""
    with pytest.raises(NormalizerError) as raised:
        api.scan_source(tmp_path / "missing.csv", NormalizationConfig())
    assert raised.value.code == "input_validation_error"


def test_a_declared_delimiter_and_encoding_are_used_verbatim(tmp_path: Path) -> None:
    """The declared delimiter and encoding decode the
    input, and the canonical object bytes are always UTF-8 JSON regardless of that encoding."""
    source = tmp_path / "latin.csv"
    source.write_bytes("nome;cidade\nJosé;Belém\n".encode("latin-1"))
    result = normalize_csv(
        source,
        tmp_path / "out",
        config=NormalizationConfig(source=DelimitedSource(delimiter=";", encoding="latin-1")),
    )
    assert [item.label for item in result.objects] == ["nome", "cidade"]
    assert (tmp_path / "out/objects/column_000001.jsonl").read_bytes() == b'"Jos\xc3\xa9"\n'
    assert (tmp_path / "out/objects/column_000002.jsonl").read_bytes() == b'"Bel\xc3\xa9m"\n'


def test_cell_text_is_preserved_and_escaped_only_by_json(tmp_path: Path) -> None:
    """An embedded newline, quote or non-ASCII character survives
    unchanged; only the JSON representation supplies escaping."""
    source = tmp_path / "quoted.csv"
    source.write_text('text,other\n"line1\nline2","say ""hi"" ☃"\n', encoding="utf-8")
    normalize_csv(source, tmp_path / "out")
    assert (tmp_path / "out/objects/column_000001.jsonl").read_bytes() == b'"line1\\nline2"\n'
    assert (
        tmp_path / "out/objects/column_000002.jsonl"
    ).read_bytes() == b'"say \\"hi\\" \xe2\x98\x83"\n'


# Each row is a byte-level defect the parser must reject rather than silently repair: a row
# whose field count disagrees with the header, and undecodable bytes in the header and in a
# later chunk, which take different code paths.
# The discriminator names the line and the counts, so a width row cannot silently start
# passing through pandas' own on_bad_lines translation, which raises the same code from a
# different site. Without it, deleting _validate_record_widths outright left these passing.
MALFORMED_INPUTS = [
    pytest.param(b"a,b\n1,2,3\n", "Line 2 has 3 fields", id="every-row-wider-than-header"),
    pytest.param(b"a,b\n1,2,3,4\n", "Line 2 has 4 fields", id="two-fields-wider-than-header"),
    pytest.param(b"a,b\n1,2,3\n4,5\n", "Line 2 has 3 fields", id="first-row-wider-than-header"),
    pytest.param(b"a,b\n1,2\n3,4,5\n", "Line 3 has 3 fields", id="later-row-wider-than-header"),
    pytest.param(b"a,b,c\n1,2\n", "Line 2 has 2 fields", id="row-narrower-than-header"),
    pytest.param(
        b"a,b,c\n1,2,3\n4,5\n", "Line 3 has 2 fields", id="later-row-narrower-than-header"
    ),
    pytest.param(
        b"a,\xffb\n1,2\n", "Could not read a valid delimited header", id="undecodable-header"
    ),
    pytest.param(
        b"a,b\n1,2\n\xff,4\n", "Could not read a valid delimited header", id="undecodable-data-row"
    ),
]


# Every chunk size must reach the same verdict. pandas resolves a field-count mismatch
# differently depending on where the chunk boundary falls, so a rule checked only through
# pandas would accept an input at one chunk size and reject it at another.
@pytest.mark.parametrize("chunk_rows", [1, 2, 50])
@pytest.mark.parametrize(("payload", "discriminator"), MALFORMED_INPUTS)
def test_malformed_input_is_rejected_as_a_dataset_format_error(
    tmp_path: Path, payload: bytes, discriminator: str, chunk_rows: int
) -> None:
    """A record whose field count disagrees with the header is
    malformed. Accepting one would silently drop or invent cell values, because pandas reads a
    uniform surplus of leading fields as an index and pads a short row."""
    source = tmp_path / "malformed.csv"
    source.write_bytes(payload)
    output = tmp_path / "out"
    with pytest.raises(NormalizerError, match=discriminator) as raised:
        normalize_csv(source, output, config=NormalizationConfig(chunk_rows=chunk_rows))
    assert raised.value.code == "dataset_format_error"
    assert not (output / "manifest.json").exists()
    assert not (output / "objects").exists()


def test_a_field_wider_than_the_csv_module_default_round_trips(tmp_path: Path) -> None:
    """The CSV contract sets no field-size limit, and pandas imposes none. csv's own 131072-char
    default applies only to the validation passes, so it must not become an input restriction
    the normalizer invents: a wide cell is well-formed and its bytes must survive intact."""
    wide = "x" * 200_000
    source = tmp_path / "wide_field.csv"
    source.write_text(f"a,b\n1,{wide}\n", encoding="utf-8")
    result = normalize_csv(source, tmp_path / "out")
    assert result.object_count == 2
    assert (tmp_path / "out/objects/column_000002.jsonl").read_bytes() == f'"{wide}"\n'.encode()


def test_a_utf8_bom_is_stripped_rather_than_read_as_part_of_the_first_header(
    tmp_path: Path,
) -> None:
    """pandas' C parser strips a leading BOM. The csv.reader validation passes must agree, or
    every spreadsheet-exported CSV is rejected with a header-changed error it cannot act on."""
    source = tmp_path / "bom.csv"
    source.write_bytes("name,note\nAna,x\nBia,y\n".encode("utf-8-sig"))
    result = normalize_csv(source, tmp_path / "out")
    assert [item.label for item in result.objects] == ["name", "note"]
    assert (tmp_path / "out/objects/column_000001.jsonl").read_bytes() == b'"Ana"\n"Bia"\n'


def test_a_header_change_mid_parse_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards an invariant pandas' chunked reader is not expected to break on its own: every
    chunk must report the same columns as the header _read_header already validated."""
    source = _csv(tmp_path)
    chunks = [
        pd.DataFrame({"name": ["Ana"], "note": ["a,b"]}),
        pd.DataFrame({"name": ["Bia"], "unexpected": [""]}),
    ]

    def fake_read_csv(*args: object, **kwargs: object) -> list[pd.DataFrame]:
        return chunks

    monkeypatch.setattr(delimited_reader.pd, "read_csv", fake_read_csv)
    with pytest.raises(NormalizerError, match="Header changed") as raised:
        normalize_csv(source, tmp_path / "out")
    assert raised.value.code == "dataset_format_error"


def test_a_pandas_parser_error_is_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pandas can reject a CSV that csv.reader's own width check tolerated; the translation
    at the bottom of scan_csv is the last line of defense before an unhandled traceback."""
    source = _csv(tmp_path)

    def failing_read_csv(*args: object, **kwargs: object) -> list[pd.DataFrame]:
        raise pd.errors.ParserError("boom")

    monkeypatch.setattr(delimited_reader.pd, "read_csv", failing_read_csv)
    with pytest.raises(NormalizerError) as raised:
        normalize_csv(source, tmp_path / "out")
    assert raised.value.code == "dataset_format_error"


def test_a_blank_line_is_a_full_width_empty_row(tmp_path: Path) -> None:
    """Reading sets skip_blank_lines=False, so a blank line is preserved as
    a row of empty cells rather than being rejected as a width mismatch or skipped."""
    source = tmp_path / "blank.csv"
    source.write_text("a,b\n1,2\n\n3,4\n", encoding="utf-8")
    result = normalize_csv(
        source,
        tmp_path / "out",
        config=NormalizationConfig(source=DelimitedSource(split="rows"), chunk_rows=1),
    )
    assert result.object_count == 3
    assert (tmp_path / "out/objects/row_000002.jsonl").read_bytes() == b'["",""]\n'


def test_more_columns_than_the_open_file_limit_stay_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LRU pool caps open handles, so a wide CSV forces
    eviction and reopening. Every object must still contain all of its rows, in order."""
    columns = 70
    limit = 8
    header = ",".join(f"c{index:03d}" for index in range(columns))
    rows = [",".join(f"r{row}c{index:03d}" for index in range(columns)) for row in range(3)]
    source = tmp_path / "wide.csv"
    source.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")

    peak_open_streams = 0
    write = table_split._FilePool.write

    def counting_write(pool: table_split._FilePool, name: str, payload: bytes) -> None:
        nonlocal peak_open_streams
        write(pool, name, payload)
        peak_open_streams = max(peak_open_streams, len(pool._streams))

    monkeypatch.setattr(table_split._FilePool, "write", counting_write)
    result = normalize_csv(
        source,
        tmp_path / "out",
        config=NormalizationConfig(chunk_rows=1, max_open_files=limit),
    )

    assert peak_open_streams == limit
    assert result.object_count == columns
    for index, item in enumerate(result.objects):
        payload = (tmp_path / "out" / item.relative_path).read_bytes()
        assert payload == "".join(f'"r{row}c{index:03d}"\n' for row in range(3)).encode("utf-8")
        assert hashlib.sha256(payload).hexdigest() == item.sha256
        assert len(payload) == item.size_bytes


def test_input_drift_during_normalization_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """input_drift is the one specialized code in v0.2 — it guards
    against silently normalizing a CSV that changed underneath the running scan."""
    source = _csv(tmp_path)
    real_scan_csv = api.scan_source

    def mutating_scan_csv(
        csv_path: str | Path, config: NormalizationConfig, *, objects_dir: Path
    ) -> ScanResult:
        result = real_scan_csv(csv_path, config, objects_dir=objects_dir)
        source.write_text('name,note\nAna,"a,b"\nBia,""\nCid,""\n', encoding="utf-8")
        return result

    monkeypatch.setattr(api, "scan_source", mutating_scan_csv)
    with pytest.raises(NormalizerError) as raised:
        normalize_csv(source, tmp_path / "out")
    assert raised.value.code == "input_drift"


def test_a_corrupted_written_object_fails_artifact_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-write hash/size re-check guards against a written object silently diverging
    from what the manifest will claim — corrupt one object and confirm it is caught."""
    source = _csv(tmp_path)
    real_scan_csv = api.scan_source

    def corrupting_scan_csv(
        csv_path: str | Path, config: NormalizationConfig, *, objects_dir: Path
    ) -> ScanResult:
        result = real_scan_csv(csv_path, config, objects_dir=objects_dir)
        first_object = objects_dir / result.objects[0].relative_path.removeprefix("objects/")
        first_object.write_bytes(b"corrupted\n")
        return result

    monkeypatch.setattr(api, "scan_source", corrupting_scan_csv)
    with pytest.raises(NormalizerError) as raised:
        normalize_csv(source, tmp_path / "out")
    assert raised.value.code == "artifact_validation_error"


def test_a_failed_manifest_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is written atomically through a temporary file, so a failure before the
    rename must not leave that partial file behind for a later run to trip over."""
    source = _csv(tmp_path)
    output = tmp_path / "out"

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated failure while committing the manifest")

    monkeypatch.setattr(api.os, "replace", failing_replace)
    with pytest.raises(OSError):
        normalize_csv(source, output)
    assert not (output / "manifest.json").exists()
    assert list(output.iterdir()) == [output / "objects"]


def test_output_must_be_empty_and_user_files_survive(tmp_path: Path) -> None:
    source = _csv(tmp_path)
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "user.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(NormalizerError) as raised:
        normalize_csv(source, output)
    assert raised.value.code == "output_conflict_error"
    assert (output / "user.txt").read_text(encoding="utf-8") == "preserve"


# Each row is one way a relative_path can escape the run directory. The manifest is read back
# through this model by the orchestrator (damicore/api.py deserializes manifest.json), so this
# validator is the gate a tampered or corrupted manifest has to pass.
UNCONTAINED_PATHS = [
    pytest.param("../evil.jsonl", id="parent-traversal"),
    pytest.param("objects/../../evil.jsonl", id="nested-traversal"),
    pytest.param("/etc/passwd", id="absolute-path"),
    pytest.param("objects//evil.jsonl", id="non-canonical-posix-form"),
]


@pytest.mark.parametrize("relative_path", UNCONTAINED_PATHS)
def test_an_uncontained_relative_path_is_rejected(relative_path: str) -> None:
    """Path containment: a manifest entry may only name a contained
    POSIX path, so a descriptor deserialized from disk can never point outside the run."""
    with pytest.raises(ValidationError, match="contained POSIX path"):
        ObjectDescriptor(
            object_id="column_000001",
            label="a",
            relative_path=relative_path,
            size_bytes=0,
            sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("delimiter", "encoding", "expected"),
    [
        pytest.param("::", "utf-8", ValueError, id="multi-character-delimiter"),
        pytest.param(",", "not-an-encoding", LookupError, id="unknown-encoding"),
    ],
)
def test_configuration_rejects_an_invalid_value(
    delimiter: str, encoding: str, expected: type[Exception]
) -> None:
    with pytest.raises(expected):
        DelimitedSource(delimiter=delimiter, encoding=encoding)


# The preflight in damicore calls scan_source without an objects_dir to size a run before
# creating anything. max_serialized_chunk_bytes feeds the memory estimate, so a wrong value
# there ships a wrong estimate silently.
@pytest.mark.parametrize(
    ("chunk_rows", "expected_max_chunk"),
    [
        pytest.param(1, 8, id="one-row-per-chunk"),
        pytest.param(50, 16, id="every-row-in-one-chunk"),
    ],
)
def test_scanning_without_an_objects_dir_measures_without_writing(
    tmp_path: Path, chunk_rows: int, expected_max_chunk: int
) -> None:
    source = tmp_path / "input.csv"
    source.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

    result = api.scan_source(source, NormalizationConfig(chunk_rows=chunk_rows))

    # Two columns of two cells; every cell serializes to `"x"\n`, four bytes.
    assert [item.object_id for item in result.objects] == ["column_000001", "column_000002"]
    assert result.total_bytes == 16
    assert result.max_serialized_chunk_bytes == expected_max_chunk
    assert list(tmp_path.iterdir()) == [source]


# The manifest is the inter-stage contract, and an NCD value is only meaningful relative to
# the bytes it measured, so a manifest that names the wrong encoding for its source
# misattributes every distance computed from it. Each row is one (source kind, encoding)
# pairing and whether the manifest may express it; the two rejected rows are the ones no
# other check can catch, because a mislabelled manifest is structurally valid otherwise.
@pytest.mark.parametrize(
    ("kind", "encoding", "accepted"),
    [
        pytest.param("delimited", "json-lines/1", True, id="delimited-json-lines"),
        pytest.param("files", "raw-bytes/1", True, id="files-raw-bytes"),
        pytest.param("delimited", "raw-bytes/1", False, id="delimited-must-not-claim-raw"),
        pytest.param("files", "json-lines/1", False, id="files-must-not-claim-json-lines"),
    ],
)
def test_a_manifest_may_only_name_the_encoding_its_source_produces(
    kind: str, encoding: str, accepted: bool
) -> None:
    payload = {
        "schema_version": 2,
        "object_encoding": encoding,
        "input": _MANIFEST_INPUTS[kind],
        "objects": [
            {
                "object_id": "object_000001",
                "label": "one",
                "relative_path": "objects/object_000001",
                "size_bytes": 3,
                "sha256": "a" * 64,
            }
        ],
    }
    # Parsed from JSON text, which is how both consumers read a manifest from disk.
    document = json.dumps(payload)
    if accepted:
        assert NormalizationManifest.model_validate_json(document).object_encoding == encoding
        return
    with pytest.raises(ValidationError, match="must carry object_encoding"):
        NormalizationManifest.model_validate_json(document)
