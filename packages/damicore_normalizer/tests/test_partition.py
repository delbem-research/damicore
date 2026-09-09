import csv
import inspect
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

import damicore_normalizer.api as api
from damicore_normalizer import (
    DelimitedSource,
    NormalizationConfig,
    NormalizerError,
    PartitionFile,
    PartitionManifest,
    PartitionResult,
    SpreadsheetSource,
    materialize_objects,
    partition_dataset,
)
from damicore_normalizer.manifest import DelimitedPartitionInput, SpreadsheetPartitionInput
from damicore_normalizer.numeric_column import scaled_integers
from damicore_normalizer.partition import (
    FileInterval,
    Ranking,
    rank_bounds,
    rank_column,
    route_rows,
)
from damicore_normalizer.spreadsheet_reader import CellValue

pytestmark = pytest.mark.unit

HEADER = ["id", "score", "text"]


def _csv(
    path: Path,
    rows: Sequence[Sequence[str]],
    *,
    header: Sequence[str] = HEADER,
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> Path:
    with path.open("w", encoding=encoding, newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _workbook(path: Path, rows: Sequence[Sequence[CellValue]], *, title: str = "Sheet") -> Path:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = title
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


def _emitted_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))[1:]


def _ranks(scores: Sequence[str]) -> list[int]:
    """The specification's order, computed independently: value descending, position ascending."""
    order = sorted(range(len(scores)), key=lambda index: Decimal(scores[index]), reverse=True)
    ranks = [0] * len(scores)
    for position, index in enumerate(order):
        ranks[index] = position
    return ranks


def _scored_rows(scores: Sequence[str]) -> list[list[str]]:
    return [[f"r{index}", score, f"t{index}"] for index, score in enumerate(scores)]


def test_quantile_files_hold_the_selected_rows_in_input_order(tmp_path: Path) -> None:
    scores = ["5", "1", "9", "3", "7", "2", "8", "4", "6", "0"]
    source = _csv(tmp_path / "in.csv", _scored_rows(scores))
    result = partition_dataset(
        source, tmp_path / "out", column="score", quantiles=(2,), percentiles=(), jenks_classes=()
    )
    ranks = _ranks(scores)
    high = [row for row, rank in zip(_scored_rows(scores), ranks, strict=True) if rank < 5]
    low = [row for row, rank in zip(_scored_rows(scores), ranks, strict=True) if rank >= 5]
    assert _emitted_rows(tmp_path / "out" / "quantile_02_high.csv") == high
    assert _emitted_rows(tmp_path / "out" / "quantile_02_low.csv") == low
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [
        "partition.json",
        "quantile_02_high.csv",
        "quantile_02_low.csv",
    ]
    manifest = result.manifest
    assert result.manifest_path == tmp_path / "out" / "partition.json"
    assert manifest.row_count == 10
    assert manifest.decimal == "."
    assert manifest.column == "score"
    assert manifest.input.kind == "delimited"
    assert manifest.input.path == str(source.resolve())
    assert [(f.method, f.parameter, f.end, f.rank_start, f.rank_end) for f in manifest.files] == [
        ("quantile", 2, "high", 0, 5),
        ("quantile", 2, "low", 5, 10),
    ]
    assert [f.relative_path for f in manifest.files] == [
        "quantile_02_high.csv",
        "quantile_02_low.csv",
    ]
    assert [f.row_count for f in manifest.files] == [5, 5]
    on_disk = PartitionManifest.model_validate_json(result.manifest_path.read_bytes())
    assert on_disk == manifest
    assert result.manifest_path.read_bytes().endswith(b"}\n")


TRICKY = [
    ["a", "1.5", "plain"],
    ["b", "2", 'has "quote"'],
    ["c", "-3", "has,comma"],
    ["d", "4", "multi\nline"],
    ["e", "5", " padded "],
    ["f", "6", ""],
    ["g", "7", "cr\rin"],
    ["h", "8", "ünïcødé"],
]


