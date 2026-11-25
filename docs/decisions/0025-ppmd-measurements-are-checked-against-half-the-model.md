# ADR 0025: PPMd measurements are checked against half the model

ADR 0023 refuses a pair longer than the window its compressor declares, and exempts PPMd
because "the model's size is not the limit". That held for the data it was measured on and
not in general. PPMd variant I is driven with restart: when its model memory fills, it starts
again from nothing, and once that happens inside a pair, the rest of the pair is coded with
the first object forgotten and the pair measures closer to 1 than the two objects warrant --
the failure ADR 0023 exists to refuse, under the default compressor.

## Why ADR 0023 did not see it

Its generator draws one word per line from a 200-word vocabulary. Such data has few distinct
contexts, so the model barely fills: a 1 % perturbed copy reads the same distance under 1, 16
and 64 MiB of model, which is what the record reports. Data with many distinct contexts --
numbers with several decimals, identifiers, free text -- fills the model in proportion to its
size. How fast is a property of the content, which is why no byte count can be declared for
PPMd the way a window is declared for the others.

## What a restart costs a table

Columns that share a distribution, which is what columns of a table share, measured as the
distance to a column of another distribution minus the distance to one of the same; above 0
the groups are told apart:

| column | data | model 4 MiB | model 64 MiB (default) | model 256 MiB |
|---|---|---|---|---|
| 1 MiB | 200-word vocabulary | +0.160 | | +0.160 |
| 32 MiB | 200-word vocabulary | +0.016 | | +0.016 |
| 8 MiB | normal, six decimals | +0.001 | +0.080 | |
| 32 MiB | normal, six decimals | +0.0002 | +0.027 | +0.081 |

On the vocabulary the model's size changes nothing. On the numeric columns a model that fills
loses the separation a larger one keeps, and the default model has lost most of it at 32 MiB
per column. `PPMD8_RESTORE_METHOD_CUT_OFF`, which frees the least used contexts instead of
restarting, was measured on the same columns and does not keep it either (+0.0006 to +0.0036
under 4 MiB), so the restoration method stays as ADR 0020 set it.

## The memory a pair needs is a property of the pair

Two checks of one object at a time were built first and each failed a fresh-context
verification. Compressing every object again under a fraction of the model and refusing if a
size differed caught an object that fills the fraction alone, but not what a pair needs
together. At half the model, 33 of 726 pairs of passing objects restarted a 256 KiB model.
At a quarter, a search over dissimilar content found none, and then near-duplicates did:
under the default 64 MiB model, 1.4 MB of random bytes and a copy with 0.1 % of its bytes
changed each passed, and together restarted the model, so the run was admitted and reported
0.198 where a model that does not fill gives 0.006. PPMd builds the deeper contexts of shared
content only when the second object revisits it, so the more alike two objects are, the more
memory their pair needs -- and alike is what NCD exists to find. No fraction fixes that; the
check has to see each compressed stream the matrix uses.

## The check

Until the model fills, PPMd codes a stream identically whatever its memory, byte for byte: a
stream that half the model codes into the same bytes as all of it was not changed by a
restart under all of it, and its size is the objects' own -- the one any larger model gives.

Bytes, not sizes. A third verification found sizes alone coinciding across two restarts: 1,001
random bytes under an 8 KiB model and under its half both restart and both code 1,088 bytes,
where a model that does not fill codes 1,077, and comparing sizes admitted that run with a
wrong distance. The bytes of the two streams differ, and the stage compares their SHA-256.

So, under PPMd, the distance stage compresses every object under the model and under half of
it, on the same pool, before any pair; then every pair, under both, as it is computed. Half
the model coding a stream into other bytes does not yet mean the model restarted: the stream
may need more than half the model and no more than all of it. So such a stream gets a second
look: it is compressed once more under **twice** the model, and if that codes the bytes the
model did, the model did not restart on it -- its memory never mattered -- and the value
stands. Only a stream the model and twice the model code differently is refused, naming the
object or the pair, and asking for a larger `ppmd_memory_bytes`.

The second look runs only once no pair is in flight -- objects before the pairs, pairs after
the last shard -- so no worker holds a stream then, and the working-memory budget is the
look's, less what the workers still hold: the PPMd stream residue of ADR 0027, which preflight
charged against the same limit. That budget is `max_working_memory_bytes` --
`ResourceLimits.max_working_memory_bytes` through `run()`, whose default the stage shares, so a
caller who declares no limit gets the same look either way in -- less every worker's charged
residue. It runs as many streams of twice the model at once as that budget holds
and at most one per worker. Where the budget holds none, or twice the model is past what the
codec takes, a stream half the model codes otherwise is refused as before, and the refusal
names the limit; it is never measured on trust. Until a flagged pair's second look passes, its
shard is written and not completed, so an interruption before the look computes that shard
again rather than resuming it unchecked. The flagged pairs wait on disk, 40 bytes each in an
unnamed temporary file, which preflight charges for every pair: held in memory, they grew the
coordinator by about 490 bytes a pair, some 230 MiB at the default limit on pairs, beyond
anything preflight charged. `ResourceLimits.allow_model_restart`
(`--allow-model-restart`) measures anyway and skips the check; like every limit it is not part
of run identity [adr-0022]. The aggregate reports the refusal as `ResourceLimitError`, since it
is lifted the way the other limits are.

A test holds the guarantee where it can fail: for eight kinds of content, at sizes either side
of where a small model fills, with a near-duplicate and an unrelated object, every admitted
run returns bit for bit the matrix of a model sixteen times larger, and, with room for the
second look, every refused run holds a stream that the model itself codes into other bytes
than that larger model. Others hold the cases that broke earlier checks -- the near-duplicate
pair a per-object check admitted, the restart whose size coincided -- and the second look:
an object and a pair that need more than half the model and no more than all of it are
measured with the larger model's values, on one worker and on a pool alike; without the
memory for it they are refused naming the limit; and a shard interrupted before its look is
computed again.

