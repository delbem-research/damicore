# Artifact contract

A completed run contains `manifest.json`, `report.json`, `distance.npy`,
`labels.json`, `tree.json`, `tree.nwk`, `membership.csv`, `clusters.json`, and
checkpoints. Diagnostics and normalized objects are optional according to the
run configuration.

JSON is UTF-8, two-space indented, sorted, finite-only, and LF terminated.
Artifacts are versioned individually rather than together: one that carries a
schema version declares it in its own payload as `schema_version`, and
`manifest.json` is the one `load_result` refuses to read a mismatch of. Internal
artifact paths are relative and contained in the run directory. Manifests and
small results use same-directory temporary files, `fsync`, and atomic
replacement. `load_result` verifies every declared size and SHA-256 before
returning a read-only matrix view.

`manifest.json` carries a `method` block naming the rule in force at each stage, so a run
directory says which variant of the method produced it without being asked to the release
that wrote it. One entry per stage — `objects`, `distance`, `tree`, `clusters` — each a
versioned name and the citation keys that are its basis, resolvable in `docs/refs.json`:

```json
"method": {
  "distance": {"name": "ncd-max/v1", "refs": ["cilibrasi2005"]},
  "tree": {"name": "neighbor-joining-q/v1", "refs": ["saitou1987", "studier1988", "adr-0017"]}
}
```

A rule is not a parameter. Which compressor and how many clusters stay in `config`, which
`config_hash` covers; the rule names the method those choices were made inside. The block
is optional and the schema version did not change for it, so a manifest written before it
reads back with `method` absent — which states that the run does not say which rules
produced it, rather than guessing. See
[ADR 0018](decisions/0018-method-provenance-in-the-manifest.md).

Only `completed` is a successful terminal state. `failed` and `interrupted`
reports remain diagnostic and may be resumed when their input, configuration,
schema, runtime fingerprint, receipts, and checkpoints remain compatible.

`partition.json`, written by `damicore_normalizer.partition_dataset` beside the
subset files it describes, is a user-facing artifact rather than an
inter-stage one: no stage reads it. It carries its own `schema_version` and
the `partition_rule` its files depend on, the input's path, size and SHA-256,
the column and the resolved decimal separator, the row count, and one entry
per emitted file with its method, parameter, end, rank interval, size and
SHA-256. It is written last, atomically, and its presence is what marks a
partition complete. The link from an emitted file to a later run is that
file's SHA-256, which the run manifest records as its input digest.
