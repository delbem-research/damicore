# Quality drivers

Eight properties, in priority order, and the hard constraints that bound all of them. The
order is the point: it is what settles a trade-off when two of these pull in opposite
directions, which is a question no test can answer and every material decision eventually
asks.

| Priority | Property | Verifiable scenario |
|---:|---|---|
| 1 | Correctness | Mathematical fixtures validate NCD, Neighbor Joining, and leaf association. |
| 2 | Bounded memory | A large dataset is processed in chunks; the quadratic matrix and workspace stay memory-mapped. |
| 3 | Reproducibility | The manifest records the input hash, the configuration, the versions, and the artifact hashes. |
| 4 | Notebook experience | `pip install` and one `damicore.run` call suffice; progress works in Colab. |
| 5 | Recovery | An interrupted run resumes only the shards that are complete and compatible. |
| 6 | Modularity | Each stage installs and tests in isolation; only the orchestrator depends on the four. |
| 7 | Operability | The report exposes timings, counts, estimated resources, progress, and typed failure. |
| 8 | Portability | Python 3.11–3.14, Linux/macOS/Windows, no mandatory system binary. |

Hard constraints, which are not traded against anything:

- the implementation must be pure Python plus wheels of the declared dependencies;
- the exact result keeps its quadratic cost in NCD and cubic cost in Neighbor Joining;
- resource gates must refuse infeasible work before millions of objects or pairs are created;
- the system must never hide infeasibility behind silent sampling.

## How the order is used

A lower-numbered property wins. Two decisions already turn on that and say so:

- [ADR 0012](decisions/0012-duplicated-helpers-stay-duplicated.md) keeps duplicated helpers
  because consolidating them would need a sixth distribution or a vendoring scheme, and
  modularity at 6 does not pay for what portability at 8 and the packaging cost would lose.
- [ADR 0013](decisions/0013-the-public-signature-stays-flat.md) keeps the flat public
  signature because notebook experience at 4 outranks modularity at 6, and accepting the
  source union instead would make the primary call import a model from another distribution.

Neither decision is derivable from the code. Both were re-derived from scratch, twice,
because this table was not written down.

## Provenance

Recovered from section 5 of `DAMICORE_IMPLEMENTATION_SPECIFICATION.md`, the 0.1 specification
deleted in PR #16 once its rules had been migrated into the tests, schemas and instructions
that enforce them. This ordering had no enforceable form, so it was migrated nowhere and left
the repository with the document. The wording is translated from the original; the order and
the scenarios are unchanged.
