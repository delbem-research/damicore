# ADR 0025: PPMd is checked per object against half its model

ADR 0023 refuses a pair longer than the window its compressor declares, and exempts PPMd
because "the model's size is not the limit". That held for the data it was measured on and
not in general. PPMd variant I is driven with restart: when its model memory fills, it starts
again from nothing, and once that happens inside the first object of a pair, nothing of it is
left when the second is coded. The pair then measures near 1 whatever the two share -- the
failure ADR 0023 exists to refuse, under the default compressor.

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
loses the separation the larger model keeps, and the default model is already losing it at 32
MiB per column. `PPMD8_RESTORE_METHOD_CUT_OFF`, which frees the least used contexts instead of
restarting, was measured on the same columns and does not keep it either (+0.0006 to +0.0036
under 4 MiB), so the restoration method stays as ADR 0020 set it.

## The check

Until the model fills, PPMd codes a stream identically whatever its memory, so compressing an
object under half the model gives exactly the size it gave under all of it when neither
restarted, and a different size when either did. Two objects that each fit in half the model
do not fill all of it together. Both claims were measured on four kinds of data -- the
vocabulary above, the numeric columns, integers, and random bytes -- at sizes from 64 KiB to 8
MiB under an 8 MiB model, against a model sixteen times larger that none of them fills:

- the size under half the model equalled the size under the full one in every case where the
  reference showed no restart, and differed in every case where it showed one;
- in every case where both objects passed, the pair compressed under the model exactly as
  under the reference.

No case contradicted either. The check is conservative by construction: 256 KiB of random
bytes fills half of the 8 MiB model without the pair filling all of it, and is refused.

So, under PPMd, the distance stage compresses every object a second time under half the model
-- one compression per object, on the same pool, against the pairs' quadratic count -- and
refuses the run before computing a pair if any size differs. The refusal says how many objects
failed and asks for a larger `ppmd_memory_bytes`, which preflight charges once per worker.
`ResourceLimits.allow_model_restart` (`--allow-model-restart`) measures anyway; it skips the
check, is recorded in the manifest under `config.limits`, and like every limit is not part of
run identity [adr-0022]. The aggregate reports the refusal as `ResourceLimitError`, since it is
lifted the way the other limits are.

Preflight cannot apply it: it needs the object bytes, which do not exist before
normalization. That is the one difference from the window gate, and the reason this check
lives in the stage that owns the compressor.

Half the model must itself be a model, so `ppmd_memory_bytes` starts at 4,096, twice the
codec's own minimum.

## What was not chosen

- **A declared window for PPMd**, derived from the model memory. Any byte count conservative
  enough for random data would refuse vocabulary columns that measure correctly at 32 MiB, and
  any count that admits those would admit numeric columns that do not.
- **Recording the restart without refusing.** A run that measured nothing would still look
  like one that worked, which is what ADR 0023 refuses for every other compressor.
- **A larger default model.** It moves the limit without detecting it, and quadruples the
  memory preflight charges every worker.

## Status

This revises ADR 0023's exemption of PPMd, not its gate: the windowed compressors are gated as
before, and PPMd is now checked by measurement instead of not at all. Runs under PPMd whose
objects fill half the model, which completed under 0.5, are refused until the model is raised
or the check is lifted. Interleaved pairing would let a pair be measured past the model
without the memory to hold it; that remains issue 62.
