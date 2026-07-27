# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project
is pre-1.0, minor versions may contain breaking changes; they are called out
explicitly.

## [Unreleased]

### Added

- schema validator for eo-services
- CLI commands `eo get`, `eo collections`, `eo queryables`, `eo list`, and
  `eo cat`, mirroring `client.get()`, `client.collections()`,
  `client.queryables()`, `client.list()`, and `client.open()` so the whole
  high-level surface is reachable from both interfaces.
- `eo download --resume/--no-resume`, exposing the existing
  `client.download(resume=...)` knob on the CLI (default on: the s3 backend
  resumes partial files, http restarts).
- `eo keys create --fresh` to force a new key pair (`client.keys.create`)
  instead of reusing the labeled one (`client.keys.get_or_create`, still the
  default with `--label`).
- Public profile-management API on `eosdk.config` (`init_profile`, `set_value`,
  `set_default_profile`, `list_profiles`, `get_default_profile`,
  `save_discovered_profile`, `user_config_path`), mirroring the
  `eo config init/set/use/profiles` commands for library callers.
- `ODataCatalogue` is now exported from `eosdk.catalogue`, and `S3KeysProvider`
  /`S3Credentials` from `eosdk.auth` (matching `StacCatalogue` and
  `S3Downloader`).
- README gains Documentation, Contributing, Acknowledgements, Authors, and
  License sections.

### Changed

- **Breaking:** `client.search()` now refuses an unbounded query (no
  collection/bbox/datetime/filter), raising `ConfigError` — matching `eo search`
  and the OData backend, which already refused; previously the default STAC
  backend ran it. Give at least one constraint.
- **Breaking:** the S3 key identifier is now named `access_key` everywhere:
  `eo keys list --json` emits it under `access_key` (was `access_id`, matching
  the `S3Credentials.access_key` attribute and the `AWS_ACCESS_KEY_ID` export),
  and `S3KeysProvider.revoke()`'s parameter is renamed `access_id` →
  `access_key`. `S3Credentials.key_id` remains an alias for `access_key`.
- **Breaking:** the built-in CDSE endpoint defaults are removed. CDSE now
  publishes its discovery document at
  `https://discover.dataspace.copernicus.eu/.well-known/eo-services.json`, so
  the SDK no longer hard-codes any deployment. Connect to a platform once with
  `eo config init --platform https://discover.dataspace.copernicus.eu` (or
  `Client(platform=...)` / `EOSDK_PLATFORM`); a bare `Client()` with no local
  configuration resolves nothing, and first use of a service raises
  `ConfigError` naming the exact knob to set. `keycloak_realm` and
  `keycloak_client_id` lost their CDSE defaults (`CDSE` / `cdse-public`) and
  now resolve from local config or discovery like every other field; the
  deployment-agnostic `s3_region = "default"` is the only remaining model
  default. Smoke tests likewise discover endpoints from the CDSE platform
  root unless `EOSDK_SMOKE_PLATFORM` overrides it.

### Fixed

- A read timeout (the endpoint accepts the connection but never answers — e.g.
  a slow or stalled catalogue search) no longer masquerades as
  `EndpointUnreachable` telling you to "check the endpoint URL". It now raises
  the new `ServiceTimeout` (a subclass of `EndpointUnreachable`, so existing
  handlers still catch it) naming the elapsed timeout and advising a retry or a
  higher `Client(timeout=...)`. Connect failures (refused/DNS/connect timeout)
  keep the original URL-checking guidance.
- S3 backend: first use of a freshly minted S3 key no longer races key
  propagation. Key pairs reach the S3 gateway asynchronously (normally
  seconds), so the first request with a new key could fail with a raw
  `InvalidAccessKeyId` botocore error. The downloader now gates first use on
  the gateway accepting the key (cheap probe, doubling backoff, 60 s cap);
  a key that never activates raises the new `S3KeyNotActive` with the
  likely cause (account key cap) and the `eo keys` commands to fix it.
- `eo doctor`: service and auth probes now trigger platform discovery for
  `<pending>` endpoints (as any service call would) instead of skipping them
  as unconfigured. Previously a discovery-based profile with no pinned
  endpoints had every Services check reported as skipped, never probed.
- `eo doctor`: the S3 credentials check no longer reports a false failure when
  there is no session. Minting a managed key requires login, so without one the
  check is now skipped (`-`, "not logged in — run `eo auth login`") rather than
  failing, and the credentials service is not called. When a session *is*
  present but the credentials service still rejects the request (HTTP 401/403,
  or an anonymous call refused before login), the hint points at
  `eo auth login` instead of misdirecting to the `s3_endpoint` config.
- `eo config init --platform`: re-running it against a discovery-managed
  profile of the same name now resyncs that profile in place instead of
  failing with `profile '<name>' already exists (pass --force)`. `--force` is
  still required only to overwrite a user-owned profile that happens to share
  the name; pass `--name` to keep a hand-tuned profile separate from the
  auto-managed one.

## [0.4.0] - 2026-07-15

### Added

- `eo search --format table|json|id|s3`: `id` prints one product uuid per
  line (feeds `eo download` arguments directly, no `jq` needed), `s3` prints
  one S3 path per line for `eo download --via s3` or external S3 tooling.
- `eo download` accepts S3 paths (`s3://…` or `/eodata/…`) as arguments with
  `--via s3`, alongside bare product uuids.
- Discovery document `platform` block (optional): `name` + `description`
  identify the deployment. On document load the SDK saves the whole projected
  online configuration as a profile named after the platform, marked
  `discovered_from = <discovery URL>`. Marked profiles are managed mirrors and
  resync on every re-fetch (TTL expiry, `eo discover --refresh`); a same-named
  unmarked profile is user-owned — the SDK warns and leaves it untouched.
