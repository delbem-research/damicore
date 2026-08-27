# Shared per-package workflow, included by every package Makefile so the commands AGENTS.md
# prescribes have one definition instead of one copy per package.

PACKAGE := $(notdir $(CURDIR))
WORKSPACE := $(CURDIR)/../..

.PHONY: dev check test clean

dev:
	uv sync --group dev

# Every tool runs from the workspace root, scoped to this package by an explicit path.
# Invoked from inside the package each of them would resolve configuration against the
# package instead, and each fails differently: Pyright treats it as the project root and
# silently loses the workspace config's relative stubPath and extraPaths, Ruff walks up to
# whichever pyproject declares [tool.ruff] first, and pytest makes the package its rootdir
# and inherits no ini options from the parent. Running from the root is what lets the
# workspace hold one copy of each tool's configuration.
check:
	cd $(WORKSPACE) && uv run ruff check packages/$(PACKAGE)
	cd $(WORKSPACE) && uv run ruff format --check packages/$(PACKAGE)
	cd $(WORKSPACE) && uv run pyright packages/$(PACKAGE)/src packages/$(PACKAGE)/tests

# The coverage target is the one setting that is genuinely per-package, so it is passed here
# rather than stored in six copies. Exit status 5 means "no tests collected", which is not a
# failure for a package without any.
test:
	cd $(WORKSPACE) && uv run pytest packages/$(PACKAGE)/tests -v \
		--cov=$(PACKAGE) --cov-branch --cov-report=term-missing --cov-fail-under=90 \
		|| [ $$? -eq 5 ]

clean:
	rm -rf .coverage dist htmlcov .pytest_cache .ruff_cache
