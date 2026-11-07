import ast
import importlib
import importlib.util
import json
import pkgutil
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast, get_args, get_origin

import pytest
from bibtexparser.entrypoint import parse_file
from pydantic import BaseModel

pytestmark = pytest.mark.contract

ROOT = Path(__file__).parents[2]
REFS = ROOT / "docs" / "refs.json"
BIBLIOGRAPHY = ROOT / "docs" / "references.bib"
PAGE = ROOT / "docs" / "references.md"
DECISIONS = ROOT / "docs" / "decisions"
MAKEFILE = ROOT / "Makefile"
CITATION_FILE = ROOT / "CITATION.cff"
METHOD = ROOT / "docs" / "method"

# A multi-valued string `Literal` on a public model is a choice someone makes and the
# documentation has to name, by the spelling they pass. `CHOICE_PAGES` says which page owes
# each one; `NOT_A_CHOICE` names the fields that are not a choice at all, and the reason,
# which is asserted non-empty so the entry cannot become a bare exemption. A field in
# neither fails by name, which is how a `Literal` added by a later feature becomes a
# decision rather than an omission -- the same partition the census uses for modules.
#
# One page per field, and it is the page that owns the caller-facing spelling. Mapping a
# field to a second page is what produced a duplicate definition of `split` here once: the
# contract was already stated on the input page, and satisfying the map wrote it again.
CHOICE_PAGES = {
    "DistanceConfig.compressor": "docs/method/ncd.md",
    "RunConfig.compressor": "docs/method/ncd.md",
    "DelimitedDatasetInput.split": "docs/input-contract.md",
    "DelimitedSource.split": "docs/input-contract.md",
    "ResourceEstimate.split": "docs/input-contract.md",
    "RunConfig.split": "docs/input-contract.md",
    "SpreadsheetDatasetInput.split": "docs/input-contract.md",
    "SpreadsheetSource.split": "docs/input-contract.md",
    "ResourceEstimate.source_kind": "docs/input-contract.md",
    "RunConfig.source_kind": "docs/input-contract.md",
    "RunInput.kind": "docs/input-contract.md",
    "PartitionFile.method": "docs/dataset-partitioning.md",
    "PartitionFile.end": "docs/dataset-partitioning.md",
    "RunReport.status": "docs/artifacts.md",
}

NOT_A_CHOICE = {
    "NormalizationManifest.object_encoding": (
        "a consequence of the source rather than a choice: a dataset produces one encoding "
        "and a file corpus the other, and the manifest records which"
    ),
    "PartitionManifest.decimal": (
        "detected by falsification against every cell, never passed; a column two separators "
        "could explain is refused rather than resolved"
    ),
    "TreeNode.kind": "structural -- a node is a leaf or it is not -- rather than a choice",
    "RunManifest.status": (
        "how far the run got, written by the run and read by resume; nobody passes it, and "
        "a state the pipeline never reaches is unreachable rather than undocumented"
    ),
    "StageReceipt.status": (
        "the same recorded progress at stage granularity: a receipt is open or it is closed"
    ),
}

# `[cilibrasi2005]` names a published work; `[adr-0001]` a decision this project made. The
# lookbehind excludes a subscript, which has the same shape and always follows a name, a `]`
# or a `)`.
CITATION = r"(?<![\w\])])\[@?((?:[a-z][a-z-]*)[0-9]{4}[a-z]?)\]"
REFERENCES_SECTION = r"^References\n-{3,}"

# An attribution to a published work declares whether anybody read that work against this
# code. Two states, one line, and a reason that cannot be empty: the honest state costs a
# sentence and the dishonest one costs a deliberate dated claim. Nothing here can tell a
# true `Read:` from a false one -- ADR 0019 records why the check stops at the declaration.
#
# A `[adr-NNNN]` basis carries no mark. The decision is in this repository, under version
# control, and reading it is not a separate act that could have failed to happen.
MARK = re.compile(
    r"^[ \t]*(?:Read: (?P<date>\d{4}-\d{2}-\d{2}), (?P<depth>primary|secondary)\."
    r"|Not read: (?P<reason>\S[^\n]*?)\.)[ \t]*$",
    re.MULTILINE,
)
# `.. [key]` opens an attribution in a docstring and `- [key]` in a `## Sources` list on a
# method page. One grammar, two places it can be written, because a page states claims the
# code does not.
DOCSTRING_ATTRIBUTION = r"\n(?=\.\. \[)"
PAGE_ATTRIBUTION = r"\n(?=- \[)"
SOURCES_SECTION = r"^## Sources$"