@pytest.mark.parametrize("kind", ["delimited", "xlsx"])
def test_every_emitted_file_normalizes_to_the_selected_row_objects(
    tmp_path: Path, kind: str
) -> None:
    """The metamorphic oracle: the row objects of a subset equal the row objects of the
    selected rows of the whole, byte for byte, whatever the quoting or the input format."""
    if kind == "delimited":
        source = _csv(tmp_path / "in.csv", TRICKY, delimiter=";", encoding="latin-1")
        result = partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2, 4),
            percentiles=(),
            jenks_classes=(2,),
            delimiter=";",
            encoding="latin-1",
        )
    else:
        typed: list[list[CellValue]] = [
            [row[0], int(row[1]) if "." not in row[1] else float(row[1]), row[2]] for row in TRICKY
        ]
        source = _workbook(tmp_path / "in.xlsx", [list(HEADER), *typed])
        result = partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2, 4),
            percentiles=(),
            jenks_classes=(2,),
            source_kind="xlsx",
        )
    rows_config = NormalizationConfig(source=DelimitedSource(split="rows"))
    if kind == "delimited":
        whole_config = NormalizationConfig(
            source=DelimitedSource(split="rows", delimiter=";", encoding="latin-1")
        )
    else:
        whole_config = NormalizationConfig(source=SpreadsheetSource(split="rows"))
    whole = materialize_objects(source, tmp_path / "norm-whole", config=whole_config)
    whole_bytes = [(tmp_path / "norm-whole" / o.relative_path).read_bytes() for o in whole.objects]
    ranks = _ranks([row[1] for row in TRICKY])
    for item in result.manifest.files:
        subset = materialize_objects(
            tmp_path / "out" / item.relative_path,
            tmp_path / f"norm-{item.relative_path}",
            config=rows_config,
        )
        emitted = [
            (tmp_path / f"norm-{item.relative_path}" / o.relative_path).read_bytes()
            for o in subset.objects
        ]
        expected = [
            payload
            for payload, rank in zip(whole_bytes, ranks, strict=True)
            if item.rank_start <= rank < item.rank_end
        ]
        assert emitted == expected, item.relative_path


def test_the_default_call_partitions_a_column_with_five_distinct_values(tmp_path: Path) -> None:
    scores = [str(index % 5) for index in range(32)]
    source = _csv(tmp_path / "in.csv", _scored_rows(scores))
    result = partition_dataset(source, tmp_path / "out", column="score")
    files = result.manifest.files
    assert len(files) == 2 * (5 + 3 + 4)
    keys = [(f.method, f.parameter, f.end) for f in files]
    assert keys == sorted(
        keys, key=lambda k: (("quantile", "percentile", "jenks").index(k[0]), k[1], k[2] == "low")
    )
    by_key = {(f.method, f.parameter, f.end): f for f in files}
    for q in (2, 4, 8, 16, 32):
        assert by_key[("quantile", q, "high")].row_count == 32 // q
        assert by_key[("quantile", q, "low")].row_count == -(-32 // q)
    for q in (2, 4, 8, 16):
        assert (
            by_key[("quantile", 2 * q, "high")].rank_end <= by_key[("quantile", q, "high")].rank_end
        )
    assert by_key[("quantile", 2, "high")].rank_end == by_key[("quantile", 2, "low")].rank_start
    for p in (5, 10, 20):
        assert by_key[("percentile", p, "high")].row_count == p * 32 // 100
        assert by_key[("percentile", p, "low")].rank_start == (100 - p) * 32 // 100
    for k in (2, 3, 4, 5):
        assert by_key[("jenks", k, "high")].rank_end <= by_key[("jenks", k, "low")].rank_start
    for f in files:
        target = tmp_path / "out" / f.relative_path
        assert target.stat().st_size == f.size_bytes
        payload = target.read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf") and b"\r" not in payload
        assert payload.startswith(b"id,score,text\n")


def test_the_default_call_refuses_a_column_with_four_distinct_values(tmp_path: Path) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows([str(index % 4) for index in range(32)]))
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(source, tmp_path / "out", column="score")
    assert raised.value.code == "dataset_format_error"
    assert "jenks 5" in str(raised.value) and "4" in str(raised.value)
    assert "jenks_classes=()" in str(raised.value)
    assert not (tmp_path / "out").exists()


