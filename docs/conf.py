from __future__ import annotations

from importlib.metadata import version as distribution_version

# The site is versioned by the aggregate distribution. The five public distributions move in
# lockstep, which `tests/architecture/test_boundaries.py` asserts, so one of them names the
# release and the other four cannot disagree with it.
project = "damicore"
author = "DAMICORE contributors"
release = distribution_version("damicore")
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
]

master_doc = "index"
exclude_patterns = ["_build", "requirements.txt"]

# Every cross-reference must resolve. An unresolvable type in a signature is a documentation
# defect, not noise: it is how a rendered page comes to promise a symbol that does not exist.
nitpicky = True

# Constraint metadata Pydantic writes into a model's signature. These are not documentation
# targets in any project, including their own, so there is nothing to link to.
nitpick_ignore_regex = [
    (r"py:class", r"annotated_types\..*"),
    (r"py:class", r"pydantic\..*"),
    (r"py:class", r"_Pydantic.*"),
    # The constraint's value, not a type: `Field(ge=1.0)` renders `1.0` as a cross-reference.
    (r"py:class", r"[0-9][0-9._e+-]*"),
]

nitpick_ignore = [
    # Names autodoc renders as written in the annotation rather than fully qualified. The
    # object is real and documented; only the short form is unresolvable.
    ("py:class", "Path"),
    ("py:class", "NDArray"),
    ("py:class", "pd.DataFrame"),
    # Internal types that appear in public signatures. These are NOT documentation defects:
    # each one is a type a reader of the reference can see and cannot follow, because the
    # distribution does not export it. Narrowing or exporting them changes the public
    # surface, which `tests/architecture/test_boundaries.py` freezes, so it is a decision
    # rather than a docs fix. The list is kept explicit so the gap stays countable.
    ("py:class", "ObjectSource"),
    ("py:class", "ObjectEncoding"),
    ("py:class", "NormalizationInput"),
    ("py:class", "PartitionInput"),
    ("py:class", "PartitionMethod"),
    ("py:class", "PartitionEnd"),
    ("py:class", "damicore_normalizer.manifest.DelimitedDatasetInput"),
    ("py:class", "damicore_normalizer.manifest.DelimitedPartitionInput"),
    ("py:class", "damicore_normalizer.manifest.SpreadsheetDatasetInput"),
    ("py:class", "damicore_normalizer.manifest.SpreadsheetPartitionInput"),
    ("py:class", "damicore_normalizer.manifest.FileCorpusInput"),
    ("py:class", "damicore_distance.api.ProgressCallback"),
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
}

autodoc_member_order = "bysource"
autodoc_typehints = "signature"
# Only deliberate exports are documented. Autodoc will happily import an implementation
# module; `docs/check_reference.py` is what keeps one from being rendered as public API.
autodoc_default_options = {
    "undoc-members": False,
    # Pydantic's own machinery, not the model's contract. Excluding it is what keeps the
    # page about the fields a reader passes rather than about how validation is configured.
    "exclude-members": "model_config,model_fields,model_computed_fields,model_post_init",
}

myst_enable_extensions = ["colon_fence", "dollarmath"]
myst_heading_anchors = 3

html_theme = "furo"
html_title = f"DAMICORE {release}"
