from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from damicore import ExecutionConfig, ResourceLimits, estimate, run
from damicore.api import VERSION
from damicore.errors import (
    ArtifactValidationError,
    CheckpointMismatchError,
    ConfigurationError,
    DamicoreError,
    InputValidationError,
    OutputDirectoryConflictError,
    ResourceLimitError,
)
from damicore.result import DamicoreResult

# The CLI exposes four of the five resource limits as flags. Their values are
# read from the model rather than restated here: ResourceLimits owns them, so raising a limit
# there cannot leave the CLI clamped at the old one while `damicore.run()` honours the new one.
# The model is frozen, so one shared instance is safe to read from.
_DEFAULT_LIMITS = ResourceLimits()


_DESCRIPTION = (
    "Cluster local files, or the rows or columns of a local dataset, by canonical "
    "serialization, exact Normalized Compression Distance, deterministic Neighbor Joining, "
    "and FastGreedy communities."
)
_ESTIMATE_DESCRIPTION = (
    "Report the exact cost of a run -- objects, pairs, matrix bytes, working memory, free "
    "disk -- without creating any artifact. Run this before raising a limit."
)
_RUN_DESCRIPTION = (
    "Execute the pipeline and write a verified run directory. An interrupted run resumes "
    "from its checkpoints to the same bytes a fresh run would have produced."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="damicore", description=_DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"damicore {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("estimate", _ESTIMATE_DESCRIPTION),
        ("run", _RUN_DESCRIPTION),
    ):
        command = commands.add_parser(name, description=description, help=description)
        command.add_argument(
            "source",
            nargs="+",
            help=(
                "dataset file to split, or the files and directories to cluster directly "
                "when --source is files"
            ),
        )
        command.add_argument(
            "--source",
            dest="source_kind",
            choices=("delimited", "xlsx", "files"),
            default="delimited",
            help=(
                "where objects come from: split a delimited text file, split a worksheet, or "
                "adopt files that already are the objects (default: delimited)"
            ),
        )
        command.add_argument(
            "--split",
            choices=("columns", "rows"),
            help=(
                "whether each column or each row becomes one object (default: columns); "
                "does not apply to --source files"
            ),
        )
        command.add_argument(
            "--delimiter",
            help="field delimiter of a delimited source (default: comma)",
        )
        command.add_argument(
            "--encoding",
            help="text encoding of a delimited source (default: utf-8)",
        )
        command.add_argument(
            "--sheet",
            help="worksheet to read from an xlsx source; required when it holds more than one",
        )
        command.add_argument(
            "--no-recursive",
            dest="recursive",
            action="store_const",
            const=False,
            help="do not descend into subdirectories of a files source",
        )
        command.add_argument(
            "--include-hidden",
            dest="include_hidden",
            action="store_const",
            const=True,
            help="include dot-prefixed files and directories in a files source",
        )
        # No `__main__` guard note here: the console script this help belongs to already
        # carries one, so the guard is a constraint on calling damicore.run() from a script,
        # never on the command line. Stating it here taught a caveat that cannot apply.
        command.add_argument(
            "--workers",
            type=int,
            help="worker processes for the distance stage; omit to choose automatically",
        )
        command.add_argument(
            "--max-objects",
            type=int,
            default=_DEFAULT_LIMITS.max_objects,
            help=f"reject more objects than this (default: {_DEFAULT_LIMITS.max_objects})",
        )
        command.add_argument(
            "--max-pairs",
            type=int,
            default=_DEFAULT_LIMITS.max_pairs,
            help=f"reject more object pairs than this (default: {_DEFAULT_LIMITS.max_pairs})",
        )
        command.add_argument(
            "--max-matrix-bytes",
            type=int,
            default=_DEFAULT_LIMITS.max_matrix_bytes,
            help=(
                "reject a distance matrix larger than this many bytes "
                f"(default: {_DEFAULT_LIMITS.max_matrix_bytes})"
            ),
        )
        command.add_argument(
            "--max-working-memory-bytes",
            type=int,
            default=_DEFAULT_LIMITS.max_working_memory_bytes,
            help=(
                "reject a run whose estimated working memory exceeds this many bytes "
                f"(default: {_DEFAULT_LIMITS.max_working_memory_bytes})"
            ),
        )
        command.add_argument(
            "--keep-normalized",
            action="store_true",
            help="keep the normalized object files instead of discarding them after the run",
        )
        command.add_argument(
            "--save-diagnostics",
            action="store_true",
            help="also write the per-pair NCD and distance diagnostics as CSV",
        )
    estimate_parser = commands.choices["estimate"]
    estimate_parser.add_argument(
        "--json",
        action="store_true",
        help="write the estimate to stdout as one JSON document instead of to stderr",
    )
    run_parser = commands.choices["run"]
    run_parser.add_argument(
        "--compressor",
        choices=("zlib", "gzip"),
        default="zlib",
        help="compressor backing the NCD measurement (default: zlib)",
    )
    run_parser.add_argument(
        "--compression-level",
        type=int,
        default=6,
        help="compression level from 0 to 9; changes the distances, so it identifies a run "
        "(default: 6)",
    )
    run_parser.add_argument(
        "--clusters",
        type=int,
        help="cut the dendrogram into exactly this many clusters; omit to cut where "
        "modularity is highest",
    )
    run_parser.add_argument(
        "--output-dir",
        help="write the run directory here; omit for ./damicore-results/<run id>",
    )
    run_parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress the progress display; checkpoints are written either way",
    )
    return parser


