# Releasing DAMICORE

The release procedure of record for maintainers. The workflows under `.github/workflows`
execute it; this file states the procedure and the facts that live outside the repository.

## Position

0.1.0 is published. All five distributions are on PyPI, and `v0.1.0` is tagged. A version
on PyPI is immutable and its number can never be reused, so the next release is a new
version — not a rebuild of this one.

`main` has since moved past `v0.1.0` while still declaring `0.1.0`, so two things are true
until the next version bump, and neither makes any check fail:

- Merging changes releases nothing. `auto-tag.yml` finds `v0.1.0` already on the remote,
  prints `nothing to release`, and exits 0. A green run is not a release.
- `make build` produces wheels labelled `0.1.0` whose bytes differ from the `0.1.0` on
  PyPI. Nothing compares a local build against the index, so treat any artifact built from
  an unbumped `main` as a development build and never upload one.

Both close the moment the five versions and the changelog heading move to the next number.

## Trigger

**Changing the version on `main` is the act that publishes.** `auto-tag.yml` fires on every
push to `main`, and when it sees a version with no `vX.Y.Z` tag yet it verifies that version
against the five public `pyproject.toml` files and the `## X.Y.Z` changelog heading, pushes
the tag, and dispatches `release.yml` on it. From there the chain runs to an irreversible
PyPI upload with no further approval inside the repository. A merge that does not change the
version finds its tag already present and does nothing. Pushing the tag by hand runs the
same pipeline.

The only gate that can stand between a merge and an upload is a required reviewer on the
five `release-<project>` GitHub environments. That is repository settings, not a file in
this repository, so it cannot be reviewed in a pull request — confirm it deliberately. A
fully automatic flow needs no reviewers; adding them turns each publish into approval
clicks, two per release per distribution.

To release: set the five `pyproject.toml` versions, rename the `## Unreleased` changelog
heading to `## X.Y.Z`, run `uv sync --all-packages --group dev` so `uv.lock` records the new
versions (every CI job installs with `--locked` and fails on a stale lock), and merge.

## Gate chain (`.github/workflows/release.yml`)

1. Tag/version guard: fast check that the tag agrees with the declared versions.
2. Large-input benchmark: proves the resource budget at the tag commit.
3. Check, test, build: `make check`, `make test`, one build verified against the
   tag, `twine check`, changelog section extraction.
4. TestPyPI publish via Trusted Publishing.
5. Clean-environment smoke on Python 3.11–3.14: third-party dependencies are
   installed from PyPI using the built artifacts, then the five DAMICORE
   distributions are swapped for their pinned TestPyPI publications; the CLI and
   pipeline smoke run against that environment (notebook on 3.12 only).
6. PyPI publish via Trusted Publishing, same artifacts as step 4, in two stages: the
   four stage distributions, and then `damicore`, which pins them. The aggregate is
   never on the index before the packages it requires.
7. GitHub Release with the version's changelog section and `SHA256SUMS`.

## External preconditions (configure before the first tag)

Trusted Publishing "pending publisher" entries on both indexes, one per project
name, all with owner `delbem-research`, repository `damicore`,
workflow `release.yml` — 10 entries in total. PyPI keeps pending publishers
unique on (owner, repository, workflow, environment) regardless of project
name, so each project needs its own environment, `release-<project>`:

| Index | Project | Environment |
|---|---|---|
| pypi.org | damicore | release-damicore |
| pypi.org | damicore-normalizer | release-damicore-normalizer |
| pypi.org | damicore-distance | release-damicore-distance |
| pypi.org | damicore-tree-builder | release-damicore-tree-builder |
| pypi.org | damicore-clusterizer | release-damicore-clusterizer |
| test.pypi.org | damicore | release-damicore |
| test.pypi.org | damicore-normalizer | release-damicore-normalizer |
| test.pypi.org | damicore-distance | release-damicore-distance |
| test.pypi.org | damicore-tree-builder | release-damicore-tree-builder |
| test.pypi.org | damicore-clusterizer | release-damicore-clusterizer |

Additionally:

- The five `release-<project>` GitHub environments are created automatically the
  first time the publish jobs reference them; pre-create them in Settings →
  Environments only to attach required reviewers (each reviewed environment adds
  one approval per index to every release).
- The organization's actions policy must allow the pinned third-party actions:
  `pypa/gh-action-pypi-publish`, `softprops/action-gh-release`, `astral-sh/setup-uv`.
- The repository must be public, or the metadata URLs baked into the wheels 404.
- Runners need roughly 5 GiB of free disk for the large benchmark.

## After a failed run

- Re-running the workflow is safe: `skip-existing` makes uploads of files already
  on TestPyPI (or PyPI) a no-op instead of a failure. The corollary: an index
  never replaces a file it already holds, so after the first TestPyPI publish of
  a version, its bytes there are frozen even if a re-tag rebuilt them; compare
  the TestPyPI downloads against the run's `SHA256SUMS` when in doubt.
- **A re-run against an already-published version publishes nothing and still reports
  success.** Every upload skips, the smoke job validates the bytes the index already held,
  and the run goes green. A green release run is therefore not evidence that anything was
  published — check the version on PyPI itself. This is why fixing forward means a new
  version number, never a re-run of the old one.
- The four stage distributions publish as independent matrix legs with `fail-fast: false`,
  so one failing leg does not strand the other three. A failed leg does block `damicore`,
  which publishes only after all four have landed — during the 0.1.0 release the aggregate
  reached PyPI eleven minutes before `damicore-tree-builder`, and for those eleven minutes
  `pip install damicore` could not resolve. If a stage leg fails, fix its cause and re-run:
  the legs that landed skip, and the aggregate follows once all four are present.
- Artifacts are never rebuilt between TestPyPI and PyPI; both publish jobs upload
  the exact files the build job produced.
- A version that reached PyPI cannot be retracted; fix forward with a new patch version.
