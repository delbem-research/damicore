#!/usr/bin/env python3
"""Create deterministic INPI source archives for the four DAMICORE stage packages."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomllib

STAGE_PACKAGES = (
    "damicore_normalizer",
    "damicore_distance",
    "damicore_tree_builder",
    "damicore_clusterizer",
)
TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class PreparationError(RuntimeError):
    """A pre-flight or archive-integrity check failed."""


@dataclass(frozen=True)
class SourceEntry:
    """One regular Git blob that belongs to a stage package."""

    archive_path: str
    blob_sha: str
    executable: bool


@dataclass(frozen=True)
class PackagePlan:
    """Validated inputs and derived output identity for one stage package."""

    project_name: str
    version: str
    entries: tuple[SourceEntry, ...]

    @property
    def archive_name(self) -> str:
        return f"{self.project_name}-{self.version}-inpi.zip"


def run_git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    """Run Git in ``repo`` and return stdout, translating failures into one error type."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise PreparationError("Git is required but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        command = " ".join(("git", *args))
        raise PreparationError(f"{command} failed: {detail or 'unknown Git error'}") from exc
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8").strip()


def repository_root(start: Path) -> Path:
    """Return the enclosing Git worktree root."""
    root = run_git(start, "rev-parse", "--show-toplevel")
    assert isinstance(root, str)
    return Path(root).resolve()


def resolve_release(repo: Path, tag: str) -> tuple[str, str]:
    """Validate a stable ``vX.Y.Z`` tag and return its version and commit SHA."""
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise PreparationError("release must be an immutable stable tag in the form vX.Y.Z")

    reference = f"refs/tags/{tag}"
    try:
        run_git(repo, "show-ref", "--verify", "--quiet", reference)
    except PreparationError as exc:
        raise PreparationError(f"tag {tag!r} does not exist") from exc

    commit = run_git(repo, "rev-parse", f"{tag}^{{commit}}")
    assert isinstance(commit, str)
    return match.group("version"), commit


def read_blob(repo: Path, blob_sha: str) -> bytes:
    """Read one Git blob without consulting the working tree."""
    content = run_git(repo, "cat-file", "blob", blob_sha, binary=True)
    assert isinstance(content, bytes)
    return content


