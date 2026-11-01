import ast
import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).parents[2]
REFS = ROOT / "docs" / "refs.json"
BIBLIOGRAPHY = ROOT / "docs" / "references.bib"
DECISIONS = ROOT / "docs" / "decisions"

# A citation is a bracketed key: a lowercase name, then a four-digit year, then an optional
# disambiguating letter. `[adr-0001]` is the same shape and names a decision this project
# made rather than a published work, which is why the hyphen is in the name class. The
# brackets are what keep this from matching ordinary prose; a bare `@key` is never matched,
# because it collides with a Python decorator.
CITATION = r"\[@?((?:[a-z][a-z-]*)[0-9]{4}[a-z]?)\]"

SOURCES = ("crossref", "openalex")


def _refs() -> dict[str, dict[str, object]]:
    if not REFS.is_file():
        pytest.fail(f"{REFS.relative_to(ROOT)} does not exist; run `make ref DOI=<doi>`")
    parsed: dict[str, dict[str, object]] = json.loads(REFS.read_text(encoding="utf-8"))
    return parsed


def _docstrings(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node)
            if docstring:
                yield docstring


def _citing_files() -> Iterator[tuple[Path, str]]:
    # Only docstrings are read from source, never the code around them: a citation is a
    # claim about what a unit computes, and a bracketed token in an expression is not one.
    for path in sorted(ROOT.glob("packages/*/src/**/*.py")):
        for docstring in _docstrings(path):
            yield path, docstring
    markdown = [ROOT / "README.md", *sorted(ROOT.glob("docs/**/*.md"))]
    markdown.extend(sorted(ROOT.glob("packages/*/README.md")))
    for path in markdown:
        if path.is_file():
            yield path, path.read_text(encoding="utf-8")


def _render_bibliography(refs: dict[str, dict[str, object]]) -> str:
    spec = importlib.util.spec_from_file_location(
        "references", ROOT / ".github" / "scripts" / "references.py"
    )
    if spec is None or spec.loader is None:
        pytest.fail(".github/scripts/references.py does not exist")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered: str = module.render_bibliography(refs)
    return rendered


def _resolves(key: str, refs: dict[str, dict[str, object]]) -> bool:
    if key in refs:
        return True
    # A unit whose basis is a decision this project made cites that decision directly, with
    # no bibliography entry: four of the ten critical functions have no published source.
    return key.startswith("adr-") and any(DECISIONS.glob(f"{key.removeprefix('adr-')}-*.md"))


def test_cited_keys_resolve() -> None:
    refs = _refs()
    unresolved = [
        f"{path.relative_to(ROOT)} cites [{key}]"
        for path, text in _citing_files()
        for key in re.findall(CITATION, text)
        if not _resolves(key, refs)
    ]
    assert not unresolved, "\n".join(
        [
            *unresolved,
            "  run: make ref DOI=<doi>",
            "  (never edit docs/refs.json or docs/references.bib by hand)",
        ]
    )


def test_entries_without_doi_name_an_existing_adr() -> None:
    refs = _refs()
    missing: list[str] = []
    for key, entry in refs.items():
        source = str(entry.get("source", ""))
        if source in SOURCES:
            assert entry.get("doi"), f"{key} is resolver-sourced and must carry its doi"
            continue
        assert source.startswith("adr:"), (
            f"{key} has source {source!r}; it must be one of {SOURCES} or 'adr:NNNN'"
        )
        if not any(DECISIONS.glob(f"{source.removeprefix('adr:')}-*.md")):
            missing.append(f"{key} names {source}, which does not exist")
    assert not missing, "\n".join(missing)


def test_generated_bibliography_matches_source() -> None:
    refs = _refs()
    assert BIBLIOGRAPHY.is_file(), "docs/references.bib is missing; run `make references`"
    assert BIBLIOGRAPHY.read_text(encoding="utf-8") == _render_bibliography(refs), (
        "docs/references.bib does not match docs/refs.json; it is generated, never edited. "
        "Run `make references`."
    )
