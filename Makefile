.PHONY: help install check test coverage-critical build wheels clean
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

# Modules held to the 95% critical-coverage floor.
CRITICAL_MODULES := serializer ncd neighbor_joining fastgreedy natural_breaks

DIST_DIR ?= dist

help:
	@echo "Workspace commands:"
	@echo "  install               Install all workspace dependencies and git hooks"
	@echo "  check                 Lint and type-check the workspace"
	@echo "  test                  Run all tests with coverage gates"
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