def test_ties_across_a_rank_cut_follow_file_position(tmp_path: Path) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "1", "1", "1"]))
    partition_dataset(
        source, tmp_path / "out", column="score", quantiles=(2,), percentiles=(), jenks_classes=()
    )
    assert [row[0] for row in _emitted_rows(tmp_path / "out" / "quantile_02_high.csv")] == [
        "r0",
        "r1",
    ]
    assert [row[0] for row in _emitted_rows(tmp_path / "out" / "quantile_02_low.csv")] == [
        "r2",
        "r3",
    ]


@pytest.mark.parametrize("row_count", [10, 11])
def test_percentile_fifty_is_quantile_two(tmp_path: Path, row_count: int) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows([str(i) for i in range(row_count)]))
    result = partition_dataset(
        source,
        tmp_path / "out",
        column="score",
        quantiles=(2,),
        percentiles=(50,),
        jenks_classes=(),
    )
    intervals = {(f.method, f.end): (f.rank_start, f.rank_end) for f in result.manifest.files}
    assert intervals[("percentile", "high")] == intervals[("quantile", "high")]
    assert intervals[("percentile", "low")] == intervals[("quantile", "low")]
    assert (tmp_path / "out" / "percentile_50_high.csv").read_bytes() == (
        tmp_path / "out" / "quantile_02_high.csv"
    ).read_bytes()


def test_runs_are_byte_identical_across_chunk_sizes_and_detection_modes(tmp_path: Path) -> None:
    scores = ["1,5", "2", "0,25", "7", "3", "3", "9", "4"]
    source = _csv(tmp_path / "in.csv", _scored_rows(scores), delimiter=";")
    outputs: list[dict[str, bytes]] = []
    for name, extra in (
        ("a", {}),
        ("b", {"chunk_rows": 1}),
        ("c", {"decimal": ","}),
    ):
        result = partition_dataset(
            source,
            tmp_path / name,
            column="score",
            quantiles=(2, 4),
            percentiles=(),
            jenks_classes=(2,),
            delimiter=";",
            **extra,  # type: ignore[arg-type]  # a heterogeneous keyword bag for the parametrization
        )
        assert result.manifest.decimal == ","
        outputs.append({p.name: p.read_bytes() for p in (tmp_path / name).iterdir()})
    assert outputs[0] == outputs[1] == outputs[2]


@pytest.mark.parametrize(
    ("scores", "expected_row_words"),
    [
        pytest.param(["1", "1.5", "2,5"], ["data row 2", "data row 3"], id="mixed"),
        pytest.param(["1.234,56", "2"], ["data row 1"], id="thousands-and-decimal"),
        pytest.param(["1", "", "2"], ["data row 2"], id="empty-cell"),
        pytest.param(["1", "1e1000000"], ["data row 2"], id="seven-digit-exponent"),
        pytest.param(["1", "1\n", "2"], ["data row 2"], id="embedded-newline"),
    ],
)
def test_a_non_numeric_target_cell_is_refused_naming_its_row(
    tmp_path: Path, scores: list[str], expected_row_words: list[str]
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(scores))
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2,),
            percentiles=(),
            jenks_classes=(),
        )
    assert raised.value.code == "dataset_format_error"
    for words in expected_row_words:
        assert words in str(raised.value)
    assert not (tmp_path / "out").exists()


def test_a_wrong_declared_separator_is_refused_at_the_first_failing_row(tmp_path: Path) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "2", "3.5", "4"]))
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2,),
            percentiles=(),
            jenks_classes=(),
            decimal=",",
        )
    assert raised.value.code == "dataset_format_error"
    assert "data row 3" in str(raised.value)


def test_padded_and_scientific_cells_partition_by_their_numeric_value(tmp_path: Path) -> None:
    scores = [" 1e2 ", "\t5", "+50", "-0.5e1", "1.0", "1"]
    source = _csv(tmp_path / "in.csv", _scored_rows(scores))
    result = partition_dataset(
        source,
        tmp_path / "out",
        column="score",
        quantiles=(2,),
        percentiles=(),
        jenks_classes=(3,),
        decimal=".",
    )
    assert [row[0] for row in _emitted_rows(tmp_path / "out" / "quantile_02_high.csv")] == [
        "r0",
        "r1",
        "r2",
    ]
    jenks_high = {(f.method, f.end): f for f in result.manifest.files}[("jenks", "high")]
    assert (jenks_high.rank_start, jenks_high.rank_end) == (0, 1)


