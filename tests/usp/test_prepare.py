"""Behavioral tests for USP dossier preparation; no network or real inventor data."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "usp" / "prepare.py"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts/usp").mkdir(parents=True)
    (root / "scripts/inpi/registrations").mkdir(parents=True)
    (root / "packages/damicore_normalizer/src/damicore_normalizer").mkdir(parents=True)
    (root / "packages/damicore_distance/src/damicore_distance").mkdir(parents=True)
    shutil.copyfile(SCRIPT, root / "scripts/usp/prepare.py")
    (root / ".gitignore").write_text(".temp/\n", encoding="utf-8")
    (root / "scripts/inpi/config.toml").write_text(
        "schema_version = 1\n"
        '[[software]]\nsource = "packages/damicore_normalizer"\n'
        'title = "DAMICORE Normalizer"\nlanguages = ["Python"]\n'
        'program_types = ["TC01"]\napplication_fields = ["IF10"]\n'
        '[[software]]\nsource = "packages/damicore_distance"\n'
        'title = "DAMICORE Distance"\nlanguages = ["Python"]\n'
        'program_types = ["TC01"]\napplication_fields = ["IF10"]\n',
        encoding="utf-8",
    )
    for name in ("normalizer", "distance"):
        folder = root / f"packages/damicore_{name}"
        (folder / "pyproject.toml").write_text(
            f'[project]\nname = "damicore-{name}"\nversion = "0.3.0"\n'
            'description = "Tested object preparation for scientific analysis."\n',
            encoding="utf-8",
        )
        (folder / f"src/damicore_{name}/__init__.py").write_text(
            "# ação e integridade\n" + "def f(value: str) -> str:\n" + "    return value\n",
            encoding="utf-8",
        )
    git(root, "init")
    git(root, "config", "user.email", "test@example.org")
    git(root, "config", "user.name", "Unit Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "synthetic release")
    git(root, "tag", "v0.3.0")
    return root


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "scripts/usp/prepare.py"), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def out(repo: Path) -> Path:
    return repo / ".temp/usp/0.3.0/damicore-normalizer"


def test_prepares_one_component_with_searchable_source_pdf(repo: Path) -> None:
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer", "--case", "PC-2026-0060")
    assert result.returncode == 0, result.stderr
    output = out(repo)
    assert {p.name for p in output.iterdir()} == {
        "form.json",
        "source-code.pdf",
        "memorial.md",
        "justification.md",
    }
    dossier = json.loads((output / "form.json").read_text(encoding="utf-8"))
    assert dossier["identity"]["commit"] == git(repo, "rev-parse", "v0.3.0")
    assert dossier["identity"]["usp_case"] == "PC-2026-0060"
    assert dossier["identity"]["inpi"] is None
    assert dossier["form"]["program"]["creation_date"] is None
    source_text = "\n".join(
        page.extract_text() for page in PdfReader(output / "source-code.pdf").pages
    )
    assert "ação e integridade" in source_text
    assert "return value" in source_text
    assert "damicore_distance" not in source_text
    assert "Page 1" in source_text


def test_working_tree_changes_do_not_affect_tagged_source(repo: Path) -> None:
    path = repo / "packages/damicore_normalizer/src/damicore_normalizer/__init__.py"
    path.write_text("# DANGEROUS DIRTY WORKTREE\n", encoding="utf-8")
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer")
    assert result.returncode == 0, result.stderr
    text = "\n".join(p.extract_text() for p in PdfReader(out(repo) / "source-code.pdf").pages)
    assert "return value" in text and "DANGEROUS" not in text


def test_existing_directory_is_never_overwritten(repo: Path) -> None:
    assert run(repo, "v0.3.0", "--package", "damicore-normalizer").returncode == 0
    edited = out(repo) / "form.json"
    edited.write_text("USER EDIT", encoding="utf-8")
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer")
    assert result.returncode == 2
    assert edited.read_text(encoding="utf-8") == "USER EDIT"


def test_missing_release_or_package_refuses_before_writing(repo: Path) -> None:
    assert run(repo, "v0.9.0", "--package", "damicore-normalizer").returncode == 2
    assert run(repo, "v0.3.0", "--package", "unknown").returncode == 2
    assert not out(repo).exists()


def test_outside_repo_is_accepted_and_unignored_inside_is_refused(repo: Path) -> None:
    invalid = repo / "not-ignored" / "case"
    assert (
        run(repo, "v0.3.0", "--package", "damicore-normalizer", "--out", str(invalid)).returncode
        == 2
    )
    external = repo.parent / "usp-output"
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer", "--out", str(external))
    assert result.returncode == 0, result.stderr
    assert (external / "source-code.pdf").is_file()


def test_mismatched_registration_is_refused(repo: Path) -> None:
    (repo / "scripts/inpi/registrations/0.3.0.toml").write_text(
        'release = "v0.3.0"\ncommit = "incorrect"\n', encoding="utf-8"
    )
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer")
    assert result.returncode == 2
    assert "INPI history does not match" in result.stderr
    assert not out(repo).exists()


def test_registration_digest_is_reused_and_checked_when_zip_present(repo: Path) -> None:
    payload = b"archive from INPI"
    archive = repo / ".temp/inpi/0.3.0/damicore-normalizer-0.3.0-inpi.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(payload)
    digest = hashlib.sha512(payload).hexdigest()
    (repo / "scripts/inpi/registrations/0.3.0.toml").write_text(
        f'release = "v0.3.0"\ncommit = "{git(repo, "rev-parse", "v0.3.0")}"\n'
        f'[[software]]\nproject = "damicore-normalizer"\n'
        f'source = "packages/damicore_normalizer"\narchive = "{archive.name}"\n'
        f'sha512 = "{digest}"\n',
        encoding="utf-8",
    )
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer")
    assert result.returncode == 0, result.stderr
    dossier = json.loads((out(repo) / "form.json").read_text(encoding="utf-8"))
    assert dossier["identity"]["inpi"] == {
        "archive": archive.name,
        "sha512": digest,
        "archive_verified": True,
    }


def test_finalization_preserves_editable_files_and_renders_pdfs(repo: Path) -> None:
    assert run(repo, "v0.3.0", "--package", "damicore-normalizer").returncode == 0
    output = out(repo)
    for filename in ("memorial.md", "justification.md"):
        draft = output / filename
        draft.write_text(
            draft.read_text(encoding="utf-8").replace("[REVISÃO OBRIGATÓRIA:", "[Revisado:"),
            encoding="utf-8",
        )
    original = (output / "form.json").read_bytes()
    result = run(repo, "--finalize", str(output))
    assert result.returncode == 0, result.stderr
    assert "Pending" in result.stdout
    assert (output / "form.json").read_bytes() == original
    for name in ("memorial.pdf", "justification.pdf"):
        pdf = PdfReader(output / name)
        assert len(pdf.pages) >= 1
        assert len(pdf.pages[0].extract_text()) > 100
    assert run(repo, "--finalize", str(output)).returncode == 0


def test_unreviewed_draft_prevents_document_finalization(repo: Path) -> None:
    assert run(repo, "v0.3.0", "--package", "damicore-normalizer").returncode == 0
    assert run(repo, "--finalize", str(out(repo))).returncode == 2
    assert not (out(repo) / "memorial.pdf").exists()


def test_wrong_choice_or_justification_length_fails_finalization(repo: Path) -> None:
    assert run(repo, "v0.3.0", "--package", "damicore-normalizer").returncode == 0
    output = out(repo)
    dossier = json.loads((output / "form.json").read_text(encoding="utf-8"))
    dossier["form"]["technology_transfer"]["viability"]["cost"]["choice"] = "made up"
    dossier["form"]["technology_transfer"]["market"]["demand_justification"] = "x" * 501
    (output / "form.json").write_text(json.dumps(dossier), encoding="utf-8")
    result = run(repo, "--finalize", str(output))
    assert result.returncode == 2
    assert "viability.cost" in result.stderr and "500" in result.stderr


def test_unsupported_unicode_fails_without_partial_output(repo: Path) -> None:
    # A glyph not covered by the embedded font must fail, never silently become a box.
    f = repo / "packages/damicore_normalizer/src/damicore_normalizer/__init__.py"
    f.write_text(f.read_text(encoding="utf-8") + "# 🧬\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unsupported glyph")
    git(repo, "tag", "-f", "v0.3.0")
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer")
    assert result.returncode == 2
    assert "unrenderable Unicode" in result.stderr
    assert not out(repo).exists()


def test_no_source_overwrite_or_other_package_mutation(repo: Path) -> None:
    before = git(repo, "status", "--porcelain")
    result = run(repo, "v0.3.0", "--package", "damicore-normalizer")
    assert result.returncode == 0, result.stderr
    after = git(repo, "status", "--porcelain")
    assert before == after
    with (out(repo) / "form.json").open(encoding="utf-8") as stream:
        assert isinstance(json.load(stream), dict)
