#!/usr/bin/env bash
# Authentication with the `eo` CLI.
#
# Sessions are cached per profile under ~/.config/eosdk/ (mode 0600), so you
# log in once and every later `eo` / library call reuses the session.
set -euo pipefail

# -- 1. interactive login (device flow — default when the realm supports it) -----
# Prints a URL + code; finish in the browser, no password touches the shell.
eo auth login

# -- 2. username/password (services, CI) -------------------------------------------
# Password from stdin so it never lands in shell history or `ps` output:
# printf '%s' "$EO_PASSWORD" | eo auth login --username alice --password-stdin

# -- 3. session status ---------------------------------------------------------------
# Exits non-zero when not logged in — script-friendly:
eo auth status
if ! eo auth status >/dev/null 2>&1; then
    echo "not logged in — refusing to start the pipeline" >&2
    exit 1
fi

# -- 4. per-profile sessions -----------------------------------------------------------
# Each profile has its own token cache; switch with the global --profile flag:
# eo --profile staging auth login
# eo --profile staging auth status

# -- 5. drop the cached session ---------------------------------------------------------
# eo auth logout