def test_a_spreadsheet_column_of_typed_numbers_partitions_like_its_delimited_twin(
    tmp_path: Path,
) -> None:
    rows: list[list[CellValue]] = [["a", 3, "x"], ["b", 1.5, "y"], ["c", 2, "z"], ["d", 10, "w"]]
    source = _workbook(tmp_path / "in.xlsx", [list(HEADER), *rows], title="Data")
    result = partition_dataset(
        source,
        tmp_path / "out",
        column="score",
        quantiles=(2,),
        percentiles=(),
        jenks_classes=(),
        source_kind="xlsx",
    )
    assert isinstance(result.manifest.input, SpreadsheetPartitionInput)
    assert result.manifest.input.sheet == "Data"
    assert result.manifest.input.cell_text_rule == "v1"
    assert _emitted_rows(tmp_path / "out" / "quantile_02_high.csv") == [
        ["a", "3", "x"],
        ["d", "10", "w"],
    ]


def test_a_spreadsheet_mixing_typed_numbers_and_comma_text_is_refused(tmp_path: Path) -> None:
    rows: list[list[CellValue]] = [["a", 1.5, "x"], ["b", "2,5", "y"]]
    source = _workbook(tmp_path / "in.xlsx", [list(HEADER), *rows])
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2,),
            percentiles=(),
            jenks_classes=(),
            source_kind="xlsx",
        )
    assert raised.value.code == "dataset_format_error"
    assert "data row 1" in str(raised.value) and "data row 2" in str(raised.value)


def test_a_missing_column_lists_the_available_names_and_leaves_no_directory(
    tmp_path: Path,
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "2"]))
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(source, tmp_path / "out", column="fitness", quantiles=(2,))
    assert raised.value.code == "dataset_format_error"
    assert "id, score, text" in str(raised.value)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        pytest.param(
            {"quantiles": (), "percentiles": (), "jenks_classes": ()},
            "At least one",
            id="nothing-requested",
        ),
        pytest.param({"quantiles": {2, 4}}, "list or tuple", id="set"),
        pytest.param({"quantiles": (2, True)}, "bool", id="bool"),
        pytest.param({"quantiles": (2, 2.0)}, "float", id="float"),
        pytest.param({"quantiles": (1,)}, "at least 2", id="quantile-below-range"),
        pytest.param({"percentiles": (51,)}, "between 1 and 50", id="percentile-above-range"),
        pytest.param({"jenks_classes": (2, 2)}, "repeat", id="repeated"),
        pytest.param({"decimal": ";"}, "decimal must be", id="decimal"),
        pytest.param({"source_kind": "parquet"}, "source_kind", id="source-kind"),
        pytest.param({"delimiter": ";;"}, "delimiter", id="two-character-delimiter"),
        pytest.param({"encoding": "no-such-codec"}, "encoding", id="unknown-encoding"),
        pytest.param({"chunk_rows": 0}, "chunk_rows", id="chunk-rows"),
    ],
)
def test_an_invalid_argument_is_refused_before_the_input_is_opened(
    tmp_path: Path, arguments: dict[str, object], words: str
) -> None:
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(tmp_path / "absent.csv", tmp_path / "out", column="score", **arguments)  # type: ignore[arg-type]  # deliberately wrong shapes
    assert raised.value.code == "input_validation_error"
    assert words in str(raised.value)


