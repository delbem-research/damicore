# INPI Registration Helper

Prepares deterministic source archives for DAMICORE software registration with INPI.

## Usage

Run from the repository root using a stable release tag:

```bash
git fetch --tags
python scripts/inpi/prepare.py <release-tag>
```

For example:

```bash
python scripts/inpi/prepare.py v0.2.0
```

The release tag must follow the `vX.Y.Z` format and exist in Git.

The script reads source files directly from the selected tag, so uncommitted working-tree changes do not affect the generated archives.

## Output

For a release `<version>`, the script creates:

```text
.temp/inpi/<version>/
├── <package>-<version>-inpi.zip
├── ...
└── SHA512SUMS.txt
```

Each ZIP contains the tracked source subtree for one software component defined in `config.toml`.

`SHA512SUMS.txt` contains the SHA-512 digest of each generated ZIP and identifies the exact archive bytes prepared for INPI submission.

The script also creates:

```text
scripts/inpi/registrations/<version>.toml
```

This file records the release, commit, archives, hashes, and registration metadata.

After filing with INPI, complete the `inpi_process`, `inpi_registration`, and `inpi_url` fields.

## Custom Output Directory

```bash
python scripts/inpi/prepare.py <release-tag> --out /path/to/output
```

The target output directory must not already exist. This prevents accidental overwrites of previously generated registration artifacts.

If it already exists, remove it explicitly or choose another path before running the script again.

## Configuration

`scripts/inpi/config.toml` defines the software components and registration metadata used by the script.

Generated ZIP files are local artifacts and should not be committed. Keep durable backups of the exact files submitted to INPI.
