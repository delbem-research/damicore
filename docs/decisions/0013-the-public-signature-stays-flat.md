# ADR 0013: The public signature stays flat

`run` and `estimate` take the source axis as seven flat keyword arguments -- `source_kind`,
`split`, `delimiter`, `encoding`, `sheet`, `recursive`, `include_hidden` -- rather than the
`ObjectSource` union that `damicore_normalizer` already defines. Since only a subset applies
to each source, the aggregate maps the flat arguments back onto that union and rejects the
ones that do not apply. That adapter reads as accidental accretion and gets proposed for
removal, so this records why it is neither accidental nor removable.

## Why flat

The shape is specified, not inherited. The 0.1 specification fixed `run(csv_path, split,
delimiter, encoding, compressor, compression_level, num_clusters)` and stated the primary use
case as Jupyter and Colab. Notebook developer experience is the fourth of eight prioritised
quality drivers, above modularity at sixth, and a flat signature is what makes the primary
call `run("dataset.csv", split="rows")`.

Accepting the union instead would make that call
`run("dataset.csv", source=DelimitedSource(split="rows"))`, which asks a notebook user to
import a model from a *different distribution* to express what one keyword expresses today.
It would also require `damicore` to re-export three models from `damicore_normalizer`,
growing the public surface in order to shrink an internal adapter. Both move against the
driver the signature exists to serve.

Rejecting an argument that does not apply is a declared 0.2 contract, not an implementation
detail: "A setting that does not apply raises `ConfigurationError` instead of being ignored."
The adapter is where that contract is honoured.

## What was actually wrong

Five lines, not the ninety a line count suggests. `_SOURCE_ARGUMENTS` restated the union's
field names by hand, giving the axis two declarations that could disagree. Measured at the
time of this decision they agreed exactly, and nothing compared them.

Enumerating how they could diverge separates one real hazard from two harmless ones:

| Divergence | Consequence |
|---|---|
| a source model gains a field with no matching keyword | **silent** -- the capability exists in the stage package and never reaches a `damicore` caller |
| a keyword is added to `run` but not threaded into the adapter | **silent** -- the caller passes it and nothing rejects it, which is what the 0.2 contract forbids |
| the hand-written table disagrees with the union | loud: either a rejection message that lies, or `extra="forbid"` at construction |

Both silent cases are invisible because each surface is individually consistent.

## Decision

The signature stays flat. Two changes close the hazard without touching it:

- `_SOURCE_ARGUMENTS` is derived from `DelimitedSource`, `SpreadsheetSource` and
  `FileCorpusSource`, so the union is the one declaration and the third row above cannot
  occur;
- `test_every_source_setting_is_reachable_and_threaded` asserts every union field appears as
  a parameter of `run`, `estimate` and the adapter, which closes the two silent rows. It was
  observed failing against both before being kept.

Net effect is seven lines added, no public API change, no version bump.

## Revision condition

Reopen if the source axis grows past roughly three variants or the per-source argument sets
start overlapping in ways a flat signature cannot express without ambiguity -- at which point
the union becomes the clearer public contract and the break is worth a major release. A
larger line count in the adapter is not by itself a reason: it is the cost of the flat
signature, which is a product decision recorded above.

## Note on the source

The evidence for this decision came from `DAMICORE_IMPLEMENTATION_SPECIFICATION.md`, deleted
in PR #16 and recoverable only from git history. Its section 5 prioritised eight quality
drivers, and that ordering is what settles both this decision and ADR 0012; it had survived
in no other file, so both decisions had to rediscover it. It is now
[docs/quality-drivers.md](../quality-drivers.md).
