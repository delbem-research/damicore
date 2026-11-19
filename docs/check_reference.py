"""Verify that the rendered API reference is exactly the public surface.

Autodoc imports whatever it is pointed at, so nothing in a Sphinx build distinguishes a
deliberate export from an implementation detail someone referenced by accident. This reads
the inventory the build produced and asserts three things about it: every symbol a
distribution lists in `__all__` is rendered, it is rendered exactly once across the whole
site, and no other `damicore*` object was rendered at all.

A symbol exported by more than one distribution is rendered once, where it is defined, and
referenced from the other page -- rendering it twice is a duplicate object description, which
the strict build already rejects. The check therefore matches on the symbol's own name rather
than on the distribution it was imported from.

This file lives in `docs/` rather than in the workspace because it is documentation tooling:
it depends on Sphinx, which is deliberately not a workspace dependency, and deleting this
directory must not change what any distribution installs.

Usage::

    python docs/check_reference.py docs/_build/html/objects.inv
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from sphinx.util.inventory import InventoryFile

PACKAGES = (
    "damicore",
    "damicore_normalizer",
    "damicore_distance",
    "damicore_tree_builder",
    "damicore_clusterizer",
)

# Splits a rendered name into its distribution, the name directly under it, and whatever
# follows. `damicore.run` has no tail and is a top-level object. `damicore.RunReport.save`
# has one and is a member of a rendered class, which is expected. The distinction matters
# because `damicore_clusterizer.fastgreedy.fastgreedy_membership` has a tail too, and is an
# implementation module rendered as public API -- which is the leak this file exists to catch.
QUALIFIED = re.compile(r"^(damicore[a-z_]*)\.([A-Za-z_][A-Za-z0-9_]*)(\..+)?$")

DEFAULT_INVENTORY = Path(__file__).parent / "_build" / "html" / "objects.inv"


def _exported() -> dict[str, set[str]]:
    exports: dict[str, set[str]] = {}
    for package in PACKAGES:
        module = importlib.import_module(package)
        exports[package] = set(getattr(module, "__all__", ()))
    return exports


def _python_objects(path: Path) -> dict[str, str]:
    """Every rendered Python-domain name, mapped to the page anchor it points at.

    The anchor is what distinguishes an alias from a leak. Autodoc inventories a class under
    both the name the directive used and the module where it is defined, so
    `damicore_normalizer.ObjectDescriptor` and `damicore_normalizer.manifest.ObjectDescriptor`
    are one rendered object with one anchor. A genuinely separate rendering has its own.
    """
    if not path.is_file():
        raise SystemExit(f"Sphinx inventory not found: {path}. Run `make docs` first.")
    loaded = InventoryFile.loads(path.read_bytes(), uri="")
    data = cast(dict[str, dict[str, Any]], loaded.data)
    objects: dict[str, str] = {}
    for object_type, entries in data.items():
        if not object_type.startswith("py:"):
            continue
        for name, entry in entries.items():
            objects[str(name)] = str(getattr(entry, "uri", ""))
    return objects


def _errors(path: Path) -> list[str]:
    objects = _python_objects(path)
    if not objects:
        return ["The inventory contains no Python-domain objects; the reference rendered nothing."]

    exports = _exported()
    every_export = {name for names in exports.values() for name in names}

    rendered: dict[str, list[str]] = defaultdict(list)
    public_anchors: set[str] = set()
    for qualified, anchor in sorted(objects.items()):
        match = QUALIFIED.match(qualified)
        if match is None or match.group(2) not in every_export:
            continue
        public_anchors.add(anchor)
        if match.group(3) is None:
            rendered[match.group(2)].append(qualified)
    public_anchors.discard("")

    unexpected = [
        qualified
        for qualified, anchor in sorted(objects.items())
        if qualified.startswith("damicore") and anchor not in public_anchors
    ]

    errors: list[str] = []
    for package, symbols in exports.items():
        missing = sorted(symbol for symbol in symbols if symbol not in rendered)
        if missing:
            errors.append(f"{package}: exported but not rendered: {', '.join(missing)}")

    twice = sorted(symbol for symbol, where in rendered.items() if len(where) > 1)
    for symbol in twice:
        errors.append(f"rendered more than once: {symbol} as {', '.join(rendered[symbol])}")

    if unexpected:
        errors.append("rendered but not exported by any distribution:")
        errors.extend(f"  {name}" for name in unexpected)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the rendered public API surface.")
    parser.add_argument("inventory", nargs="?", type=Path, default=DEFAULT_INVENTORY)
    arguments = parser.parse_args()
    errors = _errors(Path(arguments.inventory))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    total = sum(len(symbols) for symbols in _exported().values())
    print(f"reference renders the public surface exactly: {total} exports across {len(PACKAGES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