What the check does not prove: if both the model and its half restart and still code the same
bytes, a wrong size is admitted. A fourth verification classified one search of 30,000 runs --
eleven kinds of content and twelve kinds of near-duplicate, orders 2 to 16, models from 4 to
128 KiB, pairs at a hundredth to three times the model -- under both comparisons. Comparing
sizes admitted 7,905 runs, 4 of them with a distance a 256 MiB model does not give; comparing
bytes admitted 7,882, none of them wrong, refused those 4, and refused no run whose half
model coded every stream as the 256 MiB model does. A targeted search for two restarts coding
the same bytes found none. That is a search, not a proof; pyppmd exposes no restart signal
that would close the case by construction. (Part of the search's content is the repository's
own documentation, so rerun on another revision its counts move by a few runs.)
`ncd_self_largest`, which is a diagnostic and never a gate, is not checked.

Preflight cannot apply the check: it needs the object bytes, which do not exist before
normalization, and a pair's need is only known by compressing it. So unlike the window gate,
the refusal comes after the run directory exists, and a pair can be refused after other shards
are written. That directory belongs to the refused call's configuration: running again with a
larger model is another configuration and needs another directory, as after any failed run,
while running again with the check lifted resumes it from the shards already done. The
reverse is not a resume of the same values: `allow_model_restart` is not checkpoint identity,
so the shard checkpoint lists the shards measured with the check lifted, and a call that
applies the check computes those again instead of inheriting them -- without that, a run
interrupted with the check lifted and resumed with it completed as checked while holding a
restarted near-duplicate pair. `manifest.json` keeps the limits of the call that created the
directory, so the distance stage's receipt records `ppmd_model_checked`, written by the call
that ran the stage, as the record of whether the check applied to the whole matrix. A call
that applies the check refuses a directory whose distance stage completed with it lifted,
whether the run would be reused or resumed past that stage: it would otherwise hand that call
a matrix the check never saw, under a receipt saying so but without a refusal.

Half of the model must itself be a model the codec takes, so `ppmd_memory_bytes` starts at
4,096, twice the codec's own minimum.

## What it costs

- **Time.** Every pair is compressed twice, so the pair stage, which dominates a run, takes
  about twice as long; the objects add two compressions each, and a stream that needs more
  than half the model one more under twice it.
- **No refusal the measurement did not need, where memory allows the second look.** Under
  the default model, pairs of unrelated objects need more than half of it from 2 MiB per
  object for random bytes and integers, and from 2 to 8 MiB for numbers with six decimals
  depending on their distribution; before the second look those were refused although they
  measured correctly, and now they are measured. Two such 2 MiB random objects under the
  defaults and two workers complete, checked, in about 10 seconds. Where the budget holds no
  stream of twice the model -- one worker under a limit within one model of what it is
  charged -- they are refused as before, and raising the limit or the model admits them with
  the same values.
- **Not the memory preflight charges.** The second look never runs beside a pair, and never
  more streams of twice the model than the budget left beside the workers' residue holds, so
  the look and the residue together stay within the limit preflight admitted the run under,
  which a test holds by counting the encoders each worker opens.

## What was not chosen

- **A margin per object**, at a half or a quarter of the model: refuted above, by pairs that
  fill the model together while each object fits.
- **Checking every stream against twice the model.** It refuses only streams that restart,
  but each worker then holds a stream of twice the model beside every pair: at the default
  64 MiB and the four workers `"auto"` resolves to, 544 MiB, past the default 512 MiB limit
  that ADR 0021 keeps every compressor's defaults inside. The second look takes the same
  answer only for the streams half the model flags, and only once the pairs are done.
- **A declared window for PPMd**, derived from the model memory. Any byte count conservative
  enough for random data refuses vocabulary columns that measure correctly at 32 MiB, and any
  count that admits those admits numeric columns that do not.
- **Recording the restart without refusing.** A run that measured nothing would still look
  like one that worked, which is what ADR 0023 refuses for every other compressor.
- **A larger default model.** It moves the limit without detecting it, and multiplies the
  memory preflight charges every worker.

## Status

This revises ADR 0023's exemption of PPMd, not its gate: the windowed compressors are gated
as before, and PPMd is now checked by measurement instead of not at all. Runs under PPMd in
which an object or a pair restarts the model, which completed under 0.5 with a distance that
was not the objects', are refused until the model is raised or the check is lifted; so are
those that need more than half the model where the budget holds no second look. Interleaved
pairing (ADR 0026) is checked like any other stream.

Skipping the check for streams too short to fill half the model would remove its time cost
without weakening it, if the shortness could be proved. Read against `Ppmd8.c` in pyppmd 1.3.1
on 2026-11-25, the bound that can be proved is too loose to pay: the model writes one byte of
text per symbol into an eighth of its memory, and per symbol up to `ppmd_order` contexts gain
a state, each of whose state arrays may be reallocated at up to 128 units of 12 bytes, beside a
new context of one unit -- 6 x 129 x 12 = 9,288 bytes per symbol at order 6. Half the default
model's unit area holds that for about 3,160 symbols, so only pairs of a few KB are proved safe, and those
cost the check microseconds. A tighter bound would count on freed arrays being reused, which
the allocator does not promise: a fragmented free list can fail an allocation with memory to
spare. So the check runs on every stream, and the time it costs is the price of a guarantee a
bound cannot give.
