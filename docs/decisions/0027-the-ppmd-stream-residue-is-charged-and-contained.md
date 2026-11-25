# ADR 0027: The PPMd stream residue is charged and contained

## The defects

pyppmd 1.3.1, the latest release and the range this project pins, has three defects in its
encoder, read in the 1.3.1 sdist on 2026-11-25 and each measured:

- **It drops output bytes.** An `encode()` call writes its output into blocks, the first of
  32 KiB (`BUFFER_BLOCK_SIZE` in `src/lib/buffer/blockoutput.h`). It grows the output only
  between symbols, and `Writer` in `src/lib/buffer/Buffer.c` silently drops a byte that finds
  the block full (`// FIXME: When out buffer is full`). A symbol whose code crosses the end of
  a block loses its last bytes. A call coding more than 32 KiB can therefore produce a stream
  its own decoder rejects. 80,000 random bytes coded in one call, as the stage's 4 MiB chunks
  coded them, failed to decode in 4 of 40 seeds, each a byte short. So a PPMd size depended on
  `compression_chunk_bytes`, a setting declared not to change any value.
- **It keeps its input.** `Ppmd8Encoder_encode` takes its data with the `y*` format and never
  calls `PyBuffer_Release`, so every object handed to `encode()` stays alive. Fed fresh bytes,
  a stream left its whole input resident: 2,105,600 bytes for a pair of 1 MiB objects,
  measured with `tracemalloc`.
- **It keeps its state.** `Ppmd8Encoder_dealloc` frees the model (`Ppmd8_Free`) but never the
  `CPpmd8` that holds it, 7,392 bytes on x86-64, compiled from the 1.3.1 headers. Resident
  memory grew 7,405 to 7,411 bytes per encoder for model memories from 4 KiB to 64 MiB and
  orders 2 to 16, untouched by `gc.collect()` and `malloc_trim(0)`. `Ppmd7Encoder` has the same
  omission; the decoders free theirs.

A run opens one PPMd stream per object for its size and one per pair, and under the model
check of ADR 0025 up to four per object and three per pair. Under the check, a run of 1,000
objects, the default limit on objects, opens about 1.5 million streams. Before this record a
process kept everything it compressed. With the input leak closed, the run still keeps about
11.6 GB of codec state: in one process, or 2.9 GB in each of four workers. Preflight said the
run fitted a 512 MiB limit.

## Decision

The output and input defects are closed where the stage feeds the codec. The state, which
cannot be closed from here, is charged where memory is charged and contained where processes
are owned.

- **Bounded pieces.** Under PPMd each `encode()` call is handed at most
  `32,767 // (4 × (order + 2))` bytes, 1,023 at order 6. Read against `Ppmd8Enc.c`, a symbol
  is coded in at most `order + 2` range-coder steps: the first context, and an escape from each
  order below it. Each step emits at most 4 bytes as the 32-bit register shifts out. A piece's
  code therefore never fills the first output block, and nothing is dropped. The encoder is
  also guarded: a call returning 32 KiB or more raises `compression_error` rather than report
  a stream that may have lost bytes. The bound makes the guard unreachable. The piece size is
  operational: values no longer depend on `compression_chunk_bytes`, which still sizes the
  other compressors' pieces. Measured, the smaller calls cost within 5% either way: 1.96 s for
  4 MiB in 1,023-byte pieces against 2.02 s in one call, and 2.15 s against 2.06 s on a second
  run.
- **A reused buffer, per thread.** Every compressor is fed from one buffer per piece size, read
  into with `readinto` and kept for the life of the thread, which outlives it anyway. A full
  piece is the buffer itself, and only a stream's last piece is a view of it. A stream
  therefore keeps at most one 312-byte view, not its input. The buffer is per thread, so two
  streams compressed at once in one process never share one. Interleaved blocks are gathered
  into the same buffer, and the bytes fed are unchanged, which the pairing's byte-for-byte test
  holds. Measured, a pair of 1 MiB objects leaves 7,704 bytes where it left 2,105,600.
- **Charged.** `PPMD_STREAM_RESIDUE_BYTES = 8_192` is charged per PPMd stream: the state and
  the view, 7,704 bytes traced, rounded up to 8 KiB for the allocator's headers and the
  interpreter's own bookkeeping, which coverage tracing alone raises to 7,767. Tests measure an
  encoder and a stream through `tracemalloc`, under both pairings and with and without a final
  view, and fail if either leaves more. If pyppmd stops leaking, they still pass and the charge
  is merely conservative.
