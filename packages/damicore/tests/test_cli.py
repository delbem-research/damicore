import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import pytest

from damicore import DamicoreResult
from damicore.cli import _parser, main

pytestmark = pytest.mark.unit


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "input.csv"
    path.write_text("a,b\naa,ab\naa,ac\n", encoding="utf-8")
    return path


def test_cli_estimate_json_has_clean_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["estimate", str(_csv(tmp_path)), "--workers", "1", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["object_count"] == 2


def test_cli_passes_the_compressor_settings_to_estimate_and_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _csv(tmp_path)
    settings = ["--compressor", "ppmd", "--ppmd-order", "8", "--ppmd-memory-bytes", "1048576"]
    assert main(["estimate", str(source), "--workers", "2", "--json", *settings]) == 0
    deflate = ["estimate", str(source), "--workers", "2", "--json", "--compressor", "zlib"]
    estimated = json.loads(capsys.readouterr().out)["estimated_working_memory_bytes"]
    assert main(deflate) == 0
    # Two workers, each charged its stream: the 1 MiB model against DEFLATE's 256 KiB state.
    assert estimated - json.loads(capsys.readouterr().out)["estimated_working_memory_bytes"] == (
        2 * (1_048_576 - 262_144)
    )
    output = tmp_path / "run"
    run_arguments = ["run", str(source), "--workers", "1", "--no-progress", *settings]
    assert main([*run_arguments, "--output-dir", str(output)]) == 0
    config = json.loads((output / "manifest.json").read_text(encoding="utf-8"))["config"]
    assert (config["ppmd_order"], config["ppmd_memory_bytes"]) == (8, 1_048_576)
    capsys.readouterr()
    assert main(["run", str(source), "--compression-level", "9", "--no-progress"]) == 2
    assert "compression_level does not apply to ppmd" in capsys.readouterr().err
    lzma = ["--compressor", "lzma", "--lzma-dictionary-bytes", "1048576"]
    assert main(["estimate", str(source), "--workers", "2", "--json", *lzma]) == 0
    lzma_estimate = json.loads(capsys.readouterr().out)["estimated_working_memory_bytes"]
    assert main(deflate) == 0
    deflate_estimate = json.loads(capsys.readouterr().out)["estimated_working_memory_bytes"]
    assert lzma_estimate - deflate_estimate == 2 * (12 * 1_048_576 + 4_194_304 - 262_144)
    bzip2 = ["--compressor", "bzip2", "--bzip2-block-size-100k", "3"]
    bzip2_output = tmp_path / "bzip2"
    assert main([*run_arguments[:5], *bzip2, "--output-dir", str(bzip2_output)]) == 0
    config = json.loads((bzip2_output / "manifest.json").read_text(encoding="utf-8"))["config"]
    assert (config["compressor"], config["bzip2_block_size_100k"]) == ("bzip2", 3)


def test_cli_run_and_typed_error_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _csv(tmp_path)
    output = tmp_path / "run"
    assert (
        main(
            [
                "run",
                str(source),
                "--workers",
                "1",
                "--output-dir",
                str(output),
                "--no-progress",
            ]
        )
        == 0
    )
    # Success prints the artifact paths, not only the run directory.
    reported = dict(
        line.split(": ", 1) for line in capsys.readouterr().err.splitlines() if ": " in line
    )
    assert reported["run_dir"] == str(output)
    for name in ("manifest", "report", "distance_matrix", "labels", "tree_json", "membership"):
        assert Path(reported[name]).is_file()
    # keep_normalized and save_diagnostics were not requested, so those directories are absent
    # from the run and must not be reported as paths that a caller could open.
    assert "normalization_dir" not in reported
    assert "diagnostics_dir" not in reported
    assert main(["estimate", str(source), "--max-objects", "1", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "run",
                str(source),
                "--max-objects",
                "1",
                "--output-dir",
                str(tmp_path / "limited"),
                "--no-progress",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().err)["code"] == "resource_limit_error"


def test_cli_envelope_reports_the_public_code_of_a_translated_stage_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The envelope carries the public code, not the stage's internal one, and the exit
    status follows the translated class."""
    source = tmp_path / "duplicate.csv"
    source.write_text("a,a\n1,2\n", encoding="utf-8")
    assert main(["run", str(source), "--no-progress", "--output-dir", str(tmp_path / "out")]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "dataset_format_error"


def test_cli_interrupt_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Accepts any call shape: whether the CLI passes the CSV positionally or by keyword is
    # not part of what this test asserts.
    def interrupted(*_args: object, **_kwargs: object) -> DamicoreResult:
        raise KeyboardInterrupt

    monkeypatch.setattr("damicore.cli.run", interrupted)
    assert main(["run", str(_csv(tmp_path)), "--no-progress"]) == 130


def test_estimate_without_json_keeps_stdout_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --json the preview goes to stderr, so a caller piping stdout gets nothing to
    misparse. The JSON form is covered separately."""
    source = tmp_path / "input.csv"
    source.write_text("a,b\naaaa,bbbb\ncccc,dddd\n", encoding="utf-8")
    assert main(["estimate", str(source)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["object_count"] == 2


def test_every_argument_carries_help_text() -> None:
    """`--help` is the whole documentation of the command line, and the release smoke runs it
    as a gate: without this, that gate proves the parser builds, not that it explains anything.

    argparse exposes no public way to walk its arguments, so this reads `_actions`. The private
    attribute is confined to this test, where a rename surfaces as a failed test rather than as
    a silently unchecked contract.
    """
    # argparse's private attributes are untyped in the shipped stubs; one cast isolates that
    # boundary here rather than letting Unknown leak into the assertions. The subcommand action
    # is the only one whose `choices` is a mapping of parsers, so it needs no private class.
    root = cast(Any, _parser())
    commands: dict[str, Any] = {}
    for action in root._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            commands.update(cast(dict[str, Any], choices))
    assert set(commands) == {"estimate", "run"}

    bare: list[str] = []
    for name, command in commands.items():
        assert command.description, name
        for action in command._actions:
            if action.dest in {"help", "version"}:
                continue
            if not action.help:
                bare.append(f"{name} {action.dest}")
    assert bare == []


def test_version_reports_the_installed_distribution(capsys: pytest.CaptureFixture[str]) -> None:
    """`--version` must agree with what pip resolved, which is why it is read from the
    distribution metadata rather than restated in the source."""
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"damicore {metadata.version('damicore')}"


def test_a_closed_output_pipe_exits_without_a_traceback(tmp_path: Path) -> None:
    """`damicore estimate --json input.csv | head` closes stdout as soon as it has enough. The
    reader is closed here before any write, so the failure is deterministic rather than a race
    against the pipe buffer.
    """
    source = _csv(tmp_path)
    read_end, write_end = os.pipe()
    process = subprocess.Popen(
        [sys.executable, "-m", "damicore.cli", "estimate", "--json", str(source)],
        stdout=write_end,
        stderr=subprocess.PIPE,
    )
    os.close(write_end)
    os.close(read_end)
    _, captured = process.communicate(timeout=120)
    assert process.returncode == 141
    assert b"Traceback" not in captured
    assert b"Exception ignored" not in captured


def test_a_write_to_a_closed_pipe_returns_the_sigpipe_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subprocess test above proves the status a shell observes. This one exercises the
    handler in process, which is possible only because it does no descriptor surgery: the
    minimal form turned out to be enough, and it is also the testable one."""

    class ClosedPipe:
        def write(self, _text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            return None

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert main(["estimate", str(_csv(tmp_path)), "--json"]) == 141
