"""Fetch bibliography entries, and render the bibliography from them.

``docs/refs.json`` is the single source of truth for every work this repository cites, and
nothing in it is ever typed: the only input is a DOI, and every field is copied from a
resolver response. ``docs/references.bib`` is generated from it.

The reason is the maintainer: an assistant asked to "add the reference" produces a
plausible, confidently-worded and entirely false citation at no cost, and an entry that
lands in the bibliography becomes the source of truth for every later session. Making the
DOI the only thing anyone types is what keeps a fabricated entry from being cheap.

It lives beside the other repository tooling rather than under `scripts/` because that
directory is the one `pyrightconfig.json` already covers, and new code has to meet the type
gate. `make ref` and `make references` are the interface; nobody types this path.

Usage::

    python .github/scripts/references.py add 10.1109/TIT.2005.844059 --today 2026-11-01
    python .github/scripts/references.py render
    python .github/scripts/references.py verify cilibrasi2005
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
REFS = ROOT / "docs" / "refs.json"
BIBLIOGRAPHY = ROOT / "docs" / "references.bib"

# Crossref asks for a contact address and serves a faster pool to requests that carry one.
USER_AGENT = "damicore-references/0.1 (https://github.com/delbem-research/damicore)"
TIMEOUT_SECONDS = 30

# The fields a bibliography entry must carry. Crossref is asked first and OpenAlex fills a
# gap in its answer, because Crossref returns no author list at all for the two Neighbor
# Joining records (old Oxford deposits) and no year for the USP thesis deposit, and an
# entry missing either is not a citation anyone can follow.
#
# `venue` is deliberately not required. A thesis has none in either resolver, and this
# project's own corpus is largely theses: a rule that is red on the works the repository
# exists to cite is a rule someone switches off.
REQUIRED = ("title", "authors", "year", "type")

# What a resolver's own type vocabulary means here. Anything else is `misc`, which renders
# without a venue field rather than inventing one.
TYPES = {
    "journal-article": "article",
    "article": "article",
    "proceedings-article": "inproceedings",
    "book-chapter": "incollection",
    "dissertation": "phdthesis",
    "report": "techreport",
}


def _get(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            parsed: dict[str, Any] = json.load(response)
            return parsed
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


# NFKD decomposes an accented letter into a base letter and a combining mark, which the
# ASCII encoding then drops -- so Araújo folds to Araujo. It does not touch a letter that is
# its own grapheme rather than an accented one, and dropping those silently turns Grønlund
# into "grnlund", a key that reads as a typo and invites the hand edit this design forbids.
_TRANSLITERATED = str.maketrans({"ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L"})


def _ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATED))
    return folded.encode("ascii", "ignore").decode("ascii")


def _name(given: str, family: str) -> str:
    initials = " ".join(f"{part[0]}." for part in given.split() if part)
    return f"{family}, {initials}".strip().rstrip(",")


def _split_display_name(display_name: str) -> tuple[str, str]:
    # shortcut: the last whitespace-separated token is the surname, which is wrong for
    # "van der Berg" and right for everything this repository cites. Upgrade when a
    # compound surname actually appears.
    parts = display_name.split()
    if len(parts) < 2:
        return "", display_name
    return " ".join(parts[:-1]), parts[-1]


def _present(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value}


def _from_crossref(doi: str) -> dict[str, Any]:
    payload = _get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
    if payload is None:
        return {}
    message: dict[str, Any] = payload.get("message") or {}
    raw_titles: list[Any] = message.get("title") or []
    raw_containers: list[Any] = message.get("container-title") or []
    titles: list[str] = [str(title) for title in raw_titles]
    containers: list[str] = [str(name) for name in raw_containers]
    issued: dict[str, Any] = message.get("issued") or {}
    parts: list[list[Any]] = issued.get("date-parts") or [[]]
    people: list[dict[str, Any]] = message.get("author") or []
    authors: list[str] = [
        _name(str(person.get("given", "")), str(person.get("family", "")))
        for person in people
        if person.get("family")
    ]
    entry: dict[str, Any] = {
        "title": titles[0] if titles else None,
        "authors": authors or None,
        "year": parts[0][0] if parts and parts[0] else None,
        "venue": containers[0] if containers else None,
        "type": TYPES.get(str(message.get("type")), "misc"),
        "source": "crossref",
    }
    return _present(entry)


def _from_openalex(doi: str) -> dict[str, Any]:
    payload = _get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}")
    if payload is None:
        return {}
    location: dict[str, Any] = payload.get("primary_location") or {}
    source: dict[str, Any] = location.get("source") or {}
    authorships: list[dict[str, Any]] = payload.get("authorships") or []
    names: list[str] = []
    for authorship in authorships:
        author: dict[str, Any] = authorship.get("author") or {}
        display_name = str(author.get("display_name") or "")
        if display_name:
            names.append(display_name)
    authors: list[str] = [_name(*_split_display_name(name)) for name in names]
    entry: dict[str, Any] = {
        "title": payload.get("title") or payload.get("display_name"),
        "authors": authors or None,
        "year": payload.get("publication_year"),
        "venue": source.get("display_name"),
        "type": TYPES.get(str(payload.get("type")), "misc"),
        "source": "openalex",
    }
    return _present(entry)


def fetch(doi: str) -> dict[str, Any]:
    """Resolve ``doi`` to a complete entry, or raise if no resolver can complete it.

    Crossref answers first. OpenAlex fills any required field Crossref left out, and the
    recorded ``source`` names whichever resolver supplied the title, so a later re-fetch
    starts where the entry came from.
    """
    entry = _from_crossref(doi)
    if any(field not in entry for field in REQUIRED):
        fallback = _from_openalex(doi)
        source = entry.get("source") if "title" in entry else fallback.get("source")
        entry = {**fallback, **entry, "source": source or "openalex"}
    missing = [field for field in REQUIRED if field not in entry]
    if missing:
        raise SystemExit(
            f"{doi} could not be resolved to a complete entry; missing {', '.join(missing)}. "
            "Record it as a project decision in an ADR instead."
        )
    return {"doi": doi, **entry}


def derive_key(entry: dict[str, Any], taken: set[str]) -> str:
    """Derive the citation key from the fetched metadata, never from what a caller typed.

    Deriving it is what makes two sessions converge on the same key for the same work.
    """
    surname = str(entry["authors"][0]).split(",")[0]
    stem = "".join(char for char in _ascii(surname).lower() if char.isalpha())
    base = f"{stem}{entry['year']}"
    if base not in taken:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        if f"{base}{suffix}" not in taken:
            return f"{base}{suffix}"
    raise SystemExit(f"too many works share the key {base}")


# Where a BibTeX entry of each type carries what this project calls the venue. A thesis is
# held by a school and a book by a publisher; rendering either as `booktitle` parses but says
# something false about the work.
VENUE_FIELD = {
    "article": "journal",
    "inproceedings": "booktitle",
    "incollection": "booktitle",
    "book": "publisher",
    "phdthesis": "school",
    "mastersthesis": "school",
    "techreport": "institution",
}


def _field(key: str, value: object) -> str:
    return f"  {key} = {{{value}}},\n"


def render_bibliography(refs: dict[str, dict[str, Any]]) -> str:
    """Render ``refs`` as BibTeX, deterministically, so the output is diffable."""
    chunks: list[str] = []
    for key in sorted(refs):
        entry = refs[key]
        kind = str(entry.get("type", "misc"))
        rendered = f"@{kind}{{{key},\n"
        rendered += _field("author", " and ".join(str(name) for name in entry["authors"]))
        rendered += _field("title", entry["title"])
        rendered += _field("year", entry["year"])
        venue = entry.get("venue")
        if venue:
            rendered += _field(VENUE_FIELD.get(kind, "howpublished"), venue)
        if entry.get("doi"):
            rendered += _field("doi", entry["doi"])
        chunks.append(rendered + "}\n")
    return "".join(chunks)


def _load() -> dict[str, dict[str, Any]]:
    if not REFS.is_file():
        return {}
    parsed: dict[str, dict[str, Any]] = json.loads(REFS.read_text(encoding="utf-8"))
    return parsed


def _save(refs: dict[str, dict[str, Any]]) -> None:
    ordered = {key: refs[key] for key in sorted(refs)}
    REFS.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _comparable(entry: dict[str, Any]) -> dict[str, Any]:
    # `source` and `fetched` are excluded on purpose: either resolver may legitimately have
    # answered, and `fetched` changes on every call by definition.
    return {field: entry.get(field) for field in REQUIRED}


def add(doi: str, today: str) -> str:
    refs = _load()
    entry = fetch(doi)
    for key, existing in refs.items():
        if existing.get("doi") == doi:
            raise SystemExit(f"{doi} is already recorded as [{key}]")
    key = derive_key(entry, set(refs))
    refs[key] = {**entry, "fetched": today}
    _save(refs)
    BIBLIOGRAPHY.write_text(render_bibliography(refs), encoding="utf-8")
    return key


def changed_since(base: Path) -> list[str]:
    """The keys whose bibliographic fields differ from ``base``, which may not exist.

    This is what keeps continuous integration bounded: a pull request that adds one work
    re-fetches one DOI, and one that touches no work re-fetches nothing. Re-fetching the
    whole bibliography on every run would make a flaky resolver into a red gate, and a gate
    that is red by construction is a gate someone switches off.
    """
    previous: dict[str, dict[str, Any]] = {}
    if base.is_file():
        previous = json.loads(base.read_text(encoding="utf-8"))
    current = _load()
    return [
        key
        for key, entry in current.items()
        if _comparable(previous.get(key, {})) != _comparable(entry)
    ]


def verify(keys: list[str]) -> int:
    """Re-fetch ``keys`` and report any whose recorded fields no longer match the resolver."""
    refs = _load()
    failures = 0
    for key in keys:
        entry = refs.get(key)
        if entry is None:
            print(f"{key}: not in {REFS.relative_to(ROOT)}", file=sys.stderr)
            failures += 1
        elif str(entry.get("source", "")).startswith("adr:"):
            print(f"{key}: recorded as a project decision, nothing to re-fetch")
        elif _comparable(entry) != _comparable(fetch(str(entry["doi"]))):
            print(f"{key}: recorded fields do not match the resolver", file=sys.stderr)
            failures += 1
        else:
            print(f"{key}: matches the resolver")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add", help="fetch one DOI into docs/refs.json")
    adding.add_argument("doi")
    adding.add_argument("--today", required=True, help="the date to record, as YYYY-MM-DD")
    commands.add_parser("render", help="regenerate docs/references.bib")
    verifying = commands.add_parser("verify", help="re-fetch keys and compare")
    verifying.add_argument("keys", nargs="*")
    verifying.add_argument(
        "--since",
        type=Path,
        help="verify only the keys that differ from this earlier docs/refs.json",
    )
    arguments = parser.parse_args(argv)

    if arguments.command == "add":
        key = add(str(arguments.doi), str(arguments.today))
        entry = _load()[key]
        print(f"[{key}] {entry['title']} ({entry['year']}, {entry['source']})")
        return 0
    if arguments.command == "render":
        BIBLIOGRAPHY.write_text(render_bibliography(_load()), encoding="utf-8")
        return 0
    keys = list(arguments.keys)
    if arguments.since is not None:
        keys += changed_since(Path(arguments.since))
    if not keys:
        print("no bibliography entry changed")
        return 0
    return 1 if verify(keys) else 0


if __name__ == "__main__":
    raise SystemExit(main())