def test_every_source_setting_is_threaded_and_rejected_where_it_does_not_apply(
    tmp_path: Path,
) -> None:
    """The applicability sets are derived from the source models so a field added to one
    cannot go unthreaded silently. That claim has two halves and this is the only check of
    the second: every derived setting must be a keyword of `partition_dataset`, and passing
    it with a non-default value to the source kind it does not apply to must be refused by
    name. A field added to a source model fails here twice, at the sample table and at the
    refusal, until it is threaded through the function."""
    settings: set[str] = set()
    for applicable in api._PARTITION_SETTINGS.values():
        settings |= applicable
    assert settings <= set(inspect.signature(partition_dataset).parameters)
    samples: dict[str, object] = {"delimiter": ";", "encoding": "latin-1", "sheet": "Data"}
    assert set(samples) == settings
    for name, value in samples.items():
        for kind, applicable in api._PARTITION_SETTINGS.items():
            if name in applicable:
                continue
            with pytest.raises(NormalizerError) as raised:
                partition_dataset(
                    tmp_path / "absent.csv",
                    tmp_path / "out",
                    column="score",
                    source_kind=kind,
                    **{name: value},  # type: ignore[arg-type]  # one keyword chosen by name
                )
            assert raised.value.code == "input_validation_error"
            assert f"{name} does not apply to a {kind} source" in str(raised.value)


def test_a_sequence_of_sources_and_a_missing_file_are_refused(tmp_path: Path) -> None:
    with pytest.raises(NormalizerError) as sequence:
        partition_dataset([tmp_path / "a.csv"], tmp_path / "out", column="score")  # type: ignore[arg-type]  # deliberately wrong shape
    assert sequence.value.code == "input_validation_error"
    with pytest.raises(NormalizerError) as missing:
        partition_dataset(tmp_path / "absent.csv", tmp_path / "out", column="score")
    assert missing.value.code == "input_validation_error"


def test_the_caller_order_of_parameters_is_irrelevant_and_lists_are_accepted(
    tmp_path: Path,
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows([str(i) for i in range(8)]))
    result = partition_dataset(
        source, tmp_path / "out", column="score", quantiles=[4, 2], percentiles=[], jenks_classes=[]
    )
    assert [(f.parameter, f.end) for f in result.manifest.files] == [
        (2, "high"),
        (2, "low"),
        (4, "high"),
        (4, "low"),
    ]


@pytest.mark.parametrize(
    ("quantiles", "percentiles", "words"),
    [
        pytest.param(
            (16,),
            (),
            ["quantile 16", "16 data rows", "10", "quantiles=()"],
            id="short-for-quantile",
        ),
        pytest.param(
            (),
            (5,),
            ["percentile 5", "20 data rows", "10", "percentiles=()"],
            id="short-for-percentile",
        ),
    ],
)
def test_a_column_too_short_for_a_method_is_refused_naming_both_numbers(
    tmp_path: Path,
    quantiles: tuple[int, ...],
    percentiles: tuple[int, ...],
    words: list[str],
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows([str(i) for i in range(10)]))
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=quantiles,
            percentiles=percentiles,
            jenks_classes=(),
        )
    assert raised.value.code == "dataset_format_error"
    for expected in words:
        assert expected in str(raised.value)
    assert not (tmp_path / "out").exists()


def test_a_wide_magnitude_span_is_refused_for_jenks_but_partitions_by_quantile(
    tmp_path: Path,
) -> None:
    scores = ["1e1000", "1", "2", "3"]
    source = _csv(tmp_path / "in.csv", _scored_rows(scores))
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(),
            percentiles=(),
            jenks_classes=(2,),
        )
    assert raised.value.code == "dataset_format_error"
    assert "1001" in str(raised.value) and "1000" in str(raised.value)
    assert not (tmp_path / "out").exists()
    result = partition_dataset(
        source, tmp_path / "out", column="score", quantiles=(2,), percentiles=(), jenks_classes=()
    )
    assert [row[0] for row in _emitted_rows(tmp_path / "out" / "quantile_02_high.csv")] == [
        "r0",
        "r3",
    ]
    assert result.manifest.row_count == 4


