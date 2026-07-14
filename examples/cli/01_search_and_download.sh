#!/usr/bin/env bash
# Search the catalogue and download products with the `eo` CLI.
#
# `eo search --json` emits one product per line (JSON Lines), which pipes
# straight into `eo download -`, jq, head, etc.
#
# Prerequisites: `eo auth login` for the download steps (search is anonymous).
set -euo pipefail

# -- 1. human-readable search (rich table) -------------------------------------
eo search \
  --collection sentinel-2-l2a \
  --bbox 22.5,52.9,24.0,53.5 \
  --from 2026-06-01 --to 2026-06-30 \
  --filter "cloudCover=<20" \
  --sort "-datetime" \
  --limit 10

# Unbounded searches are refused; give at least one of
# --collection / --bbox / --from/--to / --filter.

# -- 2. the composable pipeline: search | download ------------------------------
eo search --collection sentinel-2-l2a \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "cloudCover=<10" \
          --limit 2 --json \
  | eo download - --via http --output ./data --concurrency 4

# -- 3. JSON Lines interoperate with standard tooling ----------------------------
# Names and sizes only:
eo search --collection sentinel-2-l2a --from 2026-06-01 --to 2026-06-08 \
          --limit 5 --json \
  | jq -r '"\(.name)\t\(.size)"'

# Filter client-side, keep only the two largest, then download:
eo search --collection sentinel-2-l2a --from 2026-06-01 --to 2026-06-08 \
          --limit 20 --json \
  | jq -s 'sort_by(-.size) | .[:2] | .[]' -c \
  | eo download - --output ./data

# -- 4. download by bare product id (no prior search needed) ----------------------
# eo download 5b9f4d4e-... c2a7de3f-... --output ./data

# -- 5. useful switches ------------------------------------------------------------
# --via s3            S3 backend (resumable; S3 keys minted automatically)
# --no-checksum       skip checksum verification
# --quiet             no progress bars (progress goes to stderr, so pipes are
#                     safe either way)
# --protocol odata    search via the OData catalogue instead of STAC
