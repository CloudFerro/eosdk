# Changelog

## Unreleased (Phase 4 — hardening)

### Added
- Documentation site (mkdocs-material + mkdocstrings): quickstart,
  configuration, CLI and API reference, error taxonomy; built `--strict` in CI.
- Live smoke suite (`tests/smoke`, gated by `EOSDK_SMOKE=1`); the anonymous
  half (STAC/OData search, cross-protocol agreement, doctor probes) verified
  against the live CDSE platform. Authenticated half awaits credentials.
- Bulk-download benchmark harness (`benchmarks/bench_download.py`).
- Release workflow: tag-triggered test matrix -> build -> PyPI Trusted
  Publishing (rc tags to TestPyPI) -> GitHub release; `RELEASING.md` tracks the
  pre-1.0 checklist (license decision and platform credentials still open).


## 0.3.0 (Phase 3 — platform)

### Added
- Remote service discovery (SPEC §6.2): `/.well-known/eo-services.json`
  fetch/parse with schema-version guard, projection onto the endpoint model,
  strategy merging (deprecation/sunset, capabilities intersection gate), and
  an on-disk TTL cache (24 h) with offline fallback to the last good document.
- Single-root bootstrap: `Client(platform=...)` resolves all endpoints lazily
  on first use; construction stays offline; pinned endpoints are final and
  never re-consulted; `config.resolved()` shows `<pending>` until first use.
- API version guard (SPEC §6.3): `UnsupportedApiVersion` raised at first use
  when discovery advertises a version outside the supported range; absent
  advertisement selects the module's newest implemented version.
- Discovery-driven strategy selection in the Client (deprecated fallback
  warns with sunset date and replacement).
- `eo discover [--refresh] [--json]`; the doctor Discovery section now runs
  real checks (document fetch, schema version, advertised api_versions).
- Plugin entry points `eosdk.plugins` (SPEC §11): third-party downloaders and
  catalogue protocols dispatch via `via=`/`protocol=`; broken plugins are
  skipped with a warning, built-ins win collisions.
- Anonymous access to public services: requests go out without a session and
  only a 401 raises the login hint (the CDSE STAC catalogue works unauthenticated).

### Changed — CDSE/CloudFerro alignment (verified against live services)
- Built-in defaults now point at the real deployment: CDSE catalogues,
  `download.dataspace.copernicus.eu` zipper, `eodata.dataspace.copernicus.eu`
  S3, CloudFerro S3 Keys Manager, Keycloak realm `CDSE` / client `cdse-public`.
  `Client()` works out of the box.
- Keys Manager client rewritten to the real API (`/credentials`,
  `access_id`/`secret`, expiration; DELETE by access id). The service has no
  labels, so labeled-reuse is implemented client-side; expired keys rotate.
- STAC normalization matches live payloads: product UUID parsed from the
  `Product` asset's zipper href, checksums decoded as varint multihash
  (md5 `d5 01 10 …`, sha3-256 `16 20 …`), product S3 root derived from the
  common prefix of `s3://` asset hrefs, POST search preferred, date-only
  datetime bounds expanded to RFC 3339, `limit` truncates across pages.
- Zipper Nodes URLs use CDSE-style unquoted node names (percent-encoded).

### Deferred
- `AsyncClient` — optional per plan decision; the sans-io core (translators,
  range planner, capability selection, parsing) is already shared code.

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
