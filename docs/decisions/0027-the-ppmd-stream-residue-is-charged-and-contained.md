# ADR 0027: The PPMd stream residue is charged and contained

## The defects

pyppmd 1.3.1, the latest release and the range this project pins, has two leaks in its
encoder, both read in `src/ext/_ppmdmodule.c` of the 1.3.1 sdist on 2026-11-25:

- **The codec state.** `Ppmd8Encoder_init` allocates it with `PyMem_Malloc(sizeof(CPpmd8))`,
  and `Ppmd8Encoder_dealloc` frees the model it holds (`Ppmd8_Free`) but never the state
  itself. `sizeof(CPpmd8)` is 7,392 bytes, compiled from the 1.3.1 headers. `Ppmd7Encoder` has
  the same omission; both decoders free theirs.
- **The input.** `Ppmd8Encoder_encode` takes its data with the `y*` format and never calls
  `PyBuffer_Release`, so every object handed to `encode()` keeps a reference it never loses.
  Fed a fresh 64 KiB `bytes` object, each call left about 70 KB resident; the stage fed it a
  fresh chunk per read, so every stream left its whole input behind.

Measured on x86-64 Linux, Python 3.12, through `tracemalloc`, which sees both because they go
through Python's allocator: a stream of two 1 MiB objects left 2,105,600 bytes, its input and
its state. Resident memory confirmed the state alone at 7,405 to 7,411 bytes per encoder for
model memories from 4 KiB to 64 MiB and orders 2 to 16, untouched by `gc.collect()` or
`malloc_trim(0)`. Decoders leave nothing.

So every PPMd stream the distance stage compressed left its input and about 7.4 KB in the
process that opened it, for as long as that process lived. A run opens one stream per object
for its size and one per pair, and under the model check of ADR 0025 up to four per object and
three per pair. A run of 1,000 objects, the default limit on objects, under the check opens
about 1.5 million streams. Before this record a process kept everything it compressed; with
the input leak closed, it still keeps about 11.6 GB of codec state in one process, or 2.9 GB in
each of four workers, against a 512 MiB limit that preflight said the run fitted. The
repository's bounded-memory invariant was not holding for the default compressor, in 0.5 and
more so with the check.

## Decision

The input leak is closed where the stage feeds the codec, and what cannot be closed is
charged where memory is charged and contained where processes are owned.

- **Fed from a reused buffer.** Every compressor is fed from one buffer per chunk size, read
  into with `readinto` and kept for the life of the process, which outlives it anyway. A full
  piece is the buffer itself and only a stream's last piece is a view of it, so a stream keeps
  at most one 312-byte view, not its input; interleaved blocks are gathered into the same
  buffer. The bytes fed are unchanged, which the pairing's byte-for-byte test holds. Measured,
  a pair of 1 MiB objects leaves 7,704 bytes where it left 2,105,600.
- **Charged.** `PPMD_STREAM_RESIDUE_BYTES = 8_192` -- the state and the view, 7,704 bytes
  traced, rounded up to 8 KiB for the allocator's headers and the interpreter's own
  bookkeeping, which coverage tracing alone raises to 7,767 -- is charged per PPMd stream. Tests measure an encoder and a stream through
  `tracemalloc`, under both pairings and with and without a final view, and fail if either
  leaves more. If pyppmd stops leaking, they still pass and the charge is merely
  conservative.
- **Contained.** `DistanceConfig.stream_residue(object_count, pair_count)` plans the
  containment, and preflight and the stage both read it, so the charge and the behavior cannot
  disagree. Each worker's budget for residue is what the working-memory limit leaves after every
  worker's stream and chunks, shared among the workers. A pooled worker is replaced after as
  many tasks as that budget holds, by `ProcessPoolExecutor(max_tasks_per_child=...)`, which the
  standard library has offered since Python 3.11, the oldest this project supports. It is never
  replaced before one task, whose residue cannot be split. Without a declared limit the budget
  is 32 MiB per worker.
- **A single worker stays in the calling process** while the residue of the whole run fits its
  budget, as every single-worker run has until now. Only a run whose residue would outgrow the
  limit gets one worker process of its own, replaced the same way. That process is spawned, so
  a module-level script without a `__main__` guard is refused there with the message that
  already explains the guard.
- **The automatic shard size is capped at 2,048 pairs**, down from 10,000, so that one checked
  shard's residue (33.6 MB) fits what the default 512 MiB limit leaves each of four workers.
  A worker can then always be replaced between shards. The shard size is operational
  [adr-0022]: it changes the checkpoint layout, which this release already changes, and no
  value.

## Why this is the least that holds the invariant

Every run that fitted its limit is unchanged:

- it runs where it ran;
- a pool whose budget holds the run's whole residue is never replaced;
- replacement changes no value, which a test holds bit for bit.

Every run that did not fit now stays within its limit, at the cost of a replacement where the
budget runs out. A replacement costs 0.3 s, measured as the cold start of a spawned worker that
imports the stage (0.31 to 0.48 s over five starts). At the default limit and four workers a
worker is replaced every 2,048 checked pairs, about 0.15 ms a pair, against compressions that
cost more than that per pair at any object size where the residue matters.

## What was not chosen

- **Freeing the structure ourselves**, by reading the pointer out of the encoder object with
  `ctypes` before it is dropped. It depends on the private layout of one build of a C
  extension, and a wrong offset corrupts memory rather than failing.
- **Patching or vendoring pyppmd.** The project ships pure Python and the wheels of its
  declared dependencies, and those are closed.
- **Replacing a worker after every task.** Measured at 6.1 s for 20 tasks against 0.6 s
  without replacement: ten times the cost, to bound what the budget already bounds.
- **Refusing runs whose residue outgrows the limit.** Those are exactly the runs the
  containment lets finish within it.
- **Leaving it uncharged.** That reports a run as fitting a limit it then exceeds.
- **Copying each piece into fresh `bytes` before `encode()`.** That is what leaked the input.

## Status

Added in 0.6.0. The fixes upstream are a line each: `PyMem_Free(self->cPpmd8)` and
`PyMem_Free(self->cPpmd7)` in the encoders' `dealloc`, and `PyBuffer_Release(&data)` before
each encoder's `encode` returns. When a pyppmd release carries them, the measurement tests
still hold, and a new record can retire the containment and the pin's lower bound together.
