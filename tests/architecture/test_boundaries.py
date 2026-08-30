import ast
import json
import re
from collections.abc import Iterator
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


def _exported_names(package: str) -> set[str]:
    """The names a package's ``__init__`` lists in ``__all__``, read statically.

    `test_package_inits_only_re_export` already holds `__all__` to a literal list, so parsing
    it is exact rather than a best effort.
    """
    path = ROOT / "packages" / package / "src" / package / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        named = (target for target in node.targets if isinstance(target, ast.Name))
        if not any(target.id == "__all__" for target in named):
            continue
        value = node.value
        if isinstance(value, ast.List):
            return {
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError(f"{package} declares no `__all__` list")


def _assignment_targets(targets: list[ast.expr]) -> Iterator[ast.expr]:
    """Every target an assignment binds, with tuple and list unpacking flattened.

    `self.a = self.b = x` and `self.a, self.b = pair` bind public attributes exactly as a
    single target does, so a walker that only reads `targets[0]` checks less than it appears to.
    """
    for target in targets:
        if isinstance(target, ast.Tuple | ast.List):
            yield from _assignment_targets(list(target.elts))
        else:
            yield target


def _unannotated_public_attributes(path: Path, exported: set[str]) -> list[str]:
    """Public ``self.<name> = ...`` bindings in an exported class that declare no type.

    Reported as ``Class.name``. A name annotated at class level, or at the assignment itself,
    is already declared and is not reported; a `_`-prefixed name is not public.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bare: list[str] = []
    for klass in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        if klass.name not in exported:
            continue
        declared = {
            statement.target.id
            for statement in klass.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        }
        for initializer in klass.body:
            if not isinstance(initializer, ast.FunctionDef) or initializer.name != "__init__":
                continue
            for node in ast.walk(initializer):
                if not isinstance(node, ast.Assign):
                    continue
                for target in _assignment_targets(node.targets):
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and not target.attr.startswith("_")
                        and target.attr not in declared
                    ):
                        bare.append(f"{klass.name}.{target.attr}")
    return bare


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


def test_public_classes_annotate_the_attributes_they_expose() -> None:
    """py.typed makes the types a consumer's checker reads; inference does not carry that far.

    Pyright resolves a bare `self.x = ...` from this workspace, so `make check` passes with or
    without the annotation, and the gap is invisible here. A consumer reads the wheel through
    their own checker, which may resolve it another way -- `pyright --verifytypes` reports the
    attribute, and every class inheriting it, as partially unknown. `DamicoreError` is the base
    of every public error class, so one bare assignment there reached the whole hierarchy.
    """
    for package in sorted(PUBLIC):
        exported = _exported_names(package)
        source = ROOT / "packages" / package / "src" / package
        modules = [path for path in source.rglob("*.py") if "__pycache__" not in path.parts]
        assert modules, package
        for module in modules:
            bare = _unannotated_public_attributes(module, exported)
            assert not bare, f"{module}: {bare}"


def test_the_attribute_check_reads_every_binding_form_and_only_public_ones(
    tmp_path: Path,
) -> None:
    """The check above passes over the packages; that says nothing about what it can see.

    No `__init__` in the five packages chains or unpacks its bindings today, so the branch
    handling those forms runs against no input at all -- and coverage would not say so, since
    it measures the packages rather than this suite. A fixture is the only thing that keeps
    the branch honest: strip it, and every real module still passes while an attribute goes
    unreported. The exclusions are here for the same reason, in the same direction: a check
    that reported a `_`-prefixed or already-annotated name would be noise nothing measures.
    """
    module = tmp_path / "sample.py"
    module.write_text(
        "class Exported:\n"
        "    annotated: int\n"
        "    def __init__(self) -> None:\n"
        "        self.chained_a = self.chained_b = 1\n"
        "        self.unpacked_a, self.unpacked_b = (1, 2)\n"
        "        self.at_assignment: int = 3\n"
        "        self.annotated = 4\n"
        "        self._private = 5\n"
        "class NotExported:\n"
        "    def __init__(self) -> None:\n"
        "        self.unreported = 6\n",
        encoding="utf-8",
    )
    # Sorted, not in walk order: the payload is which attributes are reported, and pinning
    # the traversal order would specify the implementation instead of the contract.
    assert sorted(_unannotated_public_attributes(module, {"Exported"})) == [
        "Exported.chained_a",
        "Exported.chained_b",
        "Exported.unpacked_a",
        "Exported.unpacked_b",
    ]


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


def test_the_changelog_carries_one_unreleased_section_at_the_top() -> None:
    """`release.yml` reads a version's section up to the next `## `, so a second
    `## Unreleased` below a released heading truncates that release's notes.

    v0.2.0 shipped that way: a `## Unreleased` left in place when the release section was
    added above it stranded four entries, and the extraction stopped before them, so they
    reached no reader. One heading, and first, is what keeps that extraction total.
    """
    headings = [
        line
        for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings.count("## Unreleased") <= 1, headings
    assert "## Unreleased" not in headings[1:], headings


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

    Ruff resolves configuration per file, and its isort infers first-party packages from
    `src` **relative to the directory holding that configuration**. No section declares `src`
    explicitly; the location is the difference, which is why a member's copy is what sorts a
    sibling distribution into the third-party block rather than beside that member's own
    modules. Consolidating them to the root reorders the imports of every package -- measured,
    sixteen files. The copies must still agree on the rules themselves, which is all this
    checks.

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
    # Every copy equals the root's, so the rule set is one decision made in seven places for
    # a resolution reason rather than seven decisions.
    assert tools[reference]["ruff"] == root_ruff, (tools[reference]["ruff"], root_ruff)


# The three checks below close rules AGENTS.md states and nothing else enforced. They are
# here rather than in a file of their own because this module already owns the repository
# conventions that are mechanically decidable, and a second file would add its own marker,
# imports and ROOT for three functions.

# AGENTS.md forbids these as module or package names when a domain name exists. Checked as
# whole stems rather than substrings, so `file_corpus` and `tree_graph` are unaffected.
GENERIC_BUCKET_NAMES = frozenset(
    {"utils", "helpers", "common", "core", "services", "internal", "misc", "util", "shared"}
)


def _source_modules() -> list[Path]:
    return [
        path
        for directory in sorted((ROOT / "packages").iterdir())
        if (directory / "src").is_dir()
        for path in (directory / "src").rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_no_module_is_named_for_a_generic_bucket() -> None:
    """A bucket name tells a reader nothing about what the module owns, and it attracts
    unrelated code because anything can plausibly go in it.

    Directories are checked as well as files: a `utils/` package is the same defect one level
    up, and it is the shape this repository would reach for first, since every package is
    flat today.
    """
    modules = _source_modules()
    # Guards the discovery: an empty scan would make the assertion below vacuous.
    assert len(modules) > len(PUBLIC), len(modules)

    offenders = sorted(
        path.relative_to(ROOT).as_posix()
        for path in modules
        if path.stem in GENERIC_BUCKET_NAMES
        or GENERIC_BUCKET_NAMES & set(path.relative_to(ROOT).parts[:-1])
    )
    assert not offenders, offenders


def test_package_inits_only_re_export() -> None:
    """`__init__.py` runs on import of anything inside the package, so work placed there is
    work no consumer asked for and cannot avoid.

    A passive one holds imports, dunder assignments and a docstring. Anything else -- a call,
    a conditional import, a function or class definition, a try/except fallback -- is either
    a side effect at import time or logic that belongs in a named module. `damicore` assigns
    `__version__` from its own API, which is a dunder re-export and stays allowed.
    """
    inits = sorted((ROOT / "packages").glob("*/src/*/__init__.py"))
    # Guards the discovery: one per workspace member, so a truncated glob is visible.
    assert len(inits) > len(PUBLIC), [path.name for path in inits]

    offenders: list[str] = []
    for path in inits:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if targets and all(
                isinstance(target, ast.Name)
                and target.id.startswith("__")
                and target.id.endswith("__")
                for target in targets
            ):
                continue
            offenders.append(
                f"{path.relative_to(ROOT)}:{node.lineno} {type(node).__name__} is not a "
                "re-export, a dunder assignment or the docstring"
            )
    assert not offenders, offenders


def test_every_test_module_declares_a_registered_marker() -> None:
    """`--strict-markers` rejects a marker outside the registry; it does not require one.

    A module with no `pytestmark` at all is collected and passes, so it silently belongs to no
    suite and `-m unit`, `-m e2e` and the release lanes that select by marker all skip it.
    Only reading the module -- or this -- can tell. The registry is read from the root
    configuration rather than restated, so adding a marker there is the one edit needed.
    """
    options = cast(dict[str, object], _tool(ROOT / "pyproject.toml")["pytest"])["ini_options"]
    assert isinstance(options, dict)
    declared = cast(dict[str, object], options)["markers"]
    assert isinstance(declared, list)
    registered = {str(entry).split(":", 1)[0].strip() for entry in cast(list[object], declared)}

    modules = sorted(ROOT.glob("packages/*/tests/test_*.py")) + sorted(
        ROOT.glob("tests/*/test_*.py")
    )
    # Guards the discovery: an empty glob would make every assertion below vacuous.
    assert len(modules) >= 11, [str(path) for path in modules]

    offenders: list[str] = []
    for path in modules:
        found = re.search(
            r"^pytestmark\s*=\s*pytest\.mark\.(\w+)",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if found is None:
            offenders.append(f"{path.relative_to(ROOT)}: no pytestmark")
        elif found.group(1) not in registered:
            offenders.append(
                f"{path.relative_to(ROOT)}: marker {found.group(1)!r} is not registered"
            )
    assert not offenders, offenders
