# Changelog

## Unreleased (0.1.0 — Phases 0–1)

### Added
- `Client` facade: offline construction, layered config resolution
  (kwargs > `EOSDK_*` env > `./eosdk.toml` > user config > discovery (stub) >
  built-in defaults) with per-endpoint source introspection (`config.resolved()`).
- Keycloak auth: password grant and device flow, transparent access-token
  refresh with on-disk cache (mode 0600, per profile), 401 → one forced
  refresh → retry → `AuthError`.
- Thin STAC ItemSearch client: conformance verification, landing-page search
  link, transparent pagination into a lazy re-iterable `SearchResult`,
  normalized `Product` model (incl. multihash checksum decoding).
- Zipper download backend: whole-object transfers over the OData `$value`
  route, retries with restart (Zipper has no HTTP Range), checksum
  verification, progress callbacks, bounded concurrency.
- `eo` CLI: `auth login/logout/status`, `search` (rich table or `--json`
  JSON Lines), `download` (ids or stdin piping via `-`), `config
  init/show/set/use/profiles` with comment-preserving TOML writes.
- Full exception taxonomy per SPEC §8; shared httpx transport with retry
  policy, `route()` URL builder, and OData key encoding.
- CI: ruff + mypy strict + pytest on Python 3.10–3.13, build + wheel smoke.