SOURCES = ("crossref", "openalex")

# The modules of the census that do not yet state the basis of what they compute.
#
# It exists so the gate is green from the first commit: a gate that is red by construction
# is one somebody switches off.
#
# What must never fall is the number of census modules that declare, not the size of this
# list: it also grows when the census does, because a module entering the census without a
# basis has to be exempt or the gate is red on arrival. A module leaves it when its
# `References` section lands, and nothing mechanical stops a commit from re-exempting one
# that already declared. That direction is a reviewer's judgement rather than a check,
# because the committed list is the only record of what it used to be.
#
# It is empty. `neighbor_joining` left it by stating its basis and marking both works
# unread, which is the honest state for a work nobody here can obtain -- not by somebody
# writing the sentence the empty set invites.
UNDECLARED: frozenset[str] = frozenset()


# The modules that are not scientific code: orchestration, configuration, error types, I/O
# and artifact plumbing, and the private test generator. They decide no result, so they owe
# no basis and are held to the ordinary coverage floor rather than the critical one.
#
# This list may grow, and that is the difference between it and `UNDECLARED` above. Most
# modules are not scientific; what matters is that the choice is made and written down, by
# whoever adds the module and knows the answer.
#
# Classification is by module name, so a name used in several packages is classified once.
# `api`, `config` and `errors` exist in five packages and are the same kind of thing in each.
# The census cannot work that way -- it resolves each name to exactly one file -- so a
# scientific module may not reuse a name that appears elsewhere.
NOT_SCIENTIFIC: frozenset[str] = frozenset(
    {
        "api",
        "artifacts",
        "cli",
        "config",
        "errors",
        "estimate",
        "generator",
        "manifest",
        "matrix",
        "models",
        "newick",
        "pipeline",
        "progress",
        "result",
        "scan",
        "shards",
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


def _references() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "references", ROOT / ".github" / "scripts" / "references.py"
    )
    if spec is None or spec.loader is None:
        pytest.fail(".github/scripts/references.py does not exist")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _attributions(path: Path, text: str) -> list[tuple[str, str]]:
    """Every attribution in `text`, as the key it names and the body that carries its mark.

    A docstring opens one with `.. [key]` under `References`; a method page with `- [key]`
    under `## Sources`. Both are read here so the mark has one grammar wherever the claim
    is written, and a key mentioned in running prose -- which asserts nothing about having
    read the work -- is not an attribution and is not required to carry one.
    """
    section, split = (
        (REFERENCES_SECTION, DOCSTRING_ATTRIBUTION)
        if path.suffix == ".py"
        else (SOURCES_SECTION, PAGE_ATTRIBUTION)
    )
    opened = re.search(section, text, re.MULTILINE)
    if opened is None:
        return []
    body = text[opened.end() :]
    if path.suffix != ".py":
        # A Markdown section ends where the next heading begins; a docstring section runs
        # to the end of the docstring.
        following = re.search(r"^#{1,6} ", body, re.MULTILINE)
        body = body[: following.start()] if following else body
    entries: list[tuple[str, str]] = []
    for part in re.split(split, body):
        opener = re.match(r"(?:\.\. |- )\[([^\]]+)\]", part.strip("\n"))
        if opener is not None:
            entries.append((opener.group(1), part))
    return entries


def _render_bibliography(refs: dict[str, dict[str, object]]) -> str:
    rendered: str = _references().render_bibliography(refs)
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
    r"""The lines that assign `CRITICAL_MODULES`, identified by what they are.

    Make has six assignment operators and accepts `export` and `override` before any of
    them, so enumerating them is a list of cases a Make release can extend. Every mention of
    the variable that is neither a comment nor an expansion of it is the axis instead.

    A `\` continuation is joined first, the way Make reads it. Scanning raw lines would see
    the first and drop the rest, so the census would silently be whatever fitted on one line.
    """
    joined = makefile.replace("\\\n", " ")
    return [
        line
        for line in joined.splitlines()
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


def test_every_module_is_scientific_or_is_declared_not_to_be() -> None:
    """Every module under `packages/*/src` is classified, so a new one is a decision.

    Nothing used to ask whether a module belongs in the census. `CRITICAL_MODULES` was read
    and never checked against the tree, so a scientific module nobody listed owed no basis
    and sat below no coverage floor with every gate green -- verified by adding one and
    watching the suite pass. The partition closes that: a module in neither list fails here,
    by name, in the commit that adds it, which is when its author knows which it is.
    """
    census = set(_critical_modules())
    present = {
        path.stem
        for path in ROOT.glob("packages/*/src/**/*.py")
        if path.stem != "__init__" and "__pycache__" not in path.parts
    }
    unclassified = sorted(present - census - NOT_SCIENTIFIC)
    assert not unclassified, "\n".join(
        [
            "these modules are in neither the census nor NOT_SCIENTIFIC:",
            *(f"  {name}" for name in unclassified),
            "  a scientific module joins CRITICAL_MODULES in the Makefile, and UNDECLARED",
            "  here until it states its basis; any other joins NOT_SCIENTIFIC.",
        ]
    )
    both = sorted(census & NOT_SCIENTIFIC)
    assert not both, f"classified as both scientific and not: {', '.join(both)}"
    gone = sorted(NOT_SCIENTIFIC - present)
    assert not gone, f"NOT_SCIENTIFIC names modules that no longer exist: {', '.join(gone)}"


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


def test_every_doi_the_citation_file_names_is_in_the_bibliography() -> None:
    """Citing the software and citing the method must not name different works.

    `CITATION.cff` lists what this software builds on, and `version_guard.py` already
    refuses a release whose `.cff` version disagrees with the packages and the changelog.
    What nothing covered is a work named there and absent from the bibliography. The DOI is
    what is compared, because it identifies the work; a title that differs in punctuation
    between the two files still names the same paper.
    """
    # Read as text rather than parsed: a DOI is one flat line in CFF, and the alternative
    # is a YAML dependency for one field. A DOI written as a multi-line block would escape
    # this, and none of the records uses that form.
    named = {
        match.group(1)
        for match in re.finditer(
            r'^\s*doi:\s*"?([^"\s]+)', CITATION_FILE.read_text(encoding="utf-8"), re.MULTILINE
        )
    }
    recorded = {str(entry["doi"]) for entry in _refs().values() if entry.get("doi")}
    missing = sorted(named - recorded)
    assert not missing, (
        f"CITATION.cff names works the bibliography does not record: {', '.join(missing)}\n"
        "  run: make ref DOI=<doi>"
    )


def test_a_surname_first_resolver_name_is_not_inverted() -> None:
    """OpenAlex mixes two name forms in one response, and only a comma tells them apart.

    These four strings are what it returned for the authors of `gronlund2017`. Read by the
    last whitespace-separated token alone, the last two produce people surnamed Stefan and
    Mingzhou, and the wrong name reaches `docs/references.bib`. No other check sees it: the
    bibliography and `docs/refs.json` agree because both carry the transform's output, and
    `references.py verify` re-fetches through the same transform and reports a match.
    """
    references = _references()
    rendered = {
        raw: references._name(*references._split_display_name(raw))  # pyright: ignore[reportPrivateUsage]
        for raw in ("Allan Grønlund", "Kasper Green Larsen", "Schneider, Stefan", "Song, Mingzhou")
    }
    assert rendered == {
        "Allan Grønlund": "Grønlund, A.",
        "Kasper Green Larsen": "Larsen, K. G.",
        "Schneider, Stefan": "Schneider, S.",
        "Song, Mingzhou": "Song, M.",
    }


def _string_choices() -> dict[str, tuple[str, ...]]:
    """Every multi-valued string `Literal` a public model declares, by `Model.field`.

    Every module of the five packages is walked, not the `__all__` of each one. A model
    reaches a user through an artifact it serializes as much as through an export, and the
    config a caller fills is not itself exported; reading `__all__` left nine of twenty
    such fields invisible, `RunConfig.compressor` and the ten run states among them.

    Two classes of the same name are one entry whose values are the union of theirs, so a
    copy of a model that drifts from its original shows up as a value no page names rather
    than as whichever copy was walked last.
    """
    import damicore
    import damicore_clusterizer
    import damicore_distance
    import damicore_normalizer
    import damicore_tree_builder

    def literals(annotation: object) -> Iterator[tuple[object, ...]]:
        if get_origin(annotation) is Literal:
            yield get_args(annotation)
            return
        for argument in get_args(annotation):
            yield from literals(argument)

    found: dict[str, tuple[str, ...]] = {}
    packages = (
        damicore,
        damicore_normalizer,
        damicore_distance,
        damicore_tree_builder,
        damicore_clusterizer,
    )
    for package in packages:
        modules = [package.__name__]
        modules += [
            info.name for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
        ]
        for module_name in modules:
            module = importlib.import_module(module_name)
            for name, model in vars(module).items():
                if name.startswith("_") or not isinstance(model, type):
                    continue
                if not issubclass(model, BaseModel) or model.__module__ != module_name:
                    continue
                for field, info in cast("dict[str, Any]", model.model_fields).items():
                    for values in literals(info.annotation):
                        if len(values) < 2 or not all(isinstance(v, str) for v in values):
                            continue
                        key = f"{name}.{field}"
                        merged = found.get(key, ()) + cast("tuple[str, ...]", values)
                        found[key] = tuple(dict.fromkeys(merged))
    return found


SPAN = re.compile(r"`([^`\n]+)`")


def _names(page: str, value: str) -> bool:
    """True when the page names `value` as a value rather than spelling it by accident.

    The value has to stand alone in an inline code span, or be the right-hand side of one
    (`` `split="columns"` ``). A bare word match let `delimited` pass on the sentence
    "always `,`-delimited UTF-8" and `xlsx` pass on the file extension `.xlsx`, neither of
    which tells a reader the value exists; `.` and `,`, were they ever mapped, would match
    any page in the repository. A word boundary is also unable to match a value ending in
    a non-word character, so it silently fails a `zstd-19` nobody could then document.
    """
    for span in SPAN.findall(page):
        text = span.strip()
        if text == value:
            return True
        key, separator, rest = text.partition("=")
        if separator and key.isidentifier() and rest.strip().strip("\"'") == value:
            return True
    return False


def test_every_choice_a_model_offers_is_classified() -> None:
    """A `Literal` is a choice someone makes or it is not, and the repository says which.

    Nothing used to ask. A feature could widen a `Literal` -- the next one does exactly
    that -- and the page describing the stage would go on listing the old values with every
    gate green, which is how documentation comes to promise what the code does not do.
    """
    choices = _string_choices()
    unclassified = sorted(set(choices) - set(CHOICE_PAGES) - set(NOT_A_CHOICE))
    assert not unclassified, "\n".join(
        [
            "these Literal fields are neither mapped to a page nor declared not a choice:",
            *(f"  {field} = {choices[field]}" for field in unclassified),
            "  in tests/architecture/test_citations.py, map it in CHOICE_PAGES to the",
            "  page that must name its values, or say in NOT_A_CHOICE why it is not a",
            "  choice someone makes.",
        ]
    )
    gone = sorted((set(CHOICE_PAGES) | set(NOT_A_CHOICE)) - set(choices))
    assert not gone, (
        f"classified fields that no longer exist: {', '.join(gone)}; drop each from "
        "CHOICE_PAGES or NOT_A_CHOICE in tests/architecture/test_citations.py"
    )
    unexplained = sorted(field for field, reason in NOT_A_CHOICE.items() if not reason.strip())
    assert not unexplained, (
        f"these fields are declared not a choice with no reason given: {', '.join(unexplained)}"
    )


def test_every_choice_is_named_on_the_page_that_owes_it() -> None:
    """The page names the value a caller passes, spelled the way they pass it.

    Prose describing the choice is not enough: a reader looking for `jenks` searches for
    `jenks`, and a feature that adds a value has to be forced to the page rather than
    trusted to remember it. `choices.get` rather than `choices[field]` so that a renamed
    field fails by name in the classification test instead of erroring here first.
    """
    choices = _string_choices()
    missing = [
        f"  {field}: {value!r} is not named in {page}"
        for field, page in sorted(CHOICE_PAGES.items())
        for value in choices.get(field, ())
        if not _names((ROOT / page).read_text(encoding="utf-8"), value)
    ]
    assert not missing, "\n".join(["these choices are documented nowhere:", *missing])


def test_every_method_page_names_a_basis() -> None:
    """A page that says what a stage computes states where that came from.

    These pages exist to be the sourced account of the method, so one without a citation is
    the defect they were written against rather than a page with a gap. The key has to
    resolve too, which `test_cited_keys_resolve` holds for every Markdown file; this adds
    that a method page may not be silent.
    """
    pages = sorted(METHOD.glob("*.md"))
    assert pages, "docs/method/ holds no pages"
    refs = _refs()
    silent = [
        str(page.relative_to(ROOT))
        for page in pages
        if not any(
            _resolves(key, refs) for key in re.findall(CITATION, page.read_text(encoding="utf-8"))
        )
    ]
    assert not silent, "\n".join(
        ["these method pages cite no basis for what they describe:", *(f"  {p}" for p in silent)]
    )


def test_every_rule_the_manifest_records_cites_a_basis_that_resolves() -> None:
    """A run directory carries the rules that produced it, so those keys must be real.

    `RUN_METHOD` is typed in Python, not in a docstring or a Markdown file, so it is the one
    place a citation key can be written by hand and reach a user. `test_cited_keys_resolve`
    does not read it. A typo there would be provenance pointing nowhere, in an artifact
    whose whole purpose is to still answer a year from now.
    """
    from damicore.manifest import RUN_METHOD

    refs = _refs()
    unresolved = [
        f"  {stage}: {rule['name']} cites [{key}]\n{_hint(key)}"
        for stage, rule in RUN_METHOD.model_dump().items()
        for key in rule["refs"]
        if not _resolves(key, refs)
    ]
    assert not unresolved, "\n".join(["the manifest would record unresolvable basis:", *unresolved])


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


def test_every_attribution_declares_whether_its_work_was_read() -> None:
    """An attribution to a published work says whether anybody read that work against this
    code, in the one grammar `MARK` accepts.

    The check is presence and shape, never truth: ADR 0016 was a resolved key, a real work
    and a wrong sentence, and every gate was green. What this makes impossible is the
    silent third state -- an attribution that neither claims a reading nor admits the lack
    of one -- which is what left thirteen of them indistinguishable before.
    """
    unmarked: list[str] = []
    for path, text in _citing_files():
        for key, entry in _attributions(path, text):
            marks = MARK.findall(entry)
            where = f"  {path.relative_to(ROOT)}: [{key}]"
            if key.startswith("adr-"):
                if marks:
                    unmarked.append(
                        f"{where} carries a read mark; a decision of this project is in the "
                        "repository and is not a work somebody reads against the code"
                    )
            elif len(marks) != 1:
                unmarked.append(
                    f"{where} carries {len(marks)} marks, not one\n"
                    "    write one of:\n"
                    "      Read: YYYY-MM-DD, primary.      (the work itself was opened)\n"
                    "      Read: YYYY-MM-DD, secondary.    (another work's account of it)\n"
                    "      Not read: <why not>.            (closed access, no copy, ...)"
                )
    assert not unmarked, "\n".join(["these attributions do not declare their state:", *unmarked])


def test_every_bibliography_entry_is_cited() -> None:
    """Nothing is in the bibliography that no unit names.

    `test_cited_keys_resolve` holds the other direction: a key that cites nothing real. An
    entry that nothing cites is the same defect inverted -- a bibliography asserting a
    lineage the code does not claim -- and it is how four works added for planned features
    came to sit here describing an implementation that has none of them.
    """
    cited = {key for _, text in _citing_files() for key in re.findall(CITATION, text)}
    orphans = sorted(set(_refs()) - cited)
    assert not orphans, "\n".join(
        [
            "these bibliography entries are cited by nothing:",
            *(f"  [{key}]" for key in orphans),
            "  cite the work where it is used, or remove the entry from docs/refs.json",
            "  and run `make references`",
        ]
    )


def test_generated_bibliography_matches_source() -> None:
    """Both generated views of `docs/refs.json` are held to it.

    The `.bib` is for a reader writing a paper; `references.md` is the page the site
    publishes, for a reader who met a `[key]` and wants the work. One source, two outputs,
    and neither may drift from it by hand.
    """
    refs = _refs()
    references = _references()
    for path, rendered in (
        (BIBLIOGRAPHY, references.render_bibliography(refs)),
        (PAGE, references.render_page(refs)),
    ):
        name = path.relative_to(ROOT)
        assert path.is_file(), f"{name} is missing; run `make references`"
        assert path.read_text(encoding="utf-8") == rendered, (
            f"{name} does not match docs/refs.json; it is generated, never edited. "
            "Run `make references`."
        )
