# Marker migration utility

This repo recently renamed legacy artifact markers (headers/prefixes) to remove
historic naming. Old markers remain readable at runtime, so migration is optional.
This script exists to normalize old markers on disk when you want a clean slate.

## Why this exists

- Remove legacy marker names from stored artifacts without re-encrypting data.
- Keep backward compatibility: old markers are still accepted by the runtime.
- Provide a one-off utility to rewrite headers and migrate CharacterStore files.

## Usage

Dry-run (default; no changes):

```
python3 scripts/migrate_markers.py
```

Apply changes:

```
python3 scripts/migrate_markers.py --apply
```

Notes:
- The script scans configured output roots derived from `APP_ROOT` and output
  directory settings (for example `DUBBING_OUTPUT_DIR`).
- CharacterStore migration requires `CHAR_STORE_KEY` (or `CHAR_STORE_KEY_FILE`)
  to be set so the file can be decrypted and re-saved with the new marker/AAD.
- Encrypted artifact marker rewrites do not require keys because only the header
  prefix is rewritten (content is untouched).

## Rollback

There is no rollback tool. If you want the option to revert, take a backup of
the output directories before running with `--apply`.

## Compatibility

Even without migration:
- Old markers remain readable at runtime.
- New artifacts are written with the new markers.
