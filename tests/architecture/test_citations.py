import ast
import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from bibtexparser.entrypoint import parse_file

pytestmark = pytest.mark.contract

ROOT = Path(__file__).parents[2]
REFS = ROOT / "docs" / "refs.json"
BIBLIOGRAPHY = ROOT / "docs" / "references.bib"
DECISIONS = ROOT / "docs" / "decisions"
MAKEFILE = ROOT / "Makefile"

# `[cilibrasi2005]` names a published work; `[adr-0001]` a decision this project made. The
# lookbehind excludes a subscript, which has the same shape and always follows a name, a `]`
# or a `)`.
CITATION = r"(?<![\w\])])\[@?((?:[a-z][a-z-]*)[0-9]{4}[a-z]?)\]"
REFERENCES_SECTION = r"^References\n-{3,}"

SOURCES = ("crossref", "openalex")

# The modules of the census that do not yet state the basis of what they compute.
#
# It exists so the gate is green from the first commit: a gate that is red by construction
# is one somebody switches off. It may only shrink, and that direction is a reviewer's
# judgement rather than a check, because the committed list is the only record of what it
# used to be.
UNDECLARED: frozenset[str] = frozenset(
    {
        "ncd",
        "neighbor_joining",
        "fastgreedy",
        "natural_breaks",
        "tree_graph",
        "compressor",
    }
)


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
    # Docstrings only: a citation is a claim about what a unit computes, and a bracketed
    # token in the code around it is not one.
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
    return key.startswith("adr-") and any(DECISIONS.glob(f"{key.removeprefix('adr-')}-*.md"))


def _hint(key: str) -> str:
    """The repair for an unresolved key, which is not the same for both kinds of basis.

    A bibliography entry is fetched. A decision this project made has no DOI, so telling its
    author to run `make ref` sends them to the one place the answer cannot be.
    """
    if key.startswith("adr-"):
        number = key.removeprefix("adr-")
        return f"  write the record: {DECISIONS.relative_to(ROOT)}/{number}-<slug>.md"
    return "  run: make ref DOI=<doi>   (never edit docs/refs.json or docs/references.bib)"


def _census_assignments(makefile: str) -> list[str]:
    """The lines that assign `CRITICAL_MODULES`, identified by what they are.

    Make has six assignment operators and accepts `export` and `override` before any of
    them, so enumerating them is a list of cases a Make release can extend. Every mention of
    the variable that is neither a comment nor an expansion of it is the axis instead.
    """
    return [
        line
        for line in makefile.splitlines()
        if "CRITICAL_MODULES" in line
        and not line.lstrip().startswith("#")
        and "$(CRITICAL_MODULES)" not in line
    ]


def _critical_modules() -> dict[str, Path]:
    """The census, read from the `Makefile` that owns it rather than copied beside it.

    `CRITICAL_MODULES` is already the list held to the 95% coverage floor. The test below
    makes it the census that owes a basis as well: one declaration, two gates, and no second
    copy to drift.
    """
    assignments = _census_assignments(MAKEFILE.read_text(encoding="utf-8"))
    # A second assignment would exempt the modules it names from both gates without their
    # appearing in `UNDECLARED`, so the parse refuses what it cannot read whole.
    assert len(assignments) == 1, f"the Makefile assigns CRITICAL_MODULES {len(assignments)} times"
    _, operator, declared = assignments[0].partition("=")
    assert operator, f"CRITICAL_MODULES is mentioned but not assigned: {assignments[0]!r}"
    modules: dict[str, Path] = {}
    for name in declared.split("#")[0].split():
        found = sorted(ROOT.glob(f"packages/*/src/**/{name}.py"))
        assert len(found) == 1, f"{name} resolves to {len(found)} files, not one"
        modules[name] = found[0]
    # An exemption for a module the census does not hold is debt that cannot be repaid: a
    # later module reusing the name would arrive already exempt.
    stale = sorted(UNDECLARED - modules.keys())
    assert not stale, f"UNDECLARED names modules outside the census: {', '.join(stale)}"
    return modules


def test_an_unresolved_key_is_told_the_repair_for_its_kind_of_basis() -> None:
    decision = _hint("adr-0001")
    assert "docs/decisions/" in decision
    assert "make ref" not in decision
    assert "make ref DOI=" in _hint("cilibrasi2005")


