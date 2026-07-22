#!/usr/bin/env bash
# S3 key lifecycle with the `eo` CLI.
#
# Keys unlock direct S3 access to EOData (aws-cli, rclone, GDAL /vsis3/...).
# Secrets are masked everywhere except the explicit `--export` opt-in.
#
# Prerequisites: `eo auth login`.
set -euo pipefail

# -- 1. create (or reuse) a labeled key pair --------------------------------------
# The same label returns the same pair, so this is safe to run on every job.
eo keys create --label my-pipeline

# --fresh forces a brand-new pair instead of reusing the labeled one; handy for
# rotation, but it counts against the account's cap on concurrent keys.
# eo keys create --label my-pipeline --fresh

# -- 2. list keys (secrets never shown) ---------------------------------------------
eo keys list
eo keys list --json | jq -r '.[].access_key'

# -- 3. export for aws-cli / rclone ----------------------------------------------------
# Prints AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL lines;
# eval them into the current shell:
eval "$(eo keys create --label my-pipeline --export)"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ENDPOINT_URL

aws s3 ls "s3://eodata/Sentinel-2/" --page-size 5 | head

# rclone equivalent (no config file needed):
# rclone lsd :s3,endpoint="$AWS_ENDPOINT_URL",access_key_id="$AWS_ACCESS_KEY_ID",secret_access_key="$AWS_SECRET_ACCESS_KEY":eodata

# -- 4. revoke when done ------------------------------------------------------------------
# eo keys revoke <access-key> --yes
