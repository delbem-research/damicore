# Shared per-package workflow, included by every package Makefile so the commands AGENTS.md
# prescribes have one definition instead of one copy per package.

PACKAGE := $(notdir $(CURDIR))
WORKSPACE := $(CURDIR)/../..

.PHONY: dev check test clean

dev:
	uv sync --group dev

# Everything runs from the workspace root, scoped to this package by an explicit path,
# because two of the three tools resolve configuration against the directory they are
# invoked from. Pyright treats that directory as the project root and silently loses the
# workspace config's relative stubPath and extraPaths. Pytest makes it the rootdir and
# inherits no ini options from a parent, so a section had to be copied into every member
# just to serve this command; from the root there is one copy and one marker registry.
#
# Ruff is the exception and is deliberately left to resolve per file: its isort infers
# first-party packages from the `src` of the nearest configuration, which is what sorts a
# sibling distribution into the third-party block. It is listed here only so `check` runs
# the same three tools it always did.
check:
	cd $(WORKSPACE) && uv run ruff check packages/$(PACKAGE)
	cd $(WORKSPACE) && uv run ruff format --check packages/$(PACKAGE)
	cd $(WORKSPACE) && uv run pyright packages/$(PACKAGE)/src packages/$(PACKAGE)/tests

# The coverage target is the one pytest setting that is genuinely per-package, so it is
# passed here rather than stored in six copies. Exit status 5 means "no tests collected",
# which is not a failure for a package without any.
test:
	cd $(WORKSPACE) && uv run pytest packages/$(PACKAGE)/tests -v \
		--cov=$(PACKAGE) --cov-branch --cov-report=term-missing --cov-fail-under=90 \
		|| [ $$? -eq 5 ]

clean:
	rm -rf .coverage dist htmlcov .pytest_cache .ruff_cache
