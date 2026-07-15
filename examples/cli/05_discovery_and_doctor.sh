#!/usr/bin/env bash
# Service discovery and health checks.
#
# A platform advertises its services in a discovery document at
# /.well-known/eo-services.json; the SDK resolves missing endpoints from it
# lazily. `eo doctor` verifies config, auth, and service health with
# pipeline-friendly exit codes.
set -euo pipefail

# -- 1. run a local discovery endpoint (no real platform needed) --------------------
# The repo ships an nginx image serving a sample document:
docker build -t eosdk-discovery docker/discovery
docker run --rm -d -p 8080:80 --name eosdk-discovery eosdk-discovery
trap 'docker stop eosdk-discovery >/dev/null' EXIT
sleep 1

curl -s http://localhost:8080/.well-known/eo-services.json | jq .

# -- 2. point the SDK at it and inspect --------------------------------------------------
# Table view: service, strategy, url, api version, capabilities, deprecations.
EOSDK_PLATFORM=http://localhost:8080 eo discover

# Raw document for scripting; --refresh busts the TTL cache:
EOSDK_PLATFORM=http://localhost:8080 eo discover --json | jq '.services | keys'
EOSDK_PLATFORM=http://localhost:8080 eo discover --refresh

# Discovered endpoints show up in the resolved config (source: discovery):
EOSDK_PLATFORM=http://localhost:8080 eo config show

# -- 3. health checks ---------------------------------------------------------------------
# Human output with ✓/✗ and hints; exits non-zero on any failure, so it works
# as a CI gate or container readiness probe:
eo doctor || echo "some checks failed (exit $?)"

# Machine-readable — list every failing check:
eo doctor --json | jq -r '.[] | .results[] | select(.ok == false) | .name'
