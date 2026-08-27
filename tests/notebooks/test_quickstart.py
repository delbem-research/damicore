"""Executable notebook contract.

Every published notebook must execute cell by cell in a kernel that resolves ``damicore``
from installed wheels, never from the checkout. CI builds that kernel and names it in
``DAMICORE_NOTEBOOK_KERNEL``; the test refuses to substitute a weaker execution mode when
no such kernel exists, because flattening the cells into one script would stop exercising
the notebook display protocol and the per-cell failure boundaries Colab users depend on.

Cells tagged ``install`` are dropped: the kernel already has the distribution, and running
the tag's ``%pip install`` would reach the network and could replace it with the index's
copy. Every notebook therefore needs its installation isolated in a tagged cell, and needs
each remaining input to be redirectable to a local fixture through the environment.
"""

import os
import subprocess
from pathlib import Path

import nbformat
import pytest
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient
from synthetic_data import generate_csv

pytestmark = pytest.mark.notebook

ROOT = Path(__file__).parents[2]

# Generous enough for a cold kernel on a shared runner, bounded so a hung cell fails the
# test instead of consuming the whole workflow timeout.
CELL_TIMEOUT_SECONDS = 300


def _execute(name: str, kernel: str) -> None:
    # nbformat ships no py.typed, so its node objects are untyped; the ignores below are
    # confined to the calls that cross that boundary.
    notebook = nbformat.read(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        ROOT / "notebooks" / name, as_version=4
    )
    notebook.cells = [  # pyright: ignore[reportUnknownMemberType]
        cell
        for cell in notebook.cells  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if "install" not in cell.metadata.get("tags", [])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    ]
    NotebookClient(
        notebook,  # pyright: ignore[reportUnknownArgumentType]
        timeout=CELL_TIMEOUT_SECONDS,
        kernel_name=kernel,
    ).execute()


def _kernel() -> str:
    kernel = os.environ.get("DAMICORE_NOTEBOOK_KERNEL", "python3")
    if kernel not in KernelSpecManager().find_kernel_specs():
        pytest.skip(
            f"kernel {kernel!r} is not registered; run the notebook lane, which installs "
            "the built wheels into a clean environment and registers a kernel for it"
        )
    return kernel


def test_quickstart_executes_in_installed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _kernel()
    csv_path = generate_csv(
        tmp_path / "dataset.csv",
        rows=8,
        columns=3,
        clusters=2,
        seed=9,
    )
    monkeypatch.setenv("DAMICORE_NOTEBOOK_CSV", str(csv_path))
    # The kernel inherits this working directory, so the notebook cannot reach the checkout.
    monkeypatch.chdir(tmp_path)
    _execute("colab_quickstart.ipynb", kernel)


def test_self_analysis_executes_against_a_local_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-analysis notebook clusters DAMICORE's own modules, so its corpus is a
    checkout of this repository.

    Pointed at one through ``DAMICORE_SELF_ANALYSIS_REPO`` it neither clones nor installs,
    which is what lets the lane execute it with no network. The corpus is a throwaway clone
    of the working tree rather than the working tree itself, so a notebook cell cannot write
    into the checkout the suite is running from.
    """
    kernel = _kernel()
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(ROOT), str(checkout)],
        check=True,
    )
    monkeypatch.setenv("DAMICORE_SELF_ANALYSIS_REPO", str(checkout))
    monkeypatch.chdir(tmp_path)
    _execute("damicore_self_analysis.ipynb", kernel)