def _execution_from_arguments(arguments: argparse.Namespace) -> ExecutionConfig:
    requested: int | None = arguments.workers
    limits = ResourceLimits(
        max_objects=arguments.max_objects,
        max_pairs=arguments.max_pairs,
        max_matrix_bytes=arguments.max_matrix_bytes,
        max_working_memory_bytes=arguments.max_working_memory_bytes,
    )
    # "auto" is a literal in the config contract, so branch rather than widen to int | str.
    if requested is None:
        return ExecutionConfig(workers="auto", limits=limits)
    return ExecutionConfig(workers=requested, limits=limits)


def _exit_code(error: DamicoreError) -> int:
    if isinstance(error, (ConfigurationError, InputValidationError)):
        return 2
    if isinstance(error, ResourceLimitError):
        return 3
    if isinstance(error, ArtifactValidationError):
        return 4
    if isinstance(error, (OutputDirectoryConflictError, CheckpointMismatchError)):
        return 5
    return 4


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    # argparse yields Any, and a dict of options splatted with ** erases every signature
    # check at the call. Bind the parsed values to typed locals once, so each argument below
    # is verified against the public API it is passed to.
    source: list[str] = arguments.source
    source_kind: str = arguments.source_kind
    split: str | None = arguments.split
    delimiter: str | None = arguments.delimiter
    encoding: str | None = arguments.encoding
    sheet: str | None = arguments.sheet
    recursive: bool | None = arguments.recursive
    include_hidden: bool | None = arguments.include_hidden
    keep_normalized: bool = arguments.keep_normalized
    save_diagnostics: bool = arguments.save_diagnostics
    result: DamicoreResult | None = None
    try:
        # ResourceLimits and ExecutionConfig own the bounds behind these flags, so building
        # them is a validation step and belongs inside the handler: outside it, `--workers 0`
        # reaches the user as a pydantic traceback instead of the documented exit code.
        try:
            execution = _execution_from_arguments(arguments)
        except ValidationError as exc:
            raise ConfigurationError(str(exc)) from exc
        if arguments.command == "estimate":
            preview = estimate(
                source,
                source_kind=source_kind,
                split=split,
                delimiter=delimiter,
                encoding=encoding,
                sheet=sheet,
                recursive=recursive,
                include_hidden=include_hidden,
                keep_normalized=keep_normalized,
                save_diagnostics=save_diagnostics,
                execution=execution,
            )
            payload = preview.model_dump(mode="json")
            if arguments.json:
                # flush=True forces the pipe write to happen here, inside the handler that
                # owns the 141 contract. Left buffered, a closed pipe surfaces the error at
                # interpreter shutdown instead, which exits 120 — whether that happens then
                # depends on the ambient buffering mode (a TTY, PYTHONUNBUFFERED), not on us.
                print(json.dumps(payload, sort_keys=True), flush=True)
            else:
                print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        else:
            compressor: str = arguments.compressor
            compression_level: int = arguments.compression_level
            num_clusters: int | None = arguments.clusters
            output_dir: str | None = arguments.output_dir
            result = run(
                source,
                source_kind=source_kind,
                split=split,
                delimiter=delimiter,
                encoding=encoding,
                sheet=sheet,
                recursive=recursive,
                include_hidden=include_hidden,
                keep_normalized=keep_normalized,
                save_diagnostics=save_diagnostics,
                execution=execution,
                compressor=compressor,
                compression_level=compression_level,
                num_clusters=num_clusters,
                output_dir=output_dir,
                progress=not arguments.no_progress,
            )
            # Iterating the model rather than a hand-written list means an artifact added to
            # ArtifactPaths is printed without editing the CLI, and one that is absent for
            # this configuration stays absent instead of being printed as None.
            for name, path in result.artifacts:
                if path is not None:
                    print(f"{name}: {path}", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `damicore estimate --json input.csv | head` closes stdout as soon as it has enough,
        # which is ordinary shell usage rather than a failure of ours. 141 is the shell's
        # convention for death by SIGPIPE. Redirect stdout to devnull to suppress the
        # "Exception ignored" message that Python prints during cleanup when the pipe is broken.
        import os

        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (AttributeError, OSError):
            # stdout may not have a fileno (e.g., mocked in tests) or os.devnull may fail
            pass
        return 141
    except DamicoreError as error:
        print(json.dumps({"code": error.code, "message": str(error)}), file=sys.stderr)
        return _exit_code(error)
    finally:
        # The result owns an open memory map. Closing it only after the artifact lines are
        # printed leaks it whenever that write fails, which `damicore run | head` does.
        if result is not None:
            result.close()


if __name__ == "__main__":
    raise SystemExit(main())
