# USP Inventor Portal Dossier

Prepare one DAMICORE software component from a stable Git release tag. The dossier is a **local worksheet**, not an upload format or an automatic USP submission.

From the repository root:

```bash
git fetch --tags
python scripts/usp/prepare.py v0.3.0 --package damicore-normalizer --case PC-2026-0060
```

The default destination is `.temp/usp/<version>/<package>/` (a new directory is required). Preparation creates:

- `form.json` — portal answers, verified technical identity, and guidance for missing declarations;
- `source-code.pdf` — paginated, searchable Python implementation from the selected tag;
- `memorial.md` and `justification.md` — editable technical drafts with required review markers.

Review the JSON and replace all `[REVISÃO OBRIGATÓRIA: ...]` sections in both drafts. Then run:

```bash
python scripts/usp/prepare.py --finalize .temp/usp/0.3.0/damicore-normalizer
```

Finalization validates known fields, produces `memorial.pdf` and `justification.pdf`, and reports still-unanswered form fields. It never submits to USP or changes the JSON or Markdown drafts. Confirm unverified dialogs and institutional declarations in the live Portal before submission.

The original INPI archives and hashes are independent. When a matching `scripts/inpi/registrations/<version>.toml` exists, the helper references it; the USP preparation does **not** generate an INPI filing hash.

All generated data stays under `.temp/` and must not be committed. Keep copies of documents actually submitted. The official forwarding letter and other institutional documents must be obtained from USP.
