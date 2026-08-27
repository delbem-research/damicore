import ast
import json
import re
from pathlib import Path
from typing import cast

import damicore
import damicore_clusterizer
import damicore_distance
import damicore_normalizer
import damicore_tree_builder
import pytest
import tomllib
from damicore.api import VERSION

pytestmark = pytest.mark.contract

ROOT = Path(__file__).parents[2]
STAGES = {
    "damicore_normalizer",
    "damicore_distance",
    "damicore_tree_builder",
    "damicore_clusterizer",
}
PUBLIC = STAGES | {"damicore"}

# A workspace member carrying this classifier is private test infrastructure and must never
# be published.
PRIVATE_CLASSIFIER = "Private :: Do Not Upload"


def _project(package: str) -> dict[str, object]:
    with (ROOT / "packages" / package / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]


def _tool(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return tomllib.load(stream)["tool"]


def _version(package: str) -> str:
    return str(_project(package)["version"])


def _dependencies(package: str) -> set[str]:
    declared = _project(package)["dependencies"]
    assert isinstance(declared, list)
    return {str(dependency) for dependency in cast(list[object], declared)}


def _classifiers(package: str) -> list[str]:
    declared = _project(package).get("classifiers", [])
    if not isinstance(declared, list):
        return []
    return [str(entry) for entry in cast(list[object], declared)]


def _release(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# rglob, not glob: every package is flat today, so a top-level scan happens to see every
# module. The day one grows a subpackage, glob would keep passing while checking nothing.
def test_stage_packages_do_not_import_each_other_or_orchestrator() -> None:
    for stage in STAGES:
        source = ROOT / "packages" / stage / "src" / stage
        modules = [path for path in source.rglob("*.py") if "__pycache__" not in path.parts]
        assert modules, stage
        for module in modules:
            forbidden = (STAGES - {stage}) | {"damicore", "synthetic_data"}
            assert not (_imports(module) & forbidden), module


def test_orchestrator_has_no_runtime_dependency_on_synthetic_data() -> None:
    source = ROOT / "packages/damicore/src/damicore"
    modules = [path for path in source.rglob("*.py") if "__pycache__" not in path.parts]
    assert modules
    for module in modules:
        assert "synthetic_data" not in _imports(module), module


def test_public_exports_are_exact() -> None:
    assert damicore_normalizer.__all__ == [
        "materialize_objects",
        "normalize_csv",
        "NormalizationConfig",
        "DelimitedSource",
        "SpreadsheetSource",
        "FileCorpusSource",
        "NormalizationResult",
        "NormalizationManifest",
        "ObjectDescriptor",
        "NormalizerError",
    ]
    assert damicore_distance.__all__ == [
        "compute_distance_matrix",
        "DistanceConfig",
        "DistanceResult",
        "DistanceMatrixView",
        "DistanceError",
    ]
    assert damicore_tree_builder.__all__ == [
        "build_tree",
        "neighbor_joining",
        "TreeBuildConfig",
        "TreeBuildResult",
        "Tree",
        "TreeNode",
        "TreeEdge",
        "TreeBuilderError",
    ]
    assert damicore_clusterizer.__all__ == [
        "cluster_tree",
        "ClusterConfig",
        "ClusterResult",
        "ClusterizerError",
    ]
    assert set(damicore.__all__) == {
        "run",
        "estimate",
        "load_result",
        "DamicoreResult",
        "DistanceMatrixView",
        "ExecutionConfig",
        "ResourceLimits",
        "ResourceEstimate",
        "RunReport",
        "ArtifactPaths",
        "DamicoreError",
        "ConfigurationError",
        "InputValidationError",
        "DatasetFormatError",
        "ResourceLimitError",
        "OutputDirectoryConflictError",
        "CheckpointMismatchError",
        "NormalizationError",
        "CompressionError",
        "DistanceComputationError",
        "DistanceMatrixValidationError",
        "TreeBuildError",
        "TreeFormatError",
        "ClusterizationError",
        "ArtifactValidationError",
        "MaterializationError",
    }


# Every symbol the aggregate reaches for inside a stage package, rather than through that
# package's public surface. The set is closed, and this is the only thing that closes it: the
# distributions install independently and are pinned by range, so a stage may release a patch
# that moves one of these while `test_public_exports_are_exact` above stays green and every
# in-repo run keeps passing -- the workspace only ever resolves them together. Nothing here is
# a promise to users; it is a list of couplings that must not move quietly.
INTERNAL_STAGE_COUPLING = {
    # Preflight and the run share one traversal, so the projection is exact rather than
    # sampled. There is no public entry point for "measure without writing".
    "damicore_normalizer.api:scan_source",
    # The discriminated union of the source axis, carried as a config value.
    "damicore_normalizer.config:ObjectSource",
    # The progress protocol the distance stage calls back on.
    "damicore_distance.api:ProgressCallback",
}


def _deep_stage_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        head, _, tail = node.module.partition(".")
        if head in STAGES and tail:
            reached.update(f"{node.module}:{alias.name}" for alias in node.names)
    return reached


def test_the_aggregate_reaches_into_stage_internals_only_where_declared() -> None:
    """A public symbol is reached through its package; anything else is listed above.

    Two failures this catches, and they need different fixes. A symbol appearing here that is
    already exported means the import took the long way round and should go through the
    package. A genuinely new one means the aggregate grew a dependency on another
    distribution's internals, which is a decision -- publish it, or accept it here.
    """
    source = ROOT / "packages/damicore/src/damicore"
    modules = [path for path in source.rglob("*.py") if "__pycache__" not in path.parts]
    assert modules
    reached: set[str] = set()
    for module in modules:
        reached |= _deep_stage_imports(module)
    assert reached == INTERNAL_STAGE_COUPLING, sorted(reached ^ INTERNAL_STAGE_COUPLING)


def test_public_result_models_declare_the_specified_fields() -> None:
    """A published schema is part of the public API. These two models are the schemas of
    report.json and of the paths the CLI prints, so a field added or renamed here is a
    contract change and needs a version bump, not an edit to this list."""
    assert list(damicore.RunReport.model_fields) == [
        "status",
        "failed_stage",
        "object_count",
        "pair_count",
        "community_count",
        "cluster_count",
        "effective_workers",
        "csv_chunk_rows",
        "compression_chunk_bytes",
        "pairs_per_shard",
        "matrix_bytes",
        "required_free_disk_bytes",
        "peak_rss_bytes",
        "ncd_min",
        "ncd_max",
        "ncd_out_of_range_count",
        "negative_branch_count",
        "modularity",
        "timings_seconds",
        "verification",
        "warnings",
        "error",
    ]
    assert list(damicore.ArtifactPaths.model_fields) == [
        "run_dir",
        "manifest",
        "report",
        "distance_matrix",
        "labels",
        "tree_json",
        "tree_newick",
        "membership",
        "clusters",
        "normalization_dir",
        "diagnostics_dir",
    ]


def test_public_pyprojects_contain_no_workspace_paths_or_typer() -> None:
    for package in sorted(PUBLIC):
        text = (ROOT / "packages" / package / "pyproject.toml").read_text(encoding="utf-8")
        assert "tool.uv.sources" not in text
        assert "typer" not in text.lower()
        assert "click" not in text.lower()
        assert "file://" not in text


# PyPI freezes metadata per version: a missing key is not a fix, it is a version number. These
# fields are also the only ones nothing else in the repository reads, so without this they are
# unverified by construction. The assertions are about presence and shape, never the text of a
# field, so ordinary editing stays free.
REQUIRED_URLS = frozenset({"Homepage", "Repository", "Issues", "Documentation", "Changelog"})
SHARED_CLASSIFIERS = frozenset(
    {
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Topic :: Scientific/Engineering :: Information Analysis",
        "Typing :: Typed",
    }
)


def _urls(package: str) -> dict[str, str]:
    declared = _project(package).get("urls", {})
    if not isinstance(declared, dict):
        return {}
    return {str(key): str(value) for key, value in cast(dict[str, object], declared).items()}


@pytest.mark.parametrize("package", sorted(PUBLIC))
def test_every_public_package_carries_navigable_pypi_metadata(package: str) -> None:
    urls = _urls(package)
    assert REQUIRED_URLS <= set(urls), package
    # The project declares no routable mailbox, so Issues is the contact channel and every
    # link has to actually resolve as one.
    assert all(value.startswith("https://") for value in urls.values()), urls
    assert SHARED_CLASSIFIERS <= set(_classifiers(package)), package
    keywords = _project(package).get("keywords", [])
    assert isinstance(keywords, list)
    assert keywords, package


def test_every_public_package_ships_the_typing_marker_it_advertises() -> None:
    """`Typing :: Typed` is a claim; py.typed is what makes it true. Asserting the classifier
    without the file would let the distributions advertise types they do not deliver."""
    for package in sorted(PUBLIC):
        marker = ROOT / "packages" / package / "src" / package / "py.typed"
        assert marker.is_file(), package


def test_third_party_runtime_dependencies_are_exact() -> None:
    """The runtime dependency set and its ranges are closed; this test is what closes them.

    Each set is exactly what its distribution imports: a dependency reached only through
    another package is not declared, and one that is imported directly is, even when a
    sibling would have supplied it anyway. damicore imports numpy in api.py, so it declares
    numpy rather than relying on the stage packages; damicore_clusterizer builds its graphs
    through igraph alone, so it declares none.
    """
    expected = {
        "damicore_normalizer": {
            "openpyxl>=3.1,<4",
            "pandas>=2.2,<4",
            "pydantic>=2.10,<3",
        },
        "damicore_distance": {"numpy>=1.26,<3", "pydantic>=2.10,<3"},
        "damicore_tree_builder": {"numpy>=1.26,<3", "pydantic>=2.10,<3"},
        "damicore_clusterizer": {"igraph>=1.0,<1.1", "pydantic>=2.10,<3"},
        "damicore": {
            "numpy>=1.26,<3",
            "pandas>=2.2,<4",
            "pydantic>=2.10,<3",
            "tqdm>=4.66,<5",
        },
    }
    for package, dependencies in expected.items():
        third_party = {
            dependency
            for dependency in _dependencies(package)
            if not dependency.startswith("damicore-")
        }
        assert third_party == dependencies, package


def _optional_dependencies(package: str) -> dict[str, set[str]]:
    declared = _project(package).get("optional-dependencies", {})
    if not isinstance(declared, dict):
        return {}
    return {
        str(extra): {str(entry) for entry in cast(list[object], entries)}
        for extra, entries in cast(dict[str, object], declared).items()
        if isinstance(entries, list)
    }


def test_optional_dependency_extras_are_exact() -> None:
    """The extras are closed as well as the required set.

    The check above reads only `[project.dependencies]`, so an extra is invisible to it: one
    could be added, or silently widened, without any assertion noticing. damicore-distance's
    pandas extra is what makes head() and to_pandas() optional, so its range is a contract.
    """
    expected: dict[str, dict[str, set[str]]] = {
        "damicore_normalizer": {},
        "damicore_distance": {"pandas": {"pandas>=2.2,<4"}},
        "damicore_tree_builder": {},
        "damicore_clusterizer": {},
        "damicore": {},
    }
    for package, extras in expected.items():
        assert _optional_dependencies(package) == extras, package


def test_the_aggregate_requires_the_pandas_extra_of_the_distance_package() -> None:
    """`pip install damicore` has to bring pandas with it: the documented quickstart calls
    result.distance_matrix.head(). Depending on the bare distribution would leave that
    example raising at runtime while every wheel still resolved and installed cleanly."""
    version = _version("damicore")
    major, minor, _ = _release(version)
    ceiling = f"<{major}.{minor + 1}.0"
    assert f"damicore-distance[pandas]>={version},{ceiling}" in _dependencies("damicore")


def test_public_packages_declare_one_lockstep_version() -> None:
    """The five published distributions share one version.

    The version is restated a sixth time in code: `damicore.api.VERSION` is the value `run()`
    stamps into every manifest as `damicore_version`, which is mandatory run
    provenance. Asserting it here rather than in a test of its own keeps one check total over
    every statement of the released version, so a release bump cannot leave one behind.
    """
    versions = {package: _version(package) for package in sorted(PUBLIC)}
    assert len(set(versions.values())) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions["damicore"]), versions["damicore"]
    assert VERSION == versions["damicore"], (VERSION, versions["damicore"])


def test_orchestrator_pins_every_stage_within_the_lockstep_minor() -> None:
    """A published damicore must resolve stage packages of its own compatible release.

    The bound is asserted relative to the declared version rather than against a literal,
    so a release bump does not have to be mirrored here.
    """
    version = _version("damicore")
    major, minor, _ = _release(version)
    # During 0.x an incompatible change increments the minor, so
    # the compatible range is capped at the next minor rather than the next major.
    ceiling = f"<{major}.{minor + 1}.0"

    pinned: dict[str, str] = {}
    for dependency in _dependencies("damicore"):
        if not dependency.startswith("damicore-"):
            continue
        name, _, specifier = dependency.partition(">=")
        # An extra qualifies the requirement, not the distribution: damicore-distance[pandas]
        # is still the damicore-distance release this pin has to bound.
        name = name.partition("[")[0]
        floor, _, cap = specifier.partition(",")
        assert cap == ceiling, dependency
        assert _release(floor) <= _release(version), dependency
        pinned[name] = dependency

    assert set(pinned) == {stage.replace("_", "-") for stage in STAGES}


def test_public_packages_support_the_specified_interpreter_range() -> None:
    for package in sorted(PUBLIC):
        assert _project(package)["requires-python"] == ">=3.11,<3.15"


def test_publish_allowlist_matches_the_public_workspace_members() -> None:
    """The Makefile allowlist is what CI builds; drifting from the workspace is a defect.

    Keeping the allowlist explicit means a new package cannot become publishable by
    accident; this test means it also cannot be silently forgotten.
    """
    declared = re.search(
        r"^PUBLIC_PACKAGES\s*:=\s*(.+)$",
        (ROOT / "Makefile").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert declared is not None, "Makefile declares no PUBLIC_PACKAGES"

    publishable = {
        directory.name
        for directory in (ROOT / "packages").iterdir()
        if (directory / "pyproject.toml").is_file()
        and PRIVATE_CLASSIFIER not in _classifiers(directory.name)
    }
    assert set(declared.group(1).split()) == publishable == PUBLIC


def test_the_aggregate_publishes_only_after_the_stages_it_depends_on() -> None:
    """`damicore` requires the four stage distributions within its own release, so it must
    reach an index only once they are on it.

    Publishing all five as one matrix leaves them unordered and opens a window in which the
    aggregate is on the index and a stage it pins is not; `docs/releasing.md` records the
    0.1.0 release where that happened. Merging the two publish jobs back together would
    restore the window silently, because no other check reads the workflow.

    Parsed by hand rather than with a YAML library: PyYAML reaches this environment only as
    a transitive dependency of pre-commit, and depending on one of those undeclared is the
    same defect this suite exists to catch.
    """
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    blocks = dict(
        re.findall(
            r"^  ([a-z-]+):\n(.*?)(?=^  [a-z-]+:\n|\Z)",
            workflow,
            re.MULTILINE | re.DOTALL,
        )
    )
    assert {"publish-pypi", "publish-pypi-stages", "github-release"} <= blocks.keys(), sorted(
        blocks
    )
    aggregate_needs = re.search(r"^    needs:\s*(.+)$", blocks["publish-pypi"], re.MULTILINE)
    assert aggregate_needs is not None, blocks["publish-pypi"]
    assert "publish-pypi-stages" in aggregate_needs.group(1), aggregate_needs.group(1)
    # The stages must still be the matrix leg, or "after the stages" would mean one of them.
    assert "matrix:" in blocks["publish-pypi-stages"]
    assert "matrix:" not in blocks["publish-pypi"]


def test_type_check_configuration_covers_every_workspace_package() -> None:
    """Guard the type gate against silently checking nothing.

    `pyrightconfig.json` cannot carry comments, so the reasoning behind its shape lives here:

    - every `include` entry is a concrete path, because a mid-pattern wildcard such as
      `packages/*/src` matches nothing and Pyright then reports success over an empty file
      set, which is how this gate came to analyze zero files;
    - `exclude` is declared explicitly, because Pyright's default excludes every
      dot-directory and would otherwise put `.github/scripts` out of reach;
    - `reportPrivateUsage` is off, because unit tests deliberately exercise internals such as
      the stage-error translation table, and the public surface is asserted instead by
      `test_public_exports_are_exact` above.
    """
    configuration = json.loads((ROOT / "pyrightconfig.json").read_text(encoding="utf-8"))
    included = configuration["include"]
    assert isinstance(included, list)
    entries = {str(entry) for entry in cast(list[object], included)}

    for entry in sorted(entries):
        assert "*" not in entry, entry
        assert (ROOT / entry).is_dir(), entry

    for directory in sorted((ROOT / "packages").iterdir()):
        if not (directory / "pyproject.toml").is_file():
            continue
        for area in ("src", "tests"):
            if (directory / area).is_dir():
                relative = f"packages/{directory.name}/{area}"
                assert relative in entries, relative


def test_pytest_is_configured_once_for_the_whole_workspace() -> None:
    """The marker registry and the pytest flags exist at the root and nowhere else.

    Pytest resolves one configuration per invocation, from the rootdir, and inherits no ini
    options from a parent. A section in a member therefore used to be the whole registry for
    `make -C packages/<name> test`, which is why six copies existed and why a test had to
    hold them equal. `packages/package.mk` now runs pytest from the workspace root against an
    explicit package path, so the root section is the only one, and a member reintroducing
    one would silently take that member's suite off the shared registry again.
    """
    root = _tool(ROOT / "pyproject.toml")
    assert "pytest" in root, "the repository root declares no [tool.pytest.ini_options]"

    members = sorted(
        directory for directory in (ROOT / "packages").iterdir() if (directory / "src").is_dir()
    )
    # Guards the discovery: an empty scan would make the loop below vacuous.
    assert len(members) > len(PUBLIC), members
    for member in members:
        assert "pytest" not in _tool(member / "pyproject.toml"), (
            f"{member.name} redeclares [tool.pytest.ini_options]"
        )


def test_package_ruff_and_sdist_configuration_does_not_drift() -> None:
    """Two conventions still live in six copies, for different reasons, and neither is
    compared anywhere else.

    Ruff resolves configuration per file and infers first-party packages from the `src` of
    the nearest one, so a member's section is what sorts a sibling distribution into the
    third-party block rather than beside that member's own modules. Consolidating it to the
    root would silently reorder the imports of every package. The sections must still agree
    on the rules themselves.

    The sdist exclude list is the second: a member that quietly ships its `tests/` or
    `Makefile` again would surface only to a user unpacking the published sdist, which is the
    one artifact no other check here unpacks.
    """
    members = sorted(
        directory.name
        for directory in (ROOT / "packages").iterdir()
        if (directory / "pyproject.toml").is_file()
    )
    assert set(members) >= PUBLIC, members

    tools = {member: _tool(ROOT / "packages" / member / "pyproject.toml") for member in members}
    # The root's rules must reach the code outside `packages/` too: with no section there,
    # `tests/`, `benchmarks/` and `.github/scripts` fall back to Ruff's built-in defaults.
    root_ruff = _tool(ROOT / "pyproject.toml").get("ruff")
    assert root_ruff is not None, "the repository root declares no [tool.ruff]"

    reference = members[0]
    for member in members:
        assert tools[member]["ruff"] == tools[reference]["ruff"], member
        assert tools[member]["hatch"] == tools[reference]["hatch"], member
    # `src` is the one key that must differ, since it is what makes the inference per package.
    assert {key: value for key, value in tools[reference]["ruff"].items() if key != "src"} == {  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportAttributeAccessIssue]
        key: value
        for key, value in root_ruff.items()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportAttributeAccessIssue]
        if key != "src"
    }
