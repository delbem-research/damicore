# Changelog

Each released version is one `## X.Y.Z` section. That heading format is a contract, not a
style: `.github/scripts/version_guard.py` refuses to tag a version whose section is missing,
and `release.yml` extracts the section verbatim as the GitHub Release body. A heading that
carries anything else, a date included, matches neither and fails the release.

## Unreleased

### Changed

- A run no longer writes `checkpoints/pipeline.json`. Stage receipts live in
  `manifest.json` under `stages`, which already carried them, so resume reads one file
  instead of two copies of the same state. A completed manifest no longer declares that
  artifact. Runs produced by 0.2.0 still load, and an incomplete one still resumes,
  because the receipts it resumes from were always in the manifest too.
- `DistanceResult` reports `ncd_min`, `ncd_max` and `ncd_out_of_range_count`. They are
  measured during the matrix validation pass the distance stage already performs, so the
  orchestrator no longer walks the matrix a second time to fill `report.json`. The
  reported values are unchanged.

## 0.2.0

### Breaking

- The input contract is a source axis rather than one CSV. `run` and `estimate` take
  `source_kind` (`delimited`, `xlsx`, or `files`), and `split`, `delimiter`, `encoding`,
  `sheet`, `recursive` and `include_hidden` apply only to the sources that define them. A
  setting that does not apply raises `ConfigurationError` instead of being ignored.
- `CSVFormatError` is now `DatasetFormatError`, with code `dataset_format_error`; it covers
  spreadsheets as well as delimited text.
- The normalization manifest and the run manifest are schema version 2. Their `input` block
  is a union discriminated on `kind`, and the run configuration hash changed, so existing
  run directories are not reused. Completed 0.1 runs cannot be loaded by 0.2.
- `damicore_normalizer.normalize_csv` remains for delimited datasets, but
  `materialize_objects` is the entry point for every source. `NormalizationConfig` now
  carries a `source` (`DelimitedSource`, `SpreadsheetSource`, or `FileCorpusSource`)
  instead of top-level `split`, `delimiter` and `encoding`.
- `ResourceEstimate` reports `source_kind` and `source_paths` in place of `csv_path`, and
  `split` is `None` for a files source.

### Added

- A `files` object source: pass files, a directory, or both, and each file becomes one
  object with its bytes unchanged. Objects may be binary. Files are ordered by relative
  POSIX path, labelled by it so basenames may repeat, identified by a digest over the whole
  ordered set, and copied into the run directory so it stays self-contained. An empty file,
  a symlink, a non-regular entry, a duplicate path, and a corpus of fewer than two files
  are each refused with a stable code.
- An `xlsx` dataset source reading `.xlsx` and `.xlsm` through openpyxl's read-only row
  iterator, so the bounded-memory invariant holds. The used range is trimmed to the real
  data rectangle, a workbook with several worksheets requires the name, and formulas are
  stored as text and never evaluated.
- `cell_text_rule` v1, recorded in the manifest: the total, engine-independent rule that
  turns typed cells into text. A `.csv` and an `.xlsx` of the same logical table produce
  identical object bytes.
- `object_encoding` in the normalization manifest (`json-lines/1` or `raw-bytes/1`), so a
  distance is attributable to the bytes it measured.
- `NormalizationManifest` in `damicore_normalizer`'s public surface. It was already the
  documented contract between stages, so validating `manifest.json` no longer means reaching
  past the package's public API to do it.
- `openpyxl>=3.1,<4` as a runtime dependency of `damicore-normalizer`.
- ADRs 0006-0010 recording the source axis, run self-containment, the named object
  encoding, the spreadsheet engine, and the cell-text rule.

### Unchanged

- Delimited object bytes are identical to 0.1, verified differentially across quoted cells,
  embedded newlines and tabs, Unicode, blank lines, and both splits. Delimited text already
  accepted any single-character delimiter, so `.tsv` and `.txt` are documented rather than
  added.
- Legacy `.xls` stays out of scope and raises a typed error naming the conversion.

## Unreleased

- Declare `numpy` in `damicore`, which imports it directly, and drop it from
  `damicore-clusterizer`, which never did. Installing `damicore-clusterizer` alone no
  longer pulls NumPy.
- Fix the stage examples on PyPI: the tree-builder and clusterizer examples now read the
  paths the previous stage actually writes, and the distance example carries the
  `if __name__ == "__main__":` guard its process pool requires.
- Correct the documented meaning of CLI exit status 4: it covers any failed stage, not
  only artifact validation.
- Document the public API. Every exported symbol now carries a docstring, and `run`,
  `estimate` and `load_result` document their parameters, returns and failure modes.

## 0.1.0

- Define the first public API for estimate, execution, loading, and results.
- Add canonical CSV normalization, exact resumable NCD, deterministic Neighbor
  Joining, and FastGreedy leaf clustering.
- Add versioned, hashed artifacts; resource gates; CLI; wheel and release gates.
