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
MAKEFILE = ROOT / "Makefile"

# The modules of the census that do not yet state the basis of what they compute.
#
# This list may only shrink. A module leaves it when its `References` section lands; nothing
# is ever added. That direction is a reviewer's judgement rather than a check, because the
# committed list is the only record of what it used to be -- and a check that read its own
# baseline from the working tree would be asserting against whatever the current commit says.
#
# It exists so the gate is green from the first commit. A gate that demanded declarations
# from a repository that has none would be red by construction, and a gate that is red by
# construction is one somebody switches off.
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


def _hint(key: str) -> str:
    """The repair for an unresolved key, which is not the same for both kinds of basis.

    A bibliography key is fetched. A decision this project made has no DOI to fetch, so
    telling its author to run `make ref` sends them to the one place the answer cannot be.
    """
    if key.startswith("adr-"):
        number = key.removeprefix("adr-")
        return f"  write the record: {DECISIONS.relative_to(ROOT)}/{number}-<slug>.md"
    return "  run: make ref DOI=<doi>   (never edit docs/refs.json or docs/references.bib)"


def test_an_unresolved_key_is_told_the_repair_for_its_kind_of_basis() -> None:
    """The failure message is the convention's only delivery to an assistant with no prior
    context, so it has to name the repair for the basis it is looking at. A decision this
    project made has no DOI to fetch, and four of the ten critical functions rest on one."""
    decision = _hint("adr-0001")
    assert "docs/decisions/" in decision
    assert "make ref" not in decision
    assert "make ref DOI=" in _hint("cilibrasi2005")


def test_cited_keys_resolve() -> None:
    refs = _refs()
    unresolved = [
        f"{path.relative_to(ROOT)} cites [{key}]\n{_hint(key)}"
        for path, text in _citing_files()
        for key in re.findall(CITATION, text)
        if not _resolves(key, refs)
    ]
    assert not unresolved, "\n".join(unresolved)


def _critical_modules() -> dict[str, Path]:
    """The census, read from the `Makefile` that owns it rather than copied beside it.

    `CRITICAL_MODULES` is already the list of modules held to the 95% coverage floor. The
    test below makes it the census that owes a basis as well: one declaration, two gates,
    and no second copy to drift.

    Which modules owe a basis is decided here and nowhere else. `AGENTS.md` says what a
    basis is and how it is cited; it does not scope the rule to a set of modules, so citing
    it for that scope would be citing a document for something it does not contain.
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    # One assignment, read whole. A second line -- `CRITICAL_MODULES += newick` is the edit
    # the comment beside the list invites next -- would be dropped by the search below and
    # the appended modules would be exempt without appearing in `UNDECLARED`. Refusing what
    # this parse does not read in full is the difference between a loud failure and a gate
    # that quietly covers less than it says.
    assignments = re.findall(r"^CRITICAL_MODULES\s*[+:]?=", makefile, re.MULTILINE)
    assert len(assignments) == 1, f"the Makefile assigns CRITICAL_MODULES {len(assignments)} times"
    declaration = re.search(r"^CRITICAL_MODULES\s*:=\s*(.+)$", makefile, re.MULTILINE)
    assert declaration is not None, "the Makefile no longer declares CRITICAL_MODULES"
    modules: dict[str, Path] = {}
    # A trailing `# comment` would otherwise become module names, and the failure would name
    # `#` rather than the comment that caused it.
    for name in declaration.group(1).split("#")[0].split():
        found = sorted(ROOT.glob(f"packages/*/src/*/{name}.py"))
        assert len(found) == 1, f"{name} resolves to {len(found)} files, not one"
        modules[name] = found[0]
    # An exemption for a module the census does not hold is debt that cannot be repaid: it
    # inflates the count nobody can shrink, and a later module reusing the name arrives
    # already exempt. The sibling at `test_boundaries.py` cross-checks its Makefile list the
    # same way, against the packages that actually exist.
    stale = sorted(UNDECLARED - modules.keys())
    assert not stale, f"UNDECLARED names modules outside the census: {', '.join(stale)}"
    return modules


def test_critical_modules_declare_references() -> None:
    """Every module in the census states the basis of what it computes, or is named as not
    yet doing so.

    It checks presence, at module granularity, and nothing beyond that. One docstring in the
    file carrying both a `References` heading and a citation satisfies it, so a module with
    several scientific functions can declare once and leave the rest silent; closing that
    needs a rule about which function owes which basis, which does not exist yet. It cannot
    check that a section is true, nor that the work it names supports the claim beside it --
    neither is machine-decidable, and ADR 0015 says so.

    What it does buy is that a module saying nothing at all cannot pass, which is the
    failure that actually happens: every other check in this file is conditional on a
    citation already being there.
    """
    silent: list[str] = []
    for name, path in _critical_modules().items():
        if name in UNDECLARED:
            continue
        declared = any(
            "References" in docstring and re.search(CITATION, docstring) is not None
            for docstring in _docstrings(path)
        )
        if not declared:
            silent.append(f"  {path.relative_to(ROOT)}")
    assert not silent, "\n".join(["these modules state no basis for what they compute:", *silent])


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