def parse_tree(repo: Path, tag: str, package: str, version: str) -> PackagePlan:
    """Validate and describe one package subtree at ``tag``."""
    package_root = f"packages/{package}"
    listing = run_git(
        repo, "ls-tree", "-r", "-z", "--full-tree", tag, "--", package_root, binary=True
    )
    assert isinstance(listing, bytes)
    if not listing:
        raise PreparationError(f"{package_root} does not exist in {tag}")

    raw_entries: list[tuple[str, str, str]] = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, blob_sha = metadata.decode("ascii").split()
            repository_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise PreparationError(f"unexpected git ls-tree record in {package_root}") from exc
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PreparationError(
                f"unsupported Git entry in {package_root}: {mode} {object_type} {repository_path}"
            )
        raw_entries.append((repository_path, blob_sha, mode))

    pyproject_path = f"{package_root}/pyproject.toml"
    pyproject = next((item for item in raw_entries if item[0] == pyproject_path), None)
    if pyproject is None:
        raise PreparationError(f"{pyproject_path} is missing in {tag}")

    try:
        document = tomllib.loads(read_blob(repo, pyproject[1]).decode("utf-8"))
        project = document["project"]
        project_name = project["name"]
        project_version = project["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise PreparationError(f"cannot read project name/version from {pyproject_path}") from exc

    expected_name = package.replace("_", "-")
    if project_name != expected_name:
        raise PreparationError(
            f"{pyproject_path} declares project.name={project_name!r}; expected {expected_name!r}"
        )
    if project_version != version:
        raise PreparationError(
            f"{pyproject_path} declares version {project_version!r}; "
            f"release tag requires {version!r}"
        )

    source_prefix = f"{package_root}/src/{package}/"
    has_python_source = any(
        path.startswith(source_prefix) and path.endswith(".py") for path, _, _ in raw_entries
    )
    if not has_python_source:
        raise PreparationError(
            f"{package_root} contains no Python implementation under src/{package}/"
        )

    entries: list[SourceEntry] = []
    prefix = f"{package_root}/"
    archive_root = f"{project_name}-{version}"
    for repository_path, blob_sha, mode in sorted(raw_entries):
        if not repository_path.startswith(prefix):
            raise PreparationError(f"Git returned a path outside {package_root}: {repository_path}")
        relative = repository_path.removeprefix(prefix)
        archive_path = str(PurePosixPath(archive_root, relative))
        entries.append(
            SourceEntry(
                archive_path=archive_path,
                blob_sha=blob_sha,
                executable=mode == "100755",
            )
        )

    return PackagePlan(
        project_name=project_name,
        version=version,
        entries=tuple(entries),
    )


def ensure_ignored_if_inside_repo(repo: Path, output: Path) -> None:
    """Reject generated output inside the repository unless Git ignores it."""
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


def preflight(repo: Path, tag: str, output: Path) -> tuple[str, str, tuple[PackagePlan, ...]]:
    """Validate all inputs before creating any final output."""
    version, commit = resolve_release(repo, tag)
    ensure_ignored_if_inside_repo(repo, output)
    if output.exists():
        raise PreparationError(f"output directory already exists: {output}")

    plans = tuple(parse_tree(repo, tag, package, version) for package in STAGE_PACKAGES)
    return version, commit, plans


def zip_info(path: str, executable: bool) -> zipfile.ZipInfo:
    """Return normalized ZIP metadata for one regular file."""
    info = zipfile.ZipInfo(path, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    permissions = 0o100755 if executable else 0o100644
    info.external_attr = permissions << 16
    return info


def write_archive(repo: Path, destination: Path, plan: PackagePlan) -> None:
    """Write and then verify one deterministic stage archive."""
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as archive:
        for entry in plan.entries:
            archive.writestr(
                zip_info(entry.archive_path, entry.executable), read_blob(repo, entry.blob_sha)
            )

    expected = [entry.archive_path for entry in plan.entries]
    with zipfile.ZipFile(destination, "r") as archive:
        names = archive.namelist()
        if names != expected:
            raise PreparationError(f"archive entry list mismatch after writing {destination.name}")
        if len(names) != len(set(names)):
            raise PreparationError(f"duplicate entries found in {destination.name}")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise PreparationError(f"unsafe archive path in {destination.name}: {name}")
        corrupt = archive.testzip()
        if corrupt is not None:
            raise PreparationError(f"CRC verification failed in {destination.name}: {corrupt}")


def sha512(path: Path) -> str:
    """Return the SHA-512 digest of ``path``."""
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(repo: Path, tag: str, output: Path) -> tuple[str, str, list[tuple[str, str]]]:
    """Create the four archives atomically as one output directory."""
    version, commit, plans = preflight(repo, tag, output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="damicore-inpi-", dir=output.parent) as temporary:
        staging = Path(temporary)
        checksums: list[tuple[str, str]] = []
        for plan in plans:
            archive_path = staging / plan.archive_name
            write_archive(repo, archive_path, plan)
            checksums.append((plan.archive_name, sha512(archive_path)))

        checksum_file = staging / "SHA512SUMS.txt"
        checksum_file.write_text(
            "".join(f"{digest}  {name}\n" for name, digest in checksums),
            encoding="ascii",
            newline="\n",
        )

        if output.exists():
            raise PreparationError(f"output directory appeared during generation: {output}")
        os.rename(staging, output)

    return version, commit, checksums


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""
    parser = argparse.ArgumentParser(
        description="Create deterministic INPI ZIP archives for DAMICORE stage packages."
    )
    parser.add_argument("release", help="immutable stable release tag, for example v0.2.0")
    parser.add_argument(
        "--out",
        type=Path,
        help="output directory; defaults to .temp/inpi/<version> inside the repository",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the INPI archive preparation command."""
    args = build_parser().parse_args(argv)
    try:
        repo = repository_root(Path.cwd())
        match = TAG_PATTERN.fullmatch(args.release)
        if match is None:
            raise PreparationError("release must be an immutable stable tag in the form vX.Y.Z")
        version = match.group("version")
        output = (args.out if args.out is not None else repo / ".temp" / "inpi" / version).resolve()
        version, commit, checksums = prepare(repo, args.release, output)
    except PreparationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("DAMICORE INPI preparation")
    print(f"release: {args.release}")
    print(f"commit:  {commit}")
    print(f"version: {version}")
    print(f"output:  {output}")
    for name, digest in checksums:
        print(f"\n{name}")
        print(f"  SHA-512: {digest}")
    print("\nSHA512SUMS.txt written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