def test_a_non_empty_output_directory_is_refused_and_untouched(tmp_path: Path) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "2"]))
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source, output, column="score", quantiles=(2,), percentiles=(), jenks_classes=()
        )
    assert raised.value.code == "output_conflict_error"
    assert (output / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert list(output.iterdir()) == [output / "keep.txt"]


def test_input_drift_between_passes_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "2", "3", "4"]))
    real_rank_column = api.rank_column

    def mutating_rank_column(*args: object, **kwargs: object) -> Ranking:
        result = real_rank_column(*args, **kwargs)  # type: ignore[arg-type]  # pass-through of the real call
        _csv(source, _scored_rows(["1", "2", "3", "4", "5"]))
        return result

    monkeypatch.setattr(api, "rank_column", mutating_rank_column)
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2,),
            percentiles=(),
            jenks_classes=(),
        )
    assert raised.value.code == "input_drift"
    assert "changed during partition" in str(raised.value)
    assert not (tmp_path / "out").exists()


def test_a_corrupted_emitted_file_fails_artifact_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "2", "3", "4"]))
    real_route_rows = api.route_rows

    def corrupting_route_rows(
        rows: object,
        header: object,
        rank: object,
        intervals: Sequence[FileInterval],
        output_dir: Path,
    ) -> tuple[tuple[int, str], ...]:
        written = real_route_rows(rows, header, rank, intervals, output_dir)  # type: ignore[arg-type]  # pass-through of the real call
        with (output_dir / "quantile_02_high.csv").open("ab") as stream:
            stream.write(b"extra\n")
        return written

    monkeypatch.setattr(api, "route_rows", corrupting_route_rows)
    with pytest.raises(NormalizerError) as raised:
        partition_dataset(
            source,
            tmp_path / "out",
            column="score",
            quantiles=(2,),
            percentiles=(),
            jenks_classes=(),
        )
    assert raised.value.code == "artifact_validation_error"
    assert "quantile_02_high.csv" in str(raised.value)
    assert not (tmp_path / "out" / "partition.json").exists()


def test_a_failed_manifest_write_leaves_the_files_but_no_manifest_or_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _csv(tmp_path / "in.csv", _scored_rows(["1", "2", "3", "4"]))
    output = tmp_path / "out"

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated failure while committing the manifest")

    monkeypatch.setattr(api.os, "replace", failing_replace)
    with pytest.raises(OSError):
        partition_dataset(
            source, output, column="score", quantiles=(2,), percentiles=(), jenks_classes=()
        )
    assert sorted(path.name for path in output.iterdir()) == [
        "quantile_02_high.csv",
        "quantile_02_low.csv",
    ]


def test_the_manifest_schema_pins_its_fields_and_the_chunk_default_matches_the_config() -> None:
    assert list(PartitionManifest.model_fields) == [
        "schema_version",
        "partition_rule",
        "input",
        "column",
        "decimal",
        "row_count",
        "files",
    ]
    assert list(PartitionFile.model_fields) == [
        "method",
        "parameter",
        "end",
        "rank_start",
        "rank_end",
        "size_bytes",
        "sha256",
    ]
    assert list(DelimitedPartitionInput.model_fields) == [
        "kind",
        "path",
        "sha256",
        "size_bytes",
        "delimiter",
        "encoding",
    ]
    assert list(SpreadsheetPartitionInput.model_fields) == [
        "kind",
        "path",
        "sha256",
        "size_bytes",
        "sheet",
        "cell_text_rule",
    ]
    assert list(PartitionResult.model_fields) == ["manifest_path", "manifest"]
    signature = inspect.signature(partition_dataset)
    assert signature.parameters["chunk_rows"].default == NormalizationConfig().chunk_rows
    assert signature.parameters["delimiter"].default == DelimitedSource().delimiter
    assert signature.parameters["encoding"].default == DelimitedSource().encoding


def _file_entry(method: str, parameter: int, end: str, start: int, stop: int) -> dict[str, object]:
    return {
        "method": method,
        "parameter": parameter,
        "end": end,
        "rank_start": start,
        "rank_end": stop,
        "size_bytes": 10,
        "sha256": "a" * 64,
    }


def _valid_files() -> list[dict[str, object]]:
    return [
        _file_entry("quantile", 2, "high", 0, 5),
        _file_entry("quantile", 2, "low", 5, 10),
        _file_entry("jenks", 3, "high", 0, 2),
        _file_entry("jenks", 3, "low", 7, 10),
    ]


