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
wrong distance -- 6 such runs in 7,628 admitted ones, across orders 2 to 16 and models from 4
to 128 KiB. The bytes of the two streams differ, and the stage compares their SHA-256.

So, under PPMd, the distance stage compresses every object under the model and under half of
it, on the same pool, and refuses the run before any pair if the bytes differ: every distance
to that object would hold it. Then every pair is compressed under half the model too as it is
computed, and the run is refused at the first pair whose bytes differ, naming the two
objects. The refusal asks for a larger `ppmd_memory_bytes`. `ResourceLimits.allow_model_restart`
(`--allow-model-restart`) measures anyway and skips the check; like every limit it is not part
of run identity [adr-0022]. The aggregate reports the refusal as `ResourceLimitError`, since it
is lifted the way the other limits are.

A test holds the guarantee where it can fail: for eight kinds of content, at sizes either side
of where a small model fills, with a near-duplicate and an unrelated object, every admitted
run returns bit for bit the matrix of a model sixteen times larger, and every refused run
holds a stream that half the model codes into other bytes than that larger model. Two more
hold the cases that broke earlier checks: the near-duplicate pair a per-object check admitted,
and the restart whose size coincided.

What the check does not prove: if both the model and its half restart and still code the same
bytes, a wrong size is admitted. The verification's search, rerun with bytes compared over the
same 30,000 runs, admitted 7,883 and refused 22,117: no admitted run differed from a 256 MiB
model, and every refused one held a stream half the model codes differently from it. That is
a search, not a proof; pyppmd exposes no restart signal that would close the case by
construction.
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
that ran the stage, as the record of whether the check applied to the whole matrix.

Half of the model must itself be a model the codec takes, so `ppmd_memory_bytes` starts at
4,096, twice the codec's own minimum.

## What it costs

- **Time.** Every pair is compressed twice, so the pair stage, which dominates a run, takes
  about twice as long; the objects add two compressions each.
- **Refusals within a factor of two.** A stream that needs more than half the model and no
  more than all of it measures correctly and is refused. Under the default model, pairs of
  unrelated objects were refused from 2 MiB per object for random bytes and integers, from 4
  or 8 MiB for numbers with six decimals depending on their distribution, and not at 8 MiB for
  a 5,000-word vocabulary; at 1 MiB per object none were. Twice the model admits them, at twice the memory per worker, and
  measures the same values, since a stream that fits does not depend on the model's size.
- **Not memory.** The check never runs a model larger than the one configured, so preflight
  charges each worker what it charged before.

## What was not chosen

- **A margin per object**, at a half or a quarter of the model: refuted above, by pairs that
  fill the model together while each object fits.
- **Checking against twice the model.** It refuses only streams that restart, with no factor
  of two, but each worker then holds a stream of twice the model: at the default 64 MiB and
  the four workers `"auto"` resolves to, 544 MiB, past the default 512 MiB limit that ADR 0021
  keeps every compressor's defaults inside. Admitting the default would have meant raising
  the limit for everyone, to spare a refusal that a larger model resolves for the runs that
  meet it.
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
which an object or a pair needs more than half the model, which completed under 0.5, are
refused until the model is raised or the check is lifted. Skipping the second compression for
pairs too short to fill half the model, from a bound on what PPMd can allocate per byte, would
remove most of the time cost without weakening the check; it needs that bound derived from the
codec first. Interleaved pairing, which might let a pair be measured past the model without
the memory to hold it, remains issue 62.
