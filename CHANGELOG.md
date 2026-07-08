# Changelog

## 0.2.0 (Phase 2 — full access)

### Added
- OData catalogue backend: shared `Query` → `$filter` translation (golden-case
  contract suite), `@odata.nextLink` pagination, CSC normalization,
  `query_raw` escape hatch.
- Capability framework (SPEC §6.3/§6.6): built-in matrix, capability-first
  strategy selection with sunset/deprecation handling, discovery intersection
  gate (restrict-only), `UnsupportedCapability` with actionable alternatives.
- S3 Keys Manager client: create/list/revoke pinned to `/v1`, labeled-reuse
  policy with on-disk secret store (0600), explicit `ephemeral()` context
  manager (revoke-on-exit).
- Exos S3 backend: ranged multipart downloads with ETag-validated resume
  (sidecar state; changed objects restart, never splice), `list()`
  (single-level + native recursive prefix walk), `open()` ranged reads
  (`IfMatch`-pinned, readahead-buffered `RawIOBase`).
- Zipper `list()` via the Nodes hierarchy (BFS, one request per directory,
  OData key encoding for adversarial names); cross-backend Node parity
  verified against Exos.
- Client facade: `via=`/`protocol=` dispatch through strategy selection,
  `client.list/open/keys`, lazy Exos construction (keys minted on first use).
- CLI: `eo keys create/list/revoke` (secrets masked; `--export` opt-in emits
  eval-safe `AWS_*` lines), `eo doctor` (✓/✗/- with hints, `--json`,
  non-zero exit on failure, graceful skips for unconfigured subsystems).
- SPEC §7.1 executed end-to-end as the standing acceptance test
  (respx + moto): search → download → list → ranged open.

### Pending external verification
- Staging run (doctor green, fixture re-recording) — requires real platform
  credentials and endpoints (`UNVERIFIED-FIXTURE` markers track this).

## 0.1.0 (Phases 0–1)

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
