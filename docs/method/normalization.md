# Object materialization: what an object is

## What this stage computes

An object is a sequence of bytes. Everything downstream — the compressed sizes, the
distances, the tree, the clusters — is a function of those bytes and of nothing else about
the input, so this stage is where the scientific identity of a run is decided.

Objects reach the matrix by one of two sources, which is an axis rather than a set of cases:
a **dataset** is split into objects, and **files** already are the objects. See
[adr-0006]. The source decides the encoding, recorded in the manifest as
`object_encoding`, because an NCD value is only meaningful relative to it.

### `json-lines/1` — a dataset split into objects

A dataset is split one of two ways, named by the value passed as `split`: `columns` makes
one object per column, `rows` one per row. Which one is the first choice that changes every
number downstream, because it decides what a thing being compared *is*.

Every object's bytes come from `serializer.py`, which is the only producer of them:

```
serialize_cell("a,b")   -> b'"a,b"\n'
serialize_cell("ç")      -> b'"\xc3\xa7"\n'
serialize_row(["x","y"]) -> b'["x","y"]\n'
```

One compact JSON string per cell, one compact JSON array per row, UTF-8 without escaping
non-ASCII, LF-terminated. Compact means no space after `,` or `:`. That form is what makes
the bytes independent of chunking and of the platform, and therefore the objects
reproducible and hashable; [adr-0001] is its basis.

A `.csv` and an `.xlsx` of the same logical table produce **identical** object bytes,
because a typed spreadsheet cell crosses to text through one named rule before it reaches
the serializer. That rule is versioned as `cell_text_rule` and recorded in the manifest; see
[adr-0010].

### `raw-bytes/1` — a corpus of files

Each file is one object and its bytes are unchanged. Objects may be binary. Files are
ordered by relative POSIX path and labelled by it, so two files sharing a basename stay
distinct, and the corpus is identified by a digest over the whole ordered set.

## What makes a run reproducible

- **The bytes are canonical, not incidental.** Two runs over the same input produce the same
  objects byte for byte, which is what lets a compressed size be compared at all.
- **The order is fixed.** A dataset's objects follow the split; a corpus is sorted by
  relative path. Nothing depends on filesystem enumeration order.
- **The input is digested.** The manifest records a SHA-256 over the input, so a result can
  be attributed to the bytes that produced it.
- **The encoding is recorded, never assumed.** `object_encoding` is in the manifest, so a
  distance is attributable to the bytes it measured.

## What this stage does not do

- **No published source.** Nothing here implements a method from the literature; the
  encoding is this project's own and [adr-0001] is its basis. A reader looking for the
  scientific content of DAMICORE should start at [`ncd.md`](ncd.md).
- **No normalisation of content.** Case, whitespace, number formatting and encoding of the
  source cells are passed through as text. The only transformation is the one
  `cell_text_rule` names.
- **No feature extraction or discretisation.** An object is the data, not a representation
  derived from it.

## Status in this implementation

| | |
|---|---|
| Available | delimited text with any single-character delimiter (`.csv`, `.tsv`, `.txt`); `.xlsx` and `.xlsm`; a corpus of files; split by column or by row |
| Not available | legacy `.xls`, `.xlsb`, `.ods`; archive expansion; a DataFrame or stream as input; any symbolic or discretised encoding of numeric attributes |
| Encodings | `json-lines/1` for a dataset, `raw-bytes/1` for a file corpus |