def _valid_manifest(files: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "partition_rule": "v1",
        "input": {
            "kind": "delimited",
            "path": "/tmp/in.csv",
            "sha256": "b" * 64,
            "size_bytes": 20,
            "delimiter": ",",
            "encoding": "utf-8",
        },
        "column": "score",
        "decimal": ".",
        "row_count": 10,
        "files": _valid_files() if files is None else files,
    }


def _mutated(**changes: object) -> dict[str, object]:
    manifest = _valid_manifest()
    manifest.update(changes)
    return manifest


def _with_file(index: int, **changes: object) -> dict[str, object]:
    files = _valid_files()
    files[index] = {**files[index], **changes}
    return _valid_manifest(files)


def _without_file(index: int) -> dict[str, object]:
    files = _valid_files()
    del files[index]
    return _valid_manifest(files)


def _swapped() -> dict[str, object]:
    files = _valid_files()
    return _valid_manifest([files[2], files[3], files[0], files[1]])


def _load(payload: dict[str, object]) -> PartitionManifest:
    # The artifact is JSON, and JSON has no tuples, so a manifest is read the way every
    # artifact in this workspace is read: through the JSON validator, which is what turns a
    # JSON array into the tuple the strict schema declares.
    return PartitionManifest.model_validate_json(json.dumps(payload))


def test_the_valid_manifest_fixture_validates() -> None:
    _load(_valid_manifest())


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_with_file(1, rank_end=11), id="past-the-last-rank"),
        pytest.param(_with_file(0, rank_start=1), id="high-not-from-zero"),
        pytest.param(_with_file(1, rank_end=9), id="low-not-to-the-end"),
        pytest.param(_with_file(0, rank_end=0), id="empty-interval"),
        pytest.param(_with_file(0, parameter=1), id="quantile-parameter-one"),
        pytest.param(_with_file(2, parameter=1), id="jenks-parameter-one"),
        pytest.param(_without_file(1), id="unpaired"),
        pytest.param(_swapped(), id="out-of-order"),
        pytest.param(_mutated(row_count=1), id="one-row"),
        pytest.param(_mutated(partition_rule="v2"), id="unknown-rule"),
        pytest.param(_mutated(extra="field"), id="extra-field"),
        pytest.param(
            _with_file(0, relative_path="quantile_02_high.csv"), id="persisted-derived-field"
        ),
    ],
)
def test_a_manifest_violating_a_validator_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _load(payload)


def test_a_percentile_manifest_entry_accepts_fifty_and_refuses_fifty_one() -> None:
    files = _valid_files()
    _load(
        _valid_manifest(
            [
                {**files[0], "method": "percentile", "parameter": 50},
                {**files[1], "method": "percentile", "parameter": 50},
            ]
        )
    )
    with pytest.raises(ValidationError):
        PartitionFile.model_validate({**files[0], "method": "percentile", "parameter": 51})


def test_the_pure_helpers_refuse_what_the_readers_make_unreachable(tmp_path: Path) -> None:
    """Defensive branches the readers cannot reach today, kept as the modules' own contract
    and therefore exercised directly rather than left to inference."""
    assert rank_bounds("percentile", 34, 3) == (0, 1, 1, 3)
    with pytest.raises(NormalizerError) as short:
        rank_column(["1", "2"], ".", 3)
    assert short.value.code == "input_drift"
    with pytest.raises(NormalizerError) as nonfinite:
        scaled_integers([Decimal("NaN")])
    assert nonfinite.value.code == "dataset_format_error"
    ranking = rank_column(["3", "1", "2"], ".", 3)
    interval = FileInterval("quantile", 2, "high", 0, 1)
    with pytest.raises(NormalizerError) as longer:
        route_rows([["a"], ["b"], ["c"], ["d"]], ["h"], ranking.rank, [interval], tmp_path)
    assert longer.value.code == "input_drift"
    with pytest.raises(NormalizerError) as shorter:
        route_rows([["a"], ["b"]], ["h"], ranking.rank, [interval], tmp_path)
    assert shorter.value.code == "input_drift"
