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
    # Internal types that appear in a public signature. Each was dispositioned under #100,
    # and every one is kept internal: none is a concept a caller has to name, and the
    # information a reader actually needs -- the values, or the shape -- is already
    # documented where they go looking for it. The list stays explicit, one entry per type,
    # so the surface stays countable rather than hidden behind a pattern.
    #
    # Aliases for a `Literal` whose values a reader wants. The values are not in the
    # reference, and they do not need to be: `docs/input-contract.md` states the object
    # encodings, and `docs/dataset-partitioning.md` states the partition methods and ends,
    # beside the rule that decides which to pass. A caller writes the string, never the
    # alias.
    ("py:class", "ObjectEncoding"),
    ("py:class", "PartitionMethod"),
    ("py:class", "PartitionEnd"),
    # A union of three types the distribution already exports. `NormalizationConfig.source`
    # renders the alias, but a caller constructs `DelimitedSource`, `SpreadsheetSource` or
    # `FileCorpusSource`, each public and each documented. Only the convenience name is
    # unfollowable.
    ("py:class", "ObjectSource"),
    # Manifest payloads, and the two unions over them, discriminated on `kind`. A caller
    # reads these off a validated manifest and branches on `kind`, whose values --
    # `delimited`, `xlsx`, `files` -- are the source axis `docs/input-contract.md` and
    # `AGENTS.md` both describe. Exporting seven models so that two aliases resolve would
    # commit the project to names nobody should write.
    ("py:class", "NormalizationInput"),
    ("py:class", "PartitionInput"),
    ("py:class", "damicore_normalizer.manifest.DelimitedDatasetInput"),
    ("py:class", "damicore_normalizer.manifest.DelimitedPartitionInput"),
    ("py:class", "damicore_normalizer.manifest.SpreadsheetDatasetInput"),
    ("py:class", "damicore_normalizer.manifest.SpreadsheetPartitionInput"),
    ("py:class", "damicore_normalizer.manifest.FileCorpusInput"),
    # A protocol a caller implements, so its shape is what matters rather than its name.
    # `compute_distance_matrix` documents the call it makes, which is what an implementer
    # needs; the name would add a public commitment and tell them nothing more.
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
