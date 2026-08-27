# ADR 0011: Dead schema fields wait for a breaking release

Three fields in the published 0.2 schemas carry no information and are deliberately kept.

- `RunReport.warnings` and `RunManifest.warnings` are written as `[]` by `run()` and nothing
  ever appends to them.
- `ResourceEstimate.tree_workspace_bytes` is assigned `matrix_bytes` verbatim, so it is equal
  to another field of the same model by construction.
- `estimate(keep_normalized=...)` is accepted, documented, and discarded with `del`.

Removing them is worth about eleven lines and cannot be done without a breaking release.

## Why removal breaks

0.2.0 is on PyPI, so completed run directories carrying these fields exist on users' disks.
`RunReport`, `RunManifest` and `ResourceEstimate` are all `extra="forbid"` with `strict=True`:
an unknown key is rejected, not ignored. Dropping a field therefore makes every 0.2.0 run
unloadable by the version that drops it. Measured, by producing a run and then removing the
fields:

```
ArtifactValidationError: Could not load completed result
code: artifact_validation_error
```

The message names no cause, so the failure reads as a corrupt artifact rather than a version
mismatch, and the run is not cheap to reproduce -- NCD is quadratic and Neighbor Joining
cubic in the object count.

## Rejected alternatives

**Bump to 0.3.0 now.** Eleven lines of inert schema do not justify making existing results
unreadable, nor the release itself: under `auto-tag.yml` a version change on `main` publishes
to PyPI irreversibly.

**Relax `extra="forbid"` to `extra="ignore"` on the read path.** This would make the removal
compatible by giving up the guarantee that an artifact is exactly what this version wrote.
`packages/damicore/tests/test_api.py::test_load_result_rejects_extra_manifest_fields` pins
that guarantee. Trading an integrity check for eleven lines is the wrong direction.

**Keep the fields but stop writing them.** They would need defaults to stay loadable, so the
models keep every line and only the artifacts shrink. It buys nothing.

## Revision condition

Remove all three in the next release that is already breaking for an independent reason.
That release raises `SCHEMA_VERSION` from 2 to 3 and states in its `### Breaking` section
that completed 0.2 runs cannot be loaded, as 0.2.0 did for 0.1.

The eight `RunReport` fields that restate `manifest.json` -- `object_count`, `pair_count`,
`effective_workers`, `csv_chunk_rows`, `compression_chunk_bytes`, `pairs_per_shard`,
`matrix_bytes`, `required_free_disk_bytes` -- are deliberately **not** in scope. They are
duplication on purpose: a report that cannot be read without opening the manifest beside it
is a worse artifact, not a tidier one.