def test_a_subscript_is_not_read_as_a_citation() -> None:
    for expression in ["values[row2024]", "f()[row2024]", "rows[-1][row2024]"]:
        assert re.findall(CITATION, expression) == [], expression
    assert re.findall(CITATION, "see [cilibrasi2005] and [adr-0001].") == [
        "cilibrasi2005",
        "adr-0001",
    ]


def test_a_second_assignment_of_the_census_is_refused() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert len(_census_assignments(makefile)) == 1
    for appended in ("CRITICAL_MODULES += newick", "export CRITICAL_MODULES ::= newick"):
        assert len(_census_assignments(f"{makefile}\n{appended}\n")) == 2, appended


def test_cited_keys_resolve() -> None:
    refs = _refs()
    unresolved = [
        f"{path.relative_to(ROOT)} cites [{key}]\n{_hint(key)}"
        for path, text in _citing_files()
        for key in re.findall(CITATION, text)
        if not _resolves(key, refs)
    ]
    assert not unresolved, "\n".join(unresolved)


def test_critical_modules_declare_references() -> None:
    """Every module in the census states the basis of what it computes, or is named as not
    yet doing so.

    This is presence, at module granularity: one `References` section in the file, carrying
    one citation. Whether the work it names supports the claim beside it is checked by a
    reader; ADR 0015 records that no mechanism here attempts that.
    """
    silent: list[str] = []
    for name, path in _critical_modules().items():
        if name in UNDECLARED:
            continue
        declared = any(
            re.search(REFERENCES_SECTION, docstring, re.MULTILINE) is not None
            and re.search(CITATION, docstring) is not None
            for docstring in _docstrings(path)
        )
        if not declared:
            silent.append(f"  {path.relative_to(ROOT)}")
    assert not silent, "\n".join(["these modules state no basis for what they compute:", *silent])


def test_a_mention_of_references_is_not_a_references_section() -> None:
    assert re.search(REFERENCES_SECTION, "See the References there, per [adr-0001].") is None
    assert re.search(REFERENCES_SECTION, "References\n----------\n.. [adr-0001] x", re.MULTILINE)


def test_the_bibliography_reads_back_as_what_it_records() -> None:
    """An independent reader is what says the generated file is BibTeX.

    `test_generated_bibliography_matches_source` compares the renderer with its own output,
    so it cannot see a value that renders into something a reader takes differently. This
    one reads the file with a parser the project did not write and compares what comes back
    with `docs/refs.json`.
    """
    library = parse_file(str(BIBLIOGRAPHY))
    assert not library.failed_blocks, [str(block) for block in library.failed_blocks]
    refs = _refs()
    assert {entry.key for entry in library.entries} == set(refs)
    for entry in library.entries:
        source = refs[entry.key]
        # `refs.json` is deserialized JSON, so every value is `object` until narrowed; the
        # schema that would carry the types is the resolver's, not ours.
        authors = cast("list[object]", source["authors"])
        read = {name: field.value for name, field in entry.fields_dict.items()}
        assert entry.entry_type == source["type"]
        assert read["title"] == source["title"]
        assert read["year"] == str(source["year"])
        assert read["author"] == " and ".join(str(name) for name in authors)


def test_a_value_that_would_not_read_back_is_refused() -> None:
    """An unbalanced brace does not fail the parse, which is why it needs its own refusal.

    The reader stops at the brace that closes nothing and hands back a shorter string, so a
    committed entry would disagree with its source silently and the test above is the only
    thing that could see it. This refuses the value where it is written instead.
    """
    unbalanced: dict[str, dict[str, object]] = {
        "x2026": {"authors": ["A, B"], "title": "growth}", "year": 2026, "type": "article"}
    }
    with pytest.raises(ValueError, match="unbalanced braces"):
        _render_bibliography(unbalanced)


def test_no_stored_value_carries_an_html_entity() -> None:
    """A resolver answers in JSON but its strings come from HTML, so a venue can arrive as
    `Transmission &amp; Distribution`.

    The entity survives every check that compares our files with each other, because both
    carry it, and it reaches the published bibliography as part of the work's name.
    """
    entity = re.compile(r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")
    escaped: list[str] = []
    for key, entry in _refs().items():
        for field, value in entry.items():
            # Serialized, so a list of authors is searched the same way a title is.
            rendered = json.dumps(value, ensure_ascii=False)
            if entity.search(rendered):
                escaped.append(f"{key}.{field} = {rendered}")
    assert not escaped, "\n".join(["these stored values are HTML-escaped:", *escaped])


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
