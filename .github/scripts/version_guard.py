"""Assert that a version agrees with every public package, the changelog and both citations.

One definition for the two workflows that gate on it: release.yml's tag-guard checks the
version a tag names before the expensive chain runs, and auto-tag.yml checks the version
main declares before creating that tag at all. Failing here is cheap; failing later costs
the benchmark, or worse, leaves a tag pointing at sources that disagree with it.
"""

from __future__ import annotations

import re
import sys

import tomllib


def _cff_field(citation: str, field: str) -> str:
    """The value of a top-level `field:` in `citation`, or `""` unless it declares exactly one.

    Read by line rather than with a YAML library: this runs on the stock interpreter of the
    runner, before uv exists, which is what makes it the cheap gate. Only a top-level field
    is read, which is why the line must start at column zero.
    """
    values = [
        line.partition(":")[2].strip().strip("\"'")
        for line in citation.splitlines()
        if line.startswith(f"{field}:")
    ]
    return values[0] if len(values) == 1 else ""


def _cff_authors(citation: str) -> list[str]:
    """The family names `citation` lists as authors of this software.

    Only the top-level `authors:` block counts. The `references:` block further down lists
    other works and their authors, and indenting is what separates them: the block ends at
    the next line that starts at column zero.
    """
    names: list[str] = []
    inside = False
    for line in citation.splitlines():
        if line.startswith("authors:"):
            inside = True
            continue
        if inside and line and not line.startswith((" ", "\t")):
            break
        if inside and line.strip().startswith("family-names:"):
            names.append(line.partition(":")[2].strip().strip("\"'"))
    return names


def main() -> int:
    expected = sys.argv[1]
    packages = sys.argv[2:]
    mismatches: list[str] = []
    for package in packages:
        path = f"packages/{package}/pyproject.toml"
        with open(path, "rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        if version != expected:
            mismatches.append(f"{path}: version {version} != expected version {expected}")
    # The same cheap-mistake class as a version mismatch: the build job's changelog
    # extraction requires this exact heading, and discovering its absence there costs
    # the whole benchmark first.
    with open("CHANGELOG.md", encoding="utf-8") as handle:
        if f"## {expected}\n" not in handle.read():
            mismatches.append(f"CHANGELOG.md: no '## {expected}' section for this version")
    # Two copies state the version a citation refers to, and no packaging tool would notice
    # either drifting. Parsed by line and by pattern rather than with a YAML or Markdown
    # library: this runs on the stock interpreter of the runner, before uv exists, which is
    # what makes it the cheap gate.
    with open("CITATION.cff", encoding="utf-8") as handle:
        citation = handle.read()
    declared = _cff_field(citation, "version")
    if declared != expected:
        mismatches.append(
            f"CITATION.cff: version {declared or 'absent'} != expected version {expected}"
        )
    # The other one is the citation README.md tells a reader to copy. That file is the
    # repository's landing page on GitHub, beside the button that offers the same citation
    # from CITATION.cff, so a stale number is read by anyone who cites this work. Each
    # distribution ships its own README from its package directory, and none of them
    # carries a citation.
    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()
    cited = re.findall(r"\(Version ([^)]*)\)", readme)
    cited_year = re.findall(r"\((\d{4})\)\. \*DAMICORE\*", readme)
    if cited != [expected]:
        mismatches.append(
            f"README.md: citation version {cited or 'absent'} != expected version {expected}"
        )
    # The same citation states a year and a list of surnames, and both can go stale on their
    # own: a release in a new year, or an author added to CITATION.cff alone. The year is
    # compared with the release date the citation file declares. The surnames are checked for
    # presence, not for order or initials, because the README writes them in APA form and
    # the citation file in fields; presence is what catches an author who is in one and not
    # the other, which is the drift that happens.
    released = _cff_field(citation, "date-released")
    if released and cited_year != [released[:4]]:
        mismatches.append(
            f"README.md: citation year {cited_year or 'absent'} != "
            f"CITATION.cff date-released {released}"
        )
    absent = [name for name in _cff_authors(citation) if name not in readme]
    if absent:
        mismatches.append(
            f"README.md: the citation does not name {', '.join(absent)}, "
            "who CITATION.cff lists as an author"
        )
    if mismatches:
        print("\n".join(mismatches), file=sys.stderr)
        return 1
    print(
        f"all {len(packages)} package versions, the changelog and both citations "
        f"agree on {expected}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
