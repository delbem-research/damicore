.PHONY: help install check test coverage-critical build wheels clean ref references docs docs-check
.PHONY: print-public-packages print-stage-packages print-aggregate-package

# The publish allowlist. Deliberately explicit rather than derived from the workspace, so a
# new package cannot become publishable by accident. tests/architecture asserts this list
# matches the workspace members that are not marked private.
PUBLIC_PACKAGES := damicore_normalizer damicore_distance damicore_tree_builder damicore_clusterizer damicore

# The four stage packages, which own one pipeline stage each and must install alone.
STAGE_PACKAGES := $(filter-out damicore,$(PUBLIC_PACKAGES))

# The aggregate, derived rather than restated: it is the public package that is not a stage.
# It publishes last, because it depends on the four that are.
AGGREGATE_PACKAGE := $(filter-out $(STAGE_PACKAGES),$(PUBLIC_PACKAGES))

# Modules held to the 95% critical-coverage floor. This list is also the repository's census
# of scientific code: a module here owes a basis, by the rule in `AGENTS.md`. `tree_graph`
# decides the graph FastGreedy partitions and therefore the clusters; `compressor` owns the
# window every NCD value is a function of. `newick` and `numeric_column` are the two that
# still belong and are not here yet.
CRITICAL_MODULES := serializer ncd neighbor_joining fastgreedy natural_breaks tree_graph compressor

DIST_DIR ?= dist

help:
	@echo "Workspace commands:"
	@echo "  install               Install all workspace dependencies and git hooks"
	@echo "  check                 Lint and type-check the workspace"
	@echo "  test                  Run all tests with coverage gates"
	@echo "  ref DOI=<doi>         Fetch one work into docs/refs.json and regenerate the bibliography"
	@echo "  references            Regenerate docs/references.bib from docs/refs.json"
	@echo "  docs                  Build the documentation site into docs/_build/html"
	@echo "  docs-check            Build it strictly and verify the rendered public API"
	@echo "  build                 Build sdist and wheel of the five public distributions"
	@echo "  wheels                Build wheels only, for the smoke lanes"
	@echo "  print-public-packages Print the publish allowlist"
	@echo "  print-stage-packages  Print the allowlist without the orchestrator"
	@echo "  print-aggregate-package Print the orchestrator distribution alone"
	@echo "  clean                 Remove build artifacts and the shared .venv"

install:
	uv sync --all-packages --group dev
	uv run pre-commit install

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

test:
	uv run pytest --cov --cov-report=term-missing
	$(MAKE) coverage-critical

coverage-critical:
	@for module in $(CRITICAL_MODULES); do \
		echo "coverage-critical: $$module.py >= 95%"; \
		uv run coverage report --include="*/$$module.py" --fail-under=95 >/dev/null || \
			{ echo "critical coverage below 95% for $$module.py"; exit 1; }; \
	done

# The only thing anyone ever types is a DOI. Every field of an entry is copied from a
# resolver response, so a fabricated citation costs a DOI that must resolve rather than a
# plausible sentence. The date is passed in rather than read by the script, which keeps the
# script free of a clock and its output reproducible.
ref:
	@test -n "$(DOI)" || { echo "usage: make ref DOI=<doi>"; exit 2; }
	uv run python .github/scripts/references.py add "$(DOI)" --today "$$(date -u +%F)"

references:
	uv run python .github/scripts/references.py render

# Documentation dependencies stay out of the workspace: `--with-requirements` resolves them
# for this command only, so `docs/` can be deleted without changing what anything installs.
DOCS_RUN := uv run --with-requirements docs/requirements.txt

docs:
	$(DOCS_RUN) python -m sphinx -b html docs docs/_build/html

# `-W` is the gate: an unresolved cross-reference is how a page comes to promise a symbol
# that does not exist. check_reference.py then asserts the rendered API is exactly `__all__`.
#
# `-E` discards any saved environment. Without it Sphinx reuses one, skips the pages it
# considers current, and the consistency checks -- a page in no toctree among them -- do not
# re-run. The gate then passes or fails on whether `docs/_build` happens to be stale, which
# is not a gate.
docs-check:
	$(DOCS_RUN) python -m sphinx -E -W --keep-going -b html docs docs/_build/html
	$(DOCS_RUN) python docs/check_reference.py docs/_build/html/objects.inv

build:
	@set -e; mkdir -p $(DIST_DIR); \
	for package in $(PUBLIC_PACKAGES); do uv build --package $$package --out-dir $(DIST_DIR); done

wheels:
	@set -e; mkdir -p $(DIST_DIR); \
	for package in $(PUBLIC_PACKAGES); do uv build --package $$package --wheel --out-dir $(DIST_DIR); done

print-public-packages:
	@echo $(PUBLIC_PACKAGES)

print-stage-packages:
	@echo $(STAGE_PACKAGES)

print-aggregate-package:
	@echo $(AGGREGATE_PACKAGE)

clean:
	@echo "Remove .venv, dist, coverage, and test caches explicitly when needed."
