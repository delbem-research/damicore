# Working on the documentation

This page is the operating manual for the site itself. It is written for whoever edits these
pages next, which in this repository is usually an assistant, so it states the rules that the
build enforces rather than describing them in general terms.

## Where things are

The whole site is `docs/`, and `docs/` *is* the Sphinx source directory. There is no
`docs/source/` layer: `quickstart.md`, `input-contract.md`, `artifacts.md`,
`scalability.md`, `dataset-partitioning.md`, `quality-drivers.md`, `releasing.md` and every
record under `decisions/` are pages of this site and ordinary Markdown on GitHub at the same
time. Editing one needs no build, and no file was moved or duplicated to make the site exist.

Documentation dependencies live in `docs/requirements.txt` and are deliberately outside the
`uv` workspace. They are not a package extra and not a dev-group entry, because
`tests/architecture/test_boundaries.py` asserts the runtime dependency set and deleting this
directory must not change what any distribution installs.

## Building it

```bash
make docs        # build the site into docs/_build/html
make docs-check  # build with warnings as errors, then verify the rendered API
```

`make docs-check` is what CI runs, and it is the one that matters. It builds with `-W`, so
**every warning fails the build**, and `nitpicky` is on, so an unresolvable type in a
signature is a warning. That combination is deliberate: a cross-reference that does not
resolve is how a rendered page comes to promise a symbol that does not exist.

Two consequences worth knowing before you are surprised by them:

- **every page must appear in exactly one `toctree`.** A new file that nothing includes is a
  warning, and a warning is a failure. Add it to `index.md` or to the relevant section page
  in the same change that creates it.
- **a type Sphinx cannot resolve must be fixed, not silenced.** `nitpick_ignore` in
  `conf.py` exists for third-party objects that are genuinely not Python-domain targets, and
  each entry there should be one you can justify. Adding to that list to get a green build is
  the same mistake as weakening a test.

## The API reference is generated, and guarded

`docs/reference/` documents exactly what each distribution lists in `__all__`. Autodoc will
import any module it is pointed at, so nothing stops an implementation detail from being
rendered as public API except `docs/check_reference.py`, which reads the inventory Sphinx
produced and asserts three things: every exported symbol is rendered, it is rendered exactly
once, and no non-exported `damicore*` object was rendered at all.

A symbol exported by more than one distribution is described once, where it is defined, and
referenced from the other page. `DistanceMatrixView` is the current instance. Do not add a
second `autoclass` for it: Sphinx reports a duplicate object description, and that is a
warning, and a warning is a failure.

When you add, remove or rename a public symbol, the reference page for that distribution
changes in the same commit. The check will tell you if you forget, but it tells you after the
build, and the build is the slow part.

## Adding a page

1. Write it in Markdown under `docs/`, with MyST syntax only where it buys something a
   cross-reference, a directive or a generated object.
2. Put it in a `toctree`, in the section it belongs to.
3. Run `make docs-check`.

Prose about what a stage computes belongs in a page of its own, not in the reference, which
is generated from docstrings and should stay that way.

## Citations

Published sources are cited by key, and the bibliography is fetched rather than written:
`make ref DOI=…` adds an entry to `docs/refs.json`, and `docs/references.bib` is generated
from it. Never edit either by hand. A basis with no published work cites its decision record
instead, as `[adr-NNNN]`. ADR 0015 records why, and `tests/architecture/test_citations.py`
enforces it offline on every run of `make test`.

A resolved entry proves the work exists. It never proves the work supports the claim beside
it, and nothing here checks that: what an implementation does not take from its source is
stated in the unit, in prose, and is checked by a reader.

## Publication

`.github/workflows/docs-pages.yml` publishes to GitHub Pages on every push to the default
branch, running the same `make docs-check` first. `.readthedocs.yaml` describes the same
build for Read the Docs. Neither is a pull-request check; `make docs-check` in CI is.
