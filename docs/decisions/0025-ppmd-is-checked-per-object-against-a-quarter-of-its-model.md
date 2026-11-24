# ADR 0025: PPMd is checked per object against a quarter of its model

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

## The check

Until the model fills, PPMd codes a stream identically whatever its memory, so compressing an
object under a fraction of the model gives exactly the size it gave under all of it when that
object alone does not fill the fraction, and a different size when it does. That much was
measured without exception.

What the check needs beyond it is that objects which each fit the fraction do not fill the
whole model together, and that depends on the fraction. **At a half it is false.** A
fresh-context verification searched for the largest object of each of twelve kinds of content
that passes at half the model and compressed every pair of them: 33 of 726 pairs restarted a
256 KiB model, and two random-byte objects of 2.66 MB each, both passing, restarted the
default 64 MiB model together. **At a quarter no pair did**: the same search found none in 726
pairs under a 256 KiB model and 600 under a 1 MiB one, and none in 72 under the default 64
MiB model in a reduced search over six kinds. That is a measured margin, not a proof, and a test holds
it the same way -- at the limit, where the half failed -- so a change that breaks it is seen.

So, under PPMd, the distance stage compresses every object a second time under a quarter of
the model -- one compression per object, on the same pool, against the pairs' quadratic count
-- and refuses the run before computing a pair if any size differs. The refusal says how many
objects failed and asks for a larger `ppmd_memory_bytes`, which preflight charges once per
worker. `ResourceLimits.allow_model_restart` (`--allow-model-restart`) measures anyway and
skips the check; like every limit it is not part of run identity [adr-0022]. The aggregate
reports the refusal as `ResourceLimitError`, since it is lifted the way the other limits are.

Preflight cannot apply it: it needs the object bytes, which do not exist before normalization.
So unlike the window gate, the refusal comes after the run directory exists. That directory
belongs to the refused call's configuration: running again with a larger model is another
configuration and needs another directory, as after any failed run, while running again with
the check lifted resumes it. `manifest.json` keeps the limits of the call that created the
directory, so the distance stage's receipt records `ppmd_model_checked`, written by the call
that ran the stage, as the record of whether the check applied.

A quarter of the model must itself be a model, so `ppmd_memory_bytes` starts at 8,192, four
times the codec's own minimum.

## What it costs

The check is conservative, and the cost is refusals of runs that would have measured well.
Under the default model the largest passing object is about 1.2 to 1.7 MB of high-entropy
content -- random bytes, integers, numbers with decimals, free text -- while the numeric
columns above still separated as well as under a 256 MiB model at 8 MiB, and had lost most of
it only at 32 MiB. Such a run now has to raise the model, at four times its largest object's
fill per worker, or lift the check knowingly. Low-entropy content -- categories, small
vocabularies -- fills the model slowly and is rarely refused. A margin set from where the
measurement degrades rather than from where a restart can occur would refuse less; nothing
measured here says where that is for content in general, and a restart in a pair is the
defect this record refuses.

## What was not chosen

- **A declared window for PPMd**, derived from the model memory. Any byte count conservative
  enough for random data refuses vocabulary columns that measure correctly at 32 MiB, and any
  count that admits those admits numeric columns that do not.
- **Recording the restart without refusing.** A run that measured nothing would still look
  like one that worked, which is what ADR 0023 refuses for every other compressor.
- **A larger default model.** It moves the limit without detecting it, and multiplies the
  memory preflight charges every worker.

## Status

This revises ADR 0023's exemption of PPMd, not its gate: the windowed compressors are gated as
before, and PPMd is now checked by measurement instead of not at all. Runs under PPMd whose
objects fill a quarter of the model, which completed under 0.5, are refused until the model is
raised or the check is lifted. Interleaved pairing would let a pair be measured past the model
without the memory to hold it; that remains issue 62, and revisiting the margin needs a search
at least as adversarial as the one that set it.