- `CollectionNotFound`: a STAC search whose first page comes back empty now
  probes `GET /collections/{id}` (one extra request, failure path only) to
  distinguish "no products matched" from "no such collection" — STAC servers
  answer unknown collections with an empty FeatureCollection, not an error.
  The message lists close collection-id matches and, for all-caps
  mission-level names (`SENTINEL-1`), points at the OData protocol whose
  vocabulary they belong to.
- Discovery document `auth.client_id` (optional): a deployment can advertise
  its public OAuth client for the SDK/CLI; projected onto
  `keycloak_client_id` with the usual precedence (kwargs/env/profile pins
  beat discovery; the built-in `cdse-public` remains the last-resort default).
  Previously a discovery-bootstrapped non-CDSE platform silently kept CDSE's
  client id.
- Documentation site (mkdocs-material + mkdocstrings): quickstart,
  configuration, CLI and API reference, error taxonomy; built `--strict` in CI.
- Live smoke suite (`tests/smoke`, gated by `EOSDK_SMOKE=1`); the anonymous
  half (STAC/OData search, cross-protocol agreement, doctor probes) verified
  against the live CDSE platform. Authenticated half awaits credentials.
- Bulk-download benchmark harness (`benchmarks/bench_download.py`); results
  recorded in `benchmarks/RESULTS.md`.
- Release workflow: tag-triggered test matrix -> build -> PyPI Trusted
  Publishing (rc tags to TestPyPI) -> GitHub release; `RELEASING.md` tracks the
  pre-1.0 checklist.

### Changed

- Discovery document: `zipper` / `exos` / `keys_manager` are replaced by one
  `data_access` service split by transport protocol — `data_access.http` with
  the `odata` / `resto` strategies, and `data_access.s3` with `endpoint`,
  `region`, and a `credentials` object of the shape
  `{ "url": ..., "api_version": ... }` (SPEC §6.2 documents it).
- Public API: `via="zipper" | "exos"` → `via="http" | "s3"` (`download`,
  `list`, `open`), CLI `--via http|s3`.
- Config/profile fields and env vars: `zipper` → `eodata_http`
  (`EOSDK_EODATA_HTTP_URL`), `exos_endpoint` / `exos_region` → `s3_endpoint` /
  `s3_region` (`EOSDK_S3_ENDPOINT` / `EOSDK_S3_REGION`), `keys_manager` →
  `s3_credentials` (`EOSDK_S3_CREDENTIALS_URL`).
- Version-guard keys follow the document paths: `zipper/odata` →
  `data_access/http/odata`, `keys_manager` → `data_access/s3/credentials`;
  `eosdk.discovery.models.api_versions` now walks the nested document and
  emits slash-joined paths.
- Internals renamed to match: `eosdk.eodata.zipper.ZipperDownloader` →
  `eosdk.eodata.http.HttpDownloader`, `eosdk.eodata.exos.ExosDownloader` →
  `eosdk.eodata.s3.S3Downloader` (both exported from `eosdk.eodata`).
- `eo doctor`: service checks renamed — `STAC catalogue` → `Catalogue (STAC)`,
  `OData catalogue` → `Catalogue (OData)`, `Zipper eodata` → `EOData (HTTP)`,
  `Exos eodata` → `EOData (S3)`, `Exos (S3)` → `S3 credentials`. Readiness
  results no longer print the probed `/ready` URL; they report `ready`,
  `not ready (HTTP <code>)`, or `not available (endpoint unreachable)`.
- `eo discover`: the services table renders the nested `data_access` groups
  (strategy column shows `http/odata`, `s3/credentials`, ...).

### Removed

- `eo search --json`; use `--format json`.

### Fixed

- `eo keys list` / `eo keys create` no longer mask the access key id: it is a
  public identifier and the argument `eo keys revoke` needs, so masking made
  revocation impossible from the listing. Secrets remain private (`SecretStr`)
  and are only ever printed via the explicit `eo keys create --export` opt-in.
- README quickstart examples used OData mission names (`SENTINEL-1`,
  `SENTINEL-2`) with the default STAC protocol, which silently returned zero
  products; they now use STAC collection ids (`sentinel-1-grd`,
  `sentinel-2-l2a`) and note the vocabulary split.
- `eo doctor`: when the Exos S3 probe fails with a credential-shaped error
  (`InvalidAccessKeyId`, `SignatureDoesNotMatch`, `AccessDenied`,
  `ExpiredToken`), the hint now points at the managed key / keys manager
  (e.g. too many keys on the account — prune with `eo keys list` /
  `eo keys revoke`) instead of misdirecting to the `exos_endpoint` URL.

## [0.3.0] - 2026-07-08

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

### Changed

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

## [0.2.0] - 2026-07-08

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

## [0.1.0] - 2026-07-08

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

[unreleased]: https://gitlab.cloudferro.com/data-access/eosdk/-/compare/v0.4.0...master
[0.4.0]: https://gitlab.cloudferro.com/data-access/eosdk/-/compare/v0.3.0...v0.4.0
[0.3.0]: https://gitlab.cloudferro.com/data-access/eosdk/-/compare/v0.2.0...v0.3.0
[0.2.0]: https://gitlab.cloudferro.com/data-access/eosdk/-/compare/v0.1.0...v0.2.0
[0.1.0]: https://gitlab.cloudferro.com/data-access/eosdk/-/tags/v0.1.0
