# Scalability and resource gates

For `n` objects, DAMICORE computes `n(n-1)/2` NCD pairs, stores an `n × n`
float64 matrix, uses another same-sized on-disk Neighbor Joining workspace, and
runs Neighbor Joining in cubic time. Memory maps avoid loading those matrices
as ordinary arrays; they do not change algorithmic complexity.

Preflight performs the run's own traversal without creating object files and
calculates exact object bytes plus conservative memory and disk bounds. For a
files source those object bytes include the copy the run makes of the corpus. Default limits cap
objects at 1,000, pairs at 500,000, and each matrix at 512 MiB. `estimate`
always returns its ordered violations. `run` turns any violation into
`ResourceLimitError`; free disk can never be bypassed.

The supported large-input shape is many input bytes but a moderate object
count, typically columns. Splitting a large file into rows can create millions
of objects and is deliberately rejected, and so is a directory holding more
files than the object cap allows.

In that shape the cost is compression, not the object count. Every object is
compressed once and every pair once more, so the distance stage feeds its
compressor `n` times the object bytes: 50 columns of 20 MB are 50 GB of
compressor input. `estimate` reports that figure exactly as
`compression_input_bytes`, which a measured compressor throughput turns into
time, and `max_pair_bytes`, the longest concatenation the compressor must hold
for an NCD value to see across a pair. Neither is a gate. The work is split
into at least 64 shards whatever the object count, so every worker has work
even when a few large objects make few pairs.