- **Contained.** `DistanceConfig.stream_residue(object_count, pair_count)` plans the
  containment, and preflight and the stage both read it, so the charge and the behavior cannot
  disagree. Each worker's budget for residue is what the working-memory limit leaves after
  every worker's stream and chunks, shared among the workers. Without a declared limit it is
  32 MiB. Every task declares the PPMd streams it opens, and a worker is replaced before a task
  would take its residue past the budget. It is never replaced before its first task, whose
  residue cannot be split, so it holds at most the larger of the budget and one task.
- **Not `max_tasks_per_child`.** Each worker is a single-process `ProcessPoolExecutor` of its
  own, replaced by shutting it down and starting another. The standard library's
  `max_tasks_per_child` hangs under CPython 3.11 and 3.12, 3.12.3 included: a retired worker is
  not replaced while tasks are queued. Measured, the stage built on it hung on 3.11.18 and
  3.12.3 and finished on 3.13.18. A standalone `map` of 10 to 120 tasks hung on 3.12.3 in four
  of five configurations here; a second machine finished it in 21 others, so the race is real
  but its window depends on timing. A task goes to a worker that can
  take it without replacement, the least busy of them. A worker is shut down only once the
  tasks it took have finished, and a dead worker still surfaces as the broken pool it always
  did.
- **A single worker stays in the calling process** while the residue of the whole run fits its
  budget, as every single-worker run has until now. Only a run whose residue would outgrow the
  limit gets one worker process of its own, replaced the same way. That process is spawned, so
  a module-level script without a `__main__` guard is refused there, with the message that
  already explains the guard.
- **The automatic shard size is capped at 2,048 pairs**, down from 10,000. One checked shard's
  residue (33.6 MB) then fits what the default 512 MiB limit leaves each of four workers, so a
  worker can always be replaced between shards. The shard size is operational [adr-0022]: it
  changes the checkpoint layout, which this release already changes, and no value.

## What changes, and why nothing is lost

- **Values.** This record changes a PPMd value only where pyppmd dropped a byte. The new value
  is the one the decoder accepts, where the old one was a stream it rejected. It changes no
  other value: measured against the code before it, every size under the other compressors and
  every PPMd size whose stream decoded is bit for bit the same. Against 0.5, values also move
  where ADR 0024's digest order reverses a pair.
- **Runs that fitted their limit** run where they ran. A pool whose budget holds the run's
  whole residue never replaces a worker, and replacement changes no value, which a test holds
  bit for bit.
- **Runs that did not fit** now stay within their limit, at the cost of a replacement where a
  worker's budget runs out. A replacement costs the cold start of a spawned worker that imports
  the stage: 0.22 to 0.48 s, measured on two machines. At the defaults a worker's budget holds one full
  shard of 2,048 checked pairs. A worker is therefore replaced about once per shard, about
  0.15 ms a pair, against the compressions that cost more than that at any object size where
  the residue matters. Tasks of one object each never force a replacement on their own.

## What was not chosen

- **Freeing the state ourselves**, by reading the pointer out of the encoder object with
  `ctypes` before it is dropped. It depends on the private layout of one build of a C
  extension, and a wrong offset corrupts memory rather than failing.
- **Patching or vendoring pyppmd.** The project ships pure Python and the wheels of its
  declared dependencies, and those are closed.
- **`multiprocessing.Pool(maxtasksperchild=...)`**, which replaces workers without the hang
  but waits forever on a worker the operating system killed, where the executor reports a
  broken pool.
- **Replacing a worker after every task.** Measured at 6.1 s for 20 tasks against 0.6 s
  without replacement, to bound what the budget already bounds.
- **Refusing runs whose residue outgrows the limit.** Those are exactly the runs the
  containment lets finish within it.
- **Leaving it uncharged.** That reports a run as fitting a limit it then exceeds.
- **Handing `encode()` fresh `bytes` per piece.** That is what leaked the input.

## Status

Added in 0.6.0. The fixes upstream are a line each:

- in `Writer`, grow the block instead of returning;
- `PyBuffer_Release(&data)` before each encoder's `encode` returns;
- `PyMem_Free(self->cPpmd8)` and `PyMem_Free(self->cPpmd7)` in the encoders' `dealloc`.

When a pyppmd release carries them, the measurement tests still hold, and a new record can
retire the containment, the piece bound and the pin's lower bound together. When the supported
Pythons all carry the fix for `max_tasks_per_child`, the worker pool can give way to it.
