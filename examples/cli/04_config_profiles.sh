#!/usr/bin/env bash
# Configuration and profiles with the `eo` CLI.
#
# Endpoints resolve per field, most specific wins:
#   Client kwargs > EOSDK_* env > ./eosdk.toml > user profile > discovery > defaults
set -euo pipefail

# -- 1. inspect the resolved configuration -----------------------------------------
# Every endpoint with its value and the source that provided it. Endpoints only
# discovery can fill show <pending> until the owning service is first used.
eo config show
eo config show --json | jq '.zipper'

# -- 2. create profiles ----------------------------------------------------------------
# Interactive: asks for a platform root (rest discovered) or manual endpoints.
# eo config init --name prod --platform https://platform.example.eu

# Non-interactive edits — writes preserve comments and file layout:
# eo config set profiles.staging.platform https://staging.example.eu
# eo config set profiles.staging.zipper   https://zipper-canary.example.eu   # pin beats discovery
# eo config set default_profile staging

# -- 3. list and switch ---------------------------------------------------------------
eo config profiles
# eo config use staging          # persist the default
# eo --profile staging search ...  # or per-invocation, no state changed

# -- 4. environment variables override profiles ------------------------------------------
# EOSDK_PROFILE=staging eo config show
# EOSDK_ZIPPER_URL=http://localhost:8082 eo config show     # surgical override
# EOSDK_PLATFORM=http://localhost:8080 eo discover           # point at local discovery

# -- 5. a project-local ./eosdk.toml (beats the user config, loses to env) ----------------
# cat > eosdk.toml <<'EOF'
# [profiles.default]
# platform = "https://platform.example.eu"
# EOF
