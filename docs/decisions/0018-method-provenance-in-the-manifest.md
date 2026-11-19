# ADR 0018: The manifest records which rule produced each stage

A run directory has to answer, a year later, which variant of the method produced it. Today
it records what was configured — the compressor, the cluster count — and not which rule was
in force. Those are different facts: a parameter is a choice inside a rule, and a rule is
what a citation, a method page and a manifest field can all name.

ADR 0008 already established the pattern for one stage: an NCD value is only meaningful
relative to the bytes it measured, so the encoding that produced them is recorded as
`object_encoding` rather than assumed. This extends the same treatment to the stages that
had no name at all.

## What is recorded

`manifest.json` gains a `method` block with one entry per stage, each a versioned name and
the works that are its basis:

| stage | rule | basis |
|---|---|---|
| `objects` | `canonical-json-lines/v1` | [adr-0001], [adr-0008], [adr-0010] |
| `distance` | `ncd-max/v1` | [cilibrasi2005] |
| `tree` | `neighbor-joining-q/v1` | [saitou1987], [studier1988], [adr-0017] |
| `clusters` | `fastgreedy-modularity/v1` | [clauset2004], [newman2004], [csardi2006], [adr-0016] |

The names are constant for this release. Recording them per run anyway is the point: a
directory outlives the release that wrote it, the release cannot be asked afterwards what
it implemented, and every open issue in this roadmap turns one of these constants into a
variant.

Parameters stay where they are. Which compressor, how many clusters, which delimiter — all
remain in `config`, which `config_hash` already covers. `AGENTS.md` settles the boundary:
where a semantic choice is already a `Literal` on a config field, that enum is the rule's
domain. The rule names the method; the configuration names the choice made inside it.

## It is additive, so nothing is invalidated

`method` is optional and the schema version does not change. A manifest written before this
block validates unchanged and reads back as `None`, which is the honest statement: that run
does not say which rules produced it, and nothing can infer them from the outside.

The alternative was a schema bump, which is what this project did at 0.2 and which refuses
every earlier run directory on load and on reuse. That cost buys nothing here: the block
adds a fact rather than changing the shape of one, so an older manifest is not wrong, only
silent.

## The keys are held

Every citation key in the block is asserted to resolve by
`tests/architecture/test_citations.py`. That check exists because this is the one place a
key is written in Python rather than in a docstring or a Markdown file, so the scanner that
holds every other citation does not read it — and a typo here would reach a user as
provenance pointing nowhere, inside the artifact whose purpose is to still answer later.

## Status

Extending the block is a new version of the rule whose entry changes, never a silent
redefinition, because the rule is what identifies the result a run reports. A stage that
does not exist yet has no entry; adding one is a change to this record.
