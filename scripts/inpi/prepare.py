#!/usr/bin/env python3
"""Prepare deterministic DAMICORE source archives for INPI registration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomllib

SCHEMA_VERSION = 1
HASH_ALGORITHM = "SHA-512"
TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class PreparationError(RuntimeError):
    """The INPI preparation contract was not satisfied."""


@dataclass(frozen=True)
class SoftwareSpec:
    source: str
    title: str
    languages: tuple[str, ...]
    application_fields: tuple[str, ...]
    program_types: tuple[str, ...]


@dataclass(frozen=True)
class SourceEntry:
    archive_path: str
    blob_sha: str
    executable: bool


@dataclass(frozen=True)
class PackagePlan:
    spec: SoftwareSpec
    project_name: str
    version: str
    entries: tuple[SourceEntry, ...]

    @property
    def archive_name(self) -> str:
        return f"{self.project_name}-{self.version}-inpi.zip"


@dataclass(frozen=True)
class PreparedSoftware:
    spec: SoftwareSpec
    project_name: str
    archive_name: str
    sha512: str


def run_git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    try:
        completed = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise PreparationError("Git is required but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise PreparationError(
            f"git {' '.join(args)} failed: {detail or 'unknown Git error'}"
        ) from exc
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8").strip()


def repository_root(start: Path) -> Path:
    root = run_git(start, "rev-parse", "--show-toplevel")
    assert isinstance(root, str)
    return Path(root).resolve()


def resolve_release(repo: Path, tag: str) -> tuple[str, str]:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise PreparationError("release must be an immutable stable tag in the form vX.Y.Z")
    try:
        run_git(repo, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}")
    except PreparationError as exc:
        raise PreparationError(f"tag {tag!r} does not exist") from exc
    commit = run_git(repo, "rev-parse", f"{tag}^{{commit}}")
    assert isinstance(commit, str)
    return match.group("version"), commit


def read_blob(repo: Path, sha: str) -> bytes:
    data = run_git(repo, "cat-file", "blob", sha, binary=True)
    assert isinstance(data, bytes)
    return data


def text_array(table: dict[str, object], key: str, context: str) -> tuple[str, ...]:
    value = table.get(key)
    if not isinstance(value, list) or not value:
        raise PreparationError(f"{context}.{key} must be a non-empty string array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PreparationError(f"{context}.{key} must contain only non-empty strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise PreparationError(f"{context}.{key} must not contain duplicates")
    return result


def load_config(path: Path) -> tuple[SoftwareSpec, ...]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PreparationError(f"cannot read INPI config {path}: {exc}") from exc
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PreparationError(f"config.schema_version must be {SCHEMA_VERSION}")
    raw = document.get("software")
    if not isinstance(raw, list) or not raw:
        raise PreparationError("config.software must be a non-empty array of tables")

    specs: list[SoftwareSpec] = []
    for index, item in enumerate(raw, start=1):
        context = f"config.software[{index}]"
        if not isinstance(item, dict):
            raise PreparationError(f"{context} must be a table")
        source = item.get("source")
        title = item.get("title")
        if not isinstance(source, str) or not source or not isinstance(title, str) or not title:
            raise PreparationError(f"{context}.source and .title must be non-empty strings")
        source_path = PurePosixPath(source)
        if (
            source_path.is_absolute()
            or ".." in source_path.parts
            or source_path.as_posix() != source
            or source == "."
        ):
            raise PreparationError(f"{context}.source must be a canonical relative path")
        specs.append(
            SoftwareSpec(
                source=source,
                title=title,
                languages=text_array(item, "languages", context),
                application_fields=text_array(item, "application_fields", context),
                program_types=text_array(item, "program_types", context),
            )
        )
    result = tuple(specs)
    if len({item.source for item in result}) != len(result):
        raise PreparationError("config contains duplicate software.source values")
    if len({item.title for item in result}) != len(result):
        raise PreparationError("config contains duplicate software.title values")
    return result


def parse_tree(repo: Path, tag: str, spec: SoftwareSpec, version: str) -> PackagePlan:
    listing = run_git(
        repo, "ls-tree", "-r", "-z", "--full-tree", tag, "--", spec.source, binary=True
    )
    assert isinstance(listing, bytes)
    if not listing:
        raise PreparationError(f"{spec.source} does not exist in {tag}")

    raw_entries: list[tuple[str, str, str]] = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, sha = metadata.decode("ascii").split()
            repository_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise PreparationError(f"unexpected Git tree record in {spec.source}") from exc
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PreparationError(
                f"unsupported Git entry in {spec.source}: {mode} {object_type} {repository_path}"
            )
        raw_entries.append((repository_path, sha, mode))

    pyproject_path = f"{spec.source}/pyproject.toml"
    pyproject = next((entry for entry in raw_entries if entry[0] == pyproject_path), None)
    if pyproject is None:
        raise PreparationError(f"{pyproject_path} is missing in {tag}")
    try:
        project = tomllib.loads(read_blob(repo, pyproject[1]).decode("utf-8"))["project"]
        project_name = project["name"]
        project_version = project["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise PreparationError(f"cannot read project name/version from {pyproject_path}") from exc
    if not isinstance(project_name, str) or not project_name:
        raise PreparationError(f"{pyproject_path} has an invalid project.name")
    if project_version != version:
        raise PreparationError(
            f"{pyproject_path} declares version {project_version!r}; "
            f"release tag requires {version!r}"
        )
    if not any(
        path.startswith(f"{spec.source}/src/") and path.endswith(".py")
        for path, _, _ in raw_entries
    ):
        raise PreparationError(f"{spec.source} contains no Python implementation under src/")

    prefix = f"{spec.source}/"
    archive_root = f"{project_name}-{version}"
    entries = tuple(
        SourceEntry(
            archive_path=str(PurePosixPath(archive_root, path.removeprefix(prefix))),
            blob_sha=sha,
            executable=mode == "100755",
        )
        for path, sha, mode in sorted(raw_entries)
    )
    return PackagePlan(spec, project_name, version, entries)


def ensure_output_safe(repo: Path, output: Path) -> None:
    try:
        relative = output.relative_to(repo)
    except ValueError:
        return
    try:
        run_git(repo, "check-ignore", "--quiet", "--no-index", "--", relative.as_posix())
    except PreparationError as exc:
        raise PreparationError(
            f"output directory {relative.as_posix()!r} is inside the repository but is not ignored"
        ) from exc


def zip_info(path: str, executable: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (0o100755 if executable else 0o100644) << 16
    return info


def write_archive(repo: Path, destination: Path, plan: PackagePlan) -> None:
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as archive:
        for entry in plan.entries:
            archive.writestr(
                zip_info(entry.archive_path, entry.executable), read_blob(repo, entry.blob_sha)
            )
    expected = [entry.archive_path for entry in plan.entries]
    with zipfile.ZipFile(destination, "r") as archive:
        names = archive.namelist()
        if names != expected or len(names) != len(set(names)):
            raise PreparationError(f"archive entry list mismatch in {destination.name}")
        if any(
            PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in names
        ):
            raise PreparationError(f"unsafe archive path in {destination.name}")
        corrupt = archive.testzip()
        if corrupt is not None:
            raise PreparationError(f"CRC verification failed in {destination.name}: {corrupt}")


def sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def qa(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(q(value) for value in values) + "]"


def render_registration(
    release: str, version: str, commit: str, prepared: tuple[PreparedSoftware, ...]
) -> str:
    lines = [
        f"schema_version = {SCHEMA_VERSION}",
        f"release = {q(release)}",
        f"version = {q(version)}",
        f"commit = {q(commit)}",
        f"hash_algorithm = {q(HASH_ALGORITHM)}",
    ]
    for item in prepared:
        lines += [
            "",
            "[[software]]",
            f"source = {q(item.spec.source)}",
            f"title = {q(item.spec.title)}",
            f"project = {q(item.project_name)}",
            f"archive = {q(item.archive_name)}",
            f"sha512 = {q(item.sha512)}",
            f"languages = {qa(item.spec.languages)}",
            f"application_fields = {qa(item.spec.application_fields)}",
            f"program_types = {qa(item.spec.program_types)}",
            'inpi_process = ""',
            'inpi_registration = ""',
            'inpi_url = ""',
        ]
    return "\n".join(lines) + "\n"


def verify_registration(
    path: Path, release: str, version: str, commit: str, prepared: tuple[PreparedSoftware, ...]
) -> None:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PreparationError(f"cannot read INPI registration {path}: {exc}") from exc
    header = {
        "schema_version": SCHEMA_VERSION,
        "release": release,
        "version": version,
        "commit": commit,
        "hash_algorithm": HASH_ALGORITHM,
    }
    if any(document.get(key) != value for key, value in header.items()):
        raise PreparationError("registration header does not match regenerated data")
    tables = document.get("software")
    if not isinstance(tables, list) or len(tables) != len(prepared):
        raise PreparationError("registration software count does not match config")
    for index, (table, item) in enumerate(zip(tables, prepared, strict=True), start=1):
        if not isinstance(table, dict):
            raise PreparationError(f"registration.software[{index}] must be a table")
        expected = {
            "source": item.spec.source,
            "title": item.spec.title,
            "project": item.project_name,
            "archive": item.archive_name,
            "sha512": item.sha512,
            "languages": list(item.spec.languages),
            "application_fields": list(item.spec.application_fields),
            "program_types": list(item.spec.program_types),
        }
        if any(table.get(key) != value for key, value in expected.items()):
            raise PreparationError(
                f"registration.software[{index}] does not match regenerated data"
            )
        reference_keys = ("inpi_process", "inpi_registration", "inpi_url")
        if any(not isinstance(table.get(key), str) for key in reference_keys):
            raise PreparationError(f"registration.software[{index}] has invalid INPI references")


def create_registration(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except (FileExistsError, OSError) as exc:
        raise PreparationError(f"cannot create registration record {path}: {exc}") from exc


def prepare(
    repo: Path,
    tag: str,
    output: Path,
    config_path: Path,
    registration_path: Path,
) -> tuple[str, str, tuple[PreparedSoftware, ...], bool]:
    version, commit = resolve_release(repo, tag)
    specs = load_config(config_path)
    ensure_output_safe(repo, output)
    if output.exists():
        raise PreparationError(f"output directory already exists: {output}")
    plans = tuple(parse_tree(repo, tag, spec, version) for spec in specs)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="damicore-inpi-", dir=output.parent) as temporary:
        staging = Path(temporary)
        prepared_items: list[PreparedSoftware] = []
        for plan in plans:
            archive_path = staging / plan.archive_name
            write_archive(repo, archive_path, plan)
            prepared_items.append(
                PreparedSoftware(
                    plan.spec, plan.project_name, plan.archive_name, sha512(archive_path)
                )
            )
        prepared = tuple(prepared_items)
        (staging / "SHA512SUMS.txt").write_text(
            "".join(f"{item.sha512}  {item.archive_name}\n" for item in prepared),
            encoding="ascii",
            newline="\n",
        )
        created = not registration_path.exists()
        if created:
            create_registration(
                registration_path, render_registration(tag, version, commit, prepared)
            )
        else:
            verify_registration(registration_path, tag, version, commit, prepared)
        if output.exists():
            raise PreparationError(f"output directory appeared during generation: {output}")
        os.rename(staging, output)
    return version, commit, prepared, created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create deterministic INPI archives and a release registration record."
    )
    parser.add_argument("release", help="immutable stable release tag, for example v0.2.0")
    parser.add_argument(
        "--out",
        type=Path,
        help="output directory; defaults to .temp/inpi/<version> inside the repository",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = repository_root(Path.cwd())
        match = TAG_PATTERN.fullmatch(args.release)
        if match is None:
            raise PreparationError("release must be an immutable stable tag in the form vX.Y.Z")
        version = match.group("version")
        output = (args.out if args.out is not None else repo / ".temp" / "inpi" / version).resolve()
        inpi_dir = repo / "scripts" / "inpi"
        registration_path = inpi_dir / "registrations" / f"{version}.toml"
        version, commit, prepared, created = prepare(
            repo, args.release, output, inpi_dir / "config.toml", registration_path
        )
    except PreparationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("DAMICORE INPI preparation")
    print(f"release:      {args.release}")
    print(f"commit:       {commit}")
    print(f"version:      {version}")
    print(f"output:       {output}")
    print(f"registration: {registration_path}")
    for item in prepared:
        print(f"\n{item.spec.title}")
        print(f"  archive: {item.archive_name}")
        print(f"  SHA-512: {item.sha512}")
    print(f"\nRegistration record {'created' if created else 'verified'}; SHA512SUMS.txt written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
