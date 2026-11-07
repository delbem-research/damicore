# ADR 0019: An attribution declares whether its work was read

Every attribution to a published work carries one line saying whether anybody read that
work against this code: `Read: YYYY-MM-DD, primary.`, the same with `secondary.`, or
`Not read: <why not>.` with a reason that may not be empty.
`tests/architecture/test_citations.py` requires exactly one such line per attribution and
checks nothing about its content. ADR 0015 made the bibliography fetched rather than
written; this records the state the fetch cannot establish.

## The failure this comes from happened here

ADR 0016 cited [clauset2004] for the quantity FastGreedy maximizes. The key resolved, the
work was real, the sentence read well, and every gate was green. The paper defines
modularity with an adjacency entry of 1 or 0, while the code hands igraph a weighted graph;
the quantity actually maximized is [newman2004]'s. It was found by reading the paper, and
nothing in the repository could have found it.

That is the limit this decision accepts rather than tries to beat. A resolved entry proves
the work exists. Whether it supports the sentence beside it is a reader's judgement, and
the only thing a check can do is make the absence of that judgement visible.

## Three vocabularies for one fact is how it stayed invisible

Before this, the same state was written three ways: a delta sentence with a direct quote in
`ncd.py`, a bold "None has been read against this code." paragraph in `natural_breaks.py`,
and -- in `fastgreedy.py` -- a delta sentence with no statement either way. A fourth form
was one commit away, because nothing said which to use.

The count made it worse. Thirteen of the twenty attributions in the census were presumed,
and finding that out meant reading every docstring; issue #93 counted them by hand. With
one spelling the count is derived, and `docs/method/evidence.md` (issue #58) can consume it
instead of a second manual tally.

## Why the mark hangs on the attribution and not on the module

`natural_breaks.py` names five works. Three of them -- [gronlund2017], [song2020] and
[wang2011] -- were read on 2026-11-07, and [fisher1958] and [jenks1977] cannot be obtained
at all. A module-level statement has no way to say that, and the one it had was literally
false the moment the first of the five was read. The attribution is the unit that makes a
claim, so it is the unit that declares.

## Why `primary` and `secondary` are distinguished

ADR 0016 was not "nobody read it". Somebody read *about* it. A bit that records only
whether reading happened would have been set to true, and the wrong sentence would have
shipped with a mark vouching for it. `secondary` is the honest record of having read an
account of a work, and it is visible in exactly the way the failure needs it to be.

## Why obtainability is a reason and not a field

The first draft of this put the access state in `docs/refs.json`, beside the fetched
metadata. It is not there, for three reasons. Open access is time-varying -- [saitou1987]
is free to read at the publisher today and machine-unreachable all the same -- so the field
would be a snapshot that goes stale silently. Writing it requires a resolver this project
does not already call, and ADR 0015's rule is that nothing in that file is typed by hand.
And the fact is only ever needed where it changes what a reader should do, which is beside
the attribution that cannot be completed: `Not read: closed access, no open copy anywhere.`
says it in the place it matters, in a field that already has to be non-empty.

## What this does not do

It does not check that a `Read:` date is true, that the work named is the work opened, or
that the delta sentence beside it is correct. A date in a docstring is evidence of nothing:
`make ref` keeps the fetch script free of a clock by taking `--today` from the shell, and a
mark is typed by its author in the same spirit. What the gate removes is the silent third
state -- an attribution that neither claims a reading nor admits the lack of one -- which
is what left thirteen of them indistinguishable from the four that had been checked.
