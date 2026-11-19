# ADR 0022: Execution settings are not run identity

A run is named by `run_id`, a hash of the input digest, `config_hash` and the schema version,
and `config_hash` decides whether a directory belongs to a call: whether a completed run is
reused and whether an interrupted one is resumed. Until 0.4 every field of `config` entered
it, including `workers` after `"auto"` had been resolved from `os.cpu_count()`. The same input
and the same call therefore named a different run on a machine with another CPU count: a run
interrupted on one machine could not be resumed on another, and two completed runs with
bitwise-equal artifacts carried two identities.

`AGENTS.md` already draws the line: workers, chunking, sharding, scheduling and memory
strategy are operational, and must not redefine scientific identity. Identity is the place
that line becomes observable.

## The rule

`config_hash` covers every field of `config` except the execution settings: `workers`,
`csv_chunk_rows`, `compression_chunk_bytes`, `pairs_per_shard`,
`pandas_materialization_limit_bytes` and `limits`. Each changes cost, admission or recovery
and none changes an artifact, which is a checked claim, not an assumption:
`test_the_worker_count_does_not_change_any_artifact` pins it for every published artifact,
`test_chunked_compression_matches_a_direct_single_shot_stream` for the compression chunk,
`test_columns_are_canonical_and_chunk_independent` for the CSV chunk, and
`test_a_checkpoint_resumes_under_other_execution_settings` for the shard size. The limits only gate
preflight, which every invocation runs again with its own.

The code names what is excluded, not what enters, so a field added to `config` is identity
until somebody classifies it. Forgetting to classify one makes identity stricter, never looser.

`keep_normalized` and `save_diagnostics` stay in. They change no value, but they change which
durable artifacts the directory holds, and a reused directory that lacks the diagnostics the
caller asked for would be a silent omission.

## What is recorded

`config` in `manifest.json` still records every setting, as resolved by the invocation that
created the directory. A later invocation that resumes or reuses it does not rewrite them:
the directory's record is of its creation, and the operational values of a resume change
nothing the record could be used to check. The result `run()` returns honours the caller's own
`pandas_materialization_limit_bytes`; `load_result` honours the recorded one, since it has no
caller setting to honour.

## The distance checkpoint

`compute_distance_matrix` keys its checkpoints by the same rule, applied to what a checkpoint
stores. `workers`, `compression_chunk_bytes`, `resume` and `save_diagnostics` leave the
identity; `compressor`, with every setting of its model, stays because the stored sizes
depend on it; `pairs_per_shard` stays because completed shards are recorded by index in that
layout, so a checkpoint is meaningless under another one. A resume with another shard size while the
distance stage is incomplete is therefore refused as `checkpoint_mismatch_error` rather than
remapped. `resume` leaving the identity also means a run started with `resume=False` can now
be resumed, which it could not before.

## Cost

Every `config_hash` changes, so every existing run directory has a new identity: a completed
pre-0.4 run is recomputed into a new default directory rather than reused, and an interrupted
one cannot be resumed. With an explicit `output_dir` holding such a run, `run()` raises
`OutputDirectoryConflictError`. The schema version did not change, because no artifact's shape
did, and completed earlier runs still load with `load_result`.

## Rejected alternatives

**Hash the requested `workers` value instead of the resolved one.** `"auto"` would then agree
across machines, but `workers=2` and `workers=4` would still name two runs with one result.

**Exclude `workers` only.** Smaller, but it leaves the chunk and shard sizes splitting
identities that `AGENTS.md` already calls operational, and the next report would be about them.
