# eosdk — Python SDK for Earth Observation Services

**Specification for development — v0.1 (draft)**
Date: 2026-07-06 · Status: proposal consolidated from design discussion

---

## 1. Purpose and background

The platform exposes several Earth Observation components, each with its own API,
protocol, and authentication mechanism:

| Component | Service | Protocol | Authentication |
|---|---|---|---|
| Catalogue | Catalogue API | OData | Keycloak JWT |
| Catalogue | Catalogue API | STAC | Keycloak JWT |
| EOData access | Zipper | HTTP | Keycloak JWT |
| EOData access | Exos | S3 | Access/Secret keys from **S3 Keys Manager** |
| Auth | Keycloak | OIDC | user credentials / device flow |
| Auth | S3 Keys Manager | REST | Keycloak JWT |

Both Zipper and Exos download EOData objects from the large **EOData repository**.
Today, every consumer must understand each protocol and wire the auth flows manually.

**Goal:** a single Python SDK (`eosdk`) — usable as a library and as a CLI (`eo`) —
that hides protocol and credential complexity behind one coherent interface:
*search, then download*.

## 2. Goals and non-goals

### Goals

- One import (`from eosdk import Client`) covering the 90% use case:
  search the catalogue, download products, auth handled invisibly.
- Protocol-agnostic search over both STAC and OData with a unified result model.
- Pluggable download backends (Zipper/HTTP, Exos/S3) behind one interface.
- Full credential lifecycle management (JWT refresh, S3 key generation/reuse/revocation)
  owned by the SDK; user code never touches raw credentials.
- Typed, layered configuration with named profiles and environment overrides.
- Service URL discovery so endpoints are not baked into releases.
- First-class CLI mirroring the library 1:1, composable in shell pipelines.
- Built-in diagnostics (`eo doctor`) for config, auth, and service health.
- Extensible: a future catalogue protocol or access service is one new module,
  not an API change.

### Non-goals (for v1)

- EO data processing/analysis (rasterio, xarray integration) — out of scope;
  the SDK delivers bytes/files.
- Non-Python bindings (a REST-first design keeps the door open, but v1 is Python only).
- Ordering/production services, quota management UIs.
- GUI.

## 3. Deliverables

1. **Python library** `eosdk` — sync `Client` in v1; `AsyncClient` with the same
   surface planned from day one (shared transport supports both).
2. **CLI** `eo` — thin wrapper over the library (typer), identical behaviour.
3. Documentation: quickstart, API reference, CLI reference, deployment/config guide.
4. Test suite (unit + integration) and CI pipeline.
5. PyPI package with semantic versioning.

## 4. Architecture

### 4.1 Layers

```
┌───────────────┐   ┌──────────────────┐
│   eo CLI      │   │  Python library  │        consumers
└───────┬───────┘   └────────┬─────────┘
        └──────────┬─────────┘
            ┌──────▼───────┐
            │ Client facade │  search · list · download · auth   eosdk
            └──────┬───────┘
   ┌───────────────┼────────────────┐
┌──▼────────┐ ┌────▼───────┐ ┌──────▼──────┐
│ Catalogue │ │ Data access │ │    Auth     │   domain modules
│ STAC·OData│ │ Zipper·Exos │ │ KC · S3 keys│
└──┬────────┘ └─┬───────┬──┘ └──┬───────┬──┘
   │            │       │       │       │
┌──▼───────┐ ┌──▼───┐ ┌─▼──┐ ┌──▼─────┐ ┌▼─────────────┐
│Catalogue │ │Zipper│ │Exos│ │Keycloak│ │ Keys manager │   external services
│   API    │ │ HTTP │ │ S3 │ │  JWT   │ │ S3 credentials│
└──────────┘ └──┬───┘ └─┬──┘ └────────┘ └──────────────┘
                └───┬───┘
         ┌──────────▼──────────┐
         │  EOData repository  │
         └─────────────────────┘
```

Shared foundations (used by all domain modules): `transport`, `config`, `discovery`,
`models`, `exceptions`.

### 4.2 Dependency rules

- `cli` → `client` → domain modules (`catalogue`, `eodata`, `auth`) → `transport` /
  `config` / `discovery`. Never the reverse.
- `config` may invoke `discovery` during endpoint resolution (precedence step 5,
  §6.1); the potential cycle is broken by layering — discovery receives only the
  bootstrap `platform` root (resolved from local sources: kwargs/env/file, never
  from discovery) and returns endpoints. Discovery never reads config itself.
- Domain modules **never** read config files or environment variables directly;
  they receive resolved endpoints and credential providers at construction time.
- Domain modules **never** handle raw credentials; they ask the auth module for an
  authenticated session / signed request.
- No module hand-assembles URLs; path building goes through the transport `route()`
  helper against the injected base URL.

### 4.3 Package layout

```
eosdk/
├── client.py              # Client facade — the one import most users need
├── models.py              # Product, Collection, SearchResult, Query (pydantic)
├── exceptions.py          # EosdkError (base); AuthError, ProductNotFound,
│                          #   DownloadError, EndpointUnreachable,
│                          #   UnsupportedApiVersion, UnsupportedQueryFeature,
│                          #   UnsupportedCapability, QuotaExceeded
├── config/
│   ├── settings.py        # Endpoints + Profile models (pydantic)
│   ├── loader.py          # resolution chain: kwargs > env (EOSDK_*) >
│   │                      #   ./eosdk.toml > ~/.config/eosdk/config.toml >
│   │                      #   discovery > built-in defaults
│   ├── profiles.py        # profile CRUD, default_profile handling
│   └── defaults.py        # built-in endpoints for known deployments
├── discovery/
│   ├── platform.py        # /.well-known/eo-services.json fetch + parse
│   ├── oidc.py            # Keycloak openid-configuration resolution
│   ├── stac.py            # STAC landing page: links, conformance classes
│   ├── odata.py           # OData service document / $metadata probe
│   └── cache.py           # on-disk cache with TTL (default 24 h), offline fallback
├── auth/
│   ├── base.py            # CredentialsProvider protocol
│   ├── keycloak.py        # JWT: login, refresh, on-disk token cache (0600)
│   └── s3_keys.py         # S3 Keys Manager client: create/list/revoke; pins /v1
├── catalogue/
│   ├── base.py            # Catalogue protocol: search(), get(), collections()
│   ├── stac.py            # STAC client
│   ├── odata.py           # OData client ($filter builder, pagination)
│   └── query.py           # protocol-agnostic Query → translated per backend
├── eodata/
│   ├── base.py            # Downloader protocol: resume, retry, checksum, progress
│   ├── zipper.py          # HTTP backend; version-aware routes (V1/V2)
│   └── exos.py            # S3 backend (boto3/aioboto3); keys via auth.s3_keys
├── transport.py           # shared httpx session: retries, backoff, rate limiting,
│                          #   User-Agent, route() URL builder
├── doctor.py              # health checks: config, discovery, auth, service probes
└── cli/
    ├── main.py            # typer app: eo <command>
    └── commands/
        ├── auth.py        # login, logout, status
        ├── search.py
        ├── download.py    # supports stdin piping: `eo download -`
        ├── keys.py        # create, list, revoke, --export
        ├── config.py      # init, show, set, use, profiles
        ├── discover.py    # print/refresh discovery document
        └── doctor.py      # eo doctor [--profile X] [--json]
```

## 5. Domain model

All models are pydantic, protocol-agnostic, and returned identically by both
catalogue backends.

- **`Query`** — collection, bbox, datetime range, free attribute filters, limit,
  sort. Translated by `catalogue/stac.py` into a STAC ItemSearch body and by
  `catalogue/odata.py` into an `$filter` expression. Unsupported constructs per
  backend raise `UnsupportedQueryFeature` early, with a message naming the backend.
- **`Product`** — normalized item: `id`, `name`, `collection`, `size`,
  `geometry`, `datetime`, `cloud_cover` (optional), `checksum` (algorithm + value,
  when the catalogue provides it), plus the identifiers each download backend
  needs (S3 object path for Exos, product id/URL for Zipper). Raw backend payload
  retained under `Product.raw` for power users.
- **`SearchResult`** — lazy, **re-iterable** sequence of `Product`: pages are
  fetched on demand and cached, so it can be traversed more than once (e.g.
  downloaded, then inspected) without re-querying. Transparent pagination
  (STAC paging links / OData `$skiptoken` or `$skip`); `len()` where the backend
  provides a count; `.pages()` for page-wise access.
- **`Collection`** — id, title, description, temporal/spatial extent.

## 6. Module specifications

### 6.1 Configuration (`eosdk.config`)

**Endpoint model** (single source of truth for a deployment's shape):

```python
class Endpoints(BaseModel):
    catalogue_stac: HttpUrl
    catalogue_odata: HttpUrl
    zipper: HttpUrl
    exos_endpoint: HttpUrl          # S3 endpoint_url
    exos_region: str = "default"
    keys_manager: HttpUrl
    keycloak: HttpUrl
    keycloak_realm: str = "eodata"
    discovery_url: HttpUrl | None = None   # overrides {platform}/.well-known/... derivation
```

**Resolution precedence** (most specific wins):

1. explicit kwargs to `Client(...)`
2. environment variables `EOSDK_*` (e.g. `EOSDK_PROFILE`, `EOSDK_ZIPPER_URL`)
3. project-local `./eosdk.toml`
4. user config `~/.config/eosdk/config.toml` (selected profile)
5. remote discovery document (when a `platform` root is configured)
6. built-in defaults for known deployments

**Profiles** — named endpoint sets in the config file; `default_profile` key;
a profile may define only `platform = "<root URL>"` and let discovery fill the
rest, or pin any subset of endpoints manually (pins beat discovery):

```toml
default_profile = "prod"

[profiles.prod]
platform = "https://platform.example.eu"

[profiles.staging]
platform = "https://staging.example.eu"
zipper   = "https://zipper-canary.example.eu"   # pinned; rest discovered

[profiles.local]                                 # fully manual
catalogue_stac  = "http://localhost:8081/stac"
catalogue_odata = "http://localhost:8081/odata"
zipper          = "http://localhost:8082"
exos_endpoint   = "http://localhost:9000"
keys_manager    = "http://localhost:8083/api"
keycloak        = "http://localhost:8180"
keycloak_realm  = "eodata"
```

**Introspection** — `client.config.resolved()` returns every endpoint together
with its source (`kwargs`, `env:EOSDK_ZIPPER_URL`, `profile:prod`, `discovery`,
`default`); `eo config show` prints the same.

**Validation** — URL syntax of all *locally-known* endpoints (kwargs, env, config
files) is validated at `Client` construction (offline, cheap). Endpoints that can
only come from remote discovery (e.g. a profile defining just `platform`) are
**not** fetched at construction; they are resolved and validated lazily on first
use of the owning service, or eagerly via `client.discovery.refresh()`. Until
resolved, `client.config.resolved()` reports such endpoints with source
`discovery` and value `<pending>`. Reachability/capability checks are always
deferred to first use of each service and to `eo doctor`.

### 6.2 Discovery (`eosdk.discovery`)

**Platform document** — a single document describes the whole deployment. The
SDK derives its URL from the configured `platform` root:

```
GET {platform}/.well-known/eo-services.json
```

**Document location** — three supported setups, one derivation rule:

1. **Dedicated subdomain (recommended)** — e.g.
   `platform = "https://discovery.example.eu"`. The subdomain owns its domain
   root, so the well-known path is unambiguous (no contention over `/` on a
   shared domain or gateway). It adds DNS-level indirection — the record can
   point at a static bucket, CDN, or minimal web server and move without any
   SDK or config change — and composes per environment
   (`discovery.staging.example.eu`). Since the document is static JSON, CDN or
   object-storage hosting makes the bootstrap path cheap, highly available,
   and decoupled from application deployments. Costs: one DNS record, one TLS
   certificate (trivial with wildcard/ACME), monitoring of one more host, and
   CORS headers (`Access-Control-Allow-Origin: *`) if browser clients ever
   read it.
2. **Main domain root** — e.g. `platform = "https://platform.example.eu"` with
   the file served at its `/.well-known/` path (RFC 8615 convention, same
   pattern as Keycloak's OIDC discovery). Fine when the platform controls the
   domain root. Note `eo-services.json` is not an IANA-registered well-known
   suffix — common practice, not standards-pure — and `.well-known` is
   host-scoped, so this option degrades when the platform lives under a path
   prefix or behind a gateway owned by another team.
3. **Explicit override** — `discovery_url` in config /
   `EOSDK_DISCOVERY_URL` env var pointing at any stable, unauthenticated URL
   (e.g. `{platform}/api/v1/discovery`), for deployments where neither
   well-known root is available. When set, it bypasses derivation entirely.

Regardless of host, the document is served at the well-known path (not only at
`/`), so the derivation rule stays uniform: take `platform`, append
`/.well-known/eo-services.json`. Whether `platform` points at the main domain,
a dedicated subdomain, or a staging host is an operational decision, invisible
to the SDK. Resolution order for the discovery URL itself:
`discovery_url` (kwargs/env/profile) > derived from `platform`.

```json
{
  "version": "1.0",
  "services": {
    "catalogue":    { "stac": { "url": "https://catalogue.example.eu/stac" },
                      "odata": { "url": "https://catalogue.example.eu/odata",
                                 "api_version": "v1" } },
    "zipper":       { "odata": { "url": "https://zipper.example.eu/odata",
                                 "api_version": "v1",
                                 "capabilities": ["download", "list"] },
                      "resto": { "url": "https://zipper.example.eu/download",
                                 "capabilities": ["download"],
                                 "deprecated": true,
                                 "sunset": "2027-01-01",
                                 "replacement": "odata" } },
    "exos":         { "endpoint": "https://s3.example.eu", "region": "default" },
    "keys_manager": { "url": "https://keys.example.eu/api", "api_version": "v1" },
    "auth":         { "issuer": "https://auth.example.eu/realms/eodata" }
  }
}
```

A service that exposes several coexisting endpoint families advertises them as
named **strategies** rather than a single `url` — here Zipper offers `odata`
(current) and `resto` (deprecated, sunsetting). The module picks by its
preference order and per-strategy deprecation (see §6.3); each strategy carries
its own `url` and, where the SDK owns the version, its own `api_version`.

**Mapping to the `Endpoints` model.** The flat fields in §6.1 hold per-service
*base* URLs; the nested discovery shape is projected onto them at parse time:
`auth.issuer` → `keycloak` + `keycloak_realm` (split on `/realms/`),
`exos.endpoint` / `region` → `exos_endpoint` / `exos_region`, and
`catalogue.stac` / `.odata` → the two catalogue fields. A service with several
strategies collapses onto its single base field (`zipper`): the SDK stores one
base and appends each strategy's route template itself (§6.3). Precedence is
resolved per service — a locally pinned base (kwargs/env/config, which beats
discovery) is used together with the SDK's built-in strategy set; absent a pin,
the discovered per-strategy `url`s are used directly.

A strategy may declare a `capabilities` list (`["download", "list", "open"]`) to
**gate** what the SDK will attempt through it (§6.6) — e.g. `resto`:
`["download"]`. This field is an **intersection gate, never a source of truth**:
the SDK's built-in capability matrix (§6.6) is authoritative for what a
backend/strategy *can* do, and the discovery list can only **restrict** that set,
never extend it. The effective capability set is
`built_in(code) ∩ capabilities(discovery)` when the field is present, and
`built_in(code)` when it is absent. Advertising a capability the SDK does not
implement has no effect (the code must exist); the field's sole purpose is to let
a deployment **disable** a capability it already supports, without an SDK change
(e.g. temporarily turning off `open` on Exos during an incident). Whole-strategy
disable is instead expressed via `deprecated`/`sunset`/availability (see below);
`capabilities` exists only for the finer grain of switching off one operation on
an otherwise-live endpoint (see §14 open question — confirm this granularity is
actually required before relying on it).

`api_version` is optional (see §6.3): services — or strategies — whose protocol
versioning is owned externally, such as Exos over S3, omit it.

The document may additionally carry deprecation notices
(`"deprecated": true, "sunset": "2027-01-01", "replacement": "..."`), which the
SDK surfaces as warnings. These may be declared per service or per **download
strategy** where a service exposes several coexisting endpoint families (e.g.
Zipper's `resto` strategy marked deprecated while `odata` is current — see §6.3),
so the SDK can prefer the current strategy and warn only when it falls back to a
sunsetting one.

**Document schema version** — the top-level `version` identifies the document
*schema*, distinct from any service's `api_version`. The SDK supports a known
major version; a document whose major version it does not recognize is rejected
with a clear error rather than partially parsed. Unknown service or strategy keys
are ignored, so the format stays forward-compatible.

**Per-service standard discovery** (used with or without the platform document):

- Keycloak: everything resolved from
  `{keycloak}/realms/{realm}/.well-known/openid-configuration`
  (token, auth, device endpoints). Config holds only base + realm.
- STAC: landing page is self-describing — search endpoint and conformance
  classes read from the root; SDK verifies required conformance
  (item-search; filter where used).
- OData: service document / `$metadata` used to validate the base URL on first use.

**Caching** — on-disk cache (default TTL 24 h) with fallback to last known good
document when offline; `client.discovery.refresh()` / `eo discover --refresh`
bust the cache.

### 6.3 API versioning

- Users never put API versions in config or discovery URLs. Service URLs are
  **version-free bases**; each client module pins the API version(s) it supports
  and appends the version segment itself via `route()`
  (e.g. `keys_manager` base + `/v1` lives in `auth/s3_keys.py`).
- If a configured or discovered base already contains a version segment
  (e.g. ends in `/v1`), the SDK does **not** strip it — this is a configuration
  error that would produce `…/v1/v1`. `eo doctor` flags a base whose final path
  segment matches `v\d+` as a likely mistake.
- `api_version` is **optional** per service in the discovery document. Services
  whose protocol versioning is owned externally (e.g. Exos over S3/boto3) omit it
  and have no version-aware routes.
- When `api_version` is **present**, the SDK checks compatibility at startup /
  first use and raises `UnsupportedApiVersion` with an actionable message
  ("zipper advertises v3; this SDK supports v1–v2 — upgrade eosdk or pin
  EOSDK_ZIPPER_URL"), instead of failing mid-download with a 404.
- When `api_version` is **absent** (omitted from the document, or no discovery
  document available), a version-aware module falls back to its **default
  supported version** (the newest it implements) rather than failing. This is
  also the Phase-1 path, before discovery exists.
- `api_version` governs only the **version within a route strategy**, which is a
  separate axis from *which endpoint family* serves an operation. A single
  service can expose several coexisting strategies — e.g. Zipper serves downloads
  via both the modern `odata` strategy (the OData `$value` endpoint,
  `/odata/v1/Products({id})/$value`) and the `resto` strategy (the legacy
  `/download/{id}` endpoint that is being decommissioned). These are not versions
  of each other.
- A module holds an **ordered preference of strategies** and selects
  **capability-first, then by preference**: for the requested operation, filter
  to strategies that both (a) are available and not past `sunset` and (b) support
  that capability (§6.6), then pick the highest-preference survivor. Zipper's
  preference is `odata` first, `resto` as fallback — but `resto` supports only
  the `download` capability, so it is a valid fallback for downloads and never
  for `list`/`open`.
  If no available strategy supports the requested capability, the SDK raises
  `UnsupportedCapability` (§8) rather than silently degrading. If the selected
  strategy is deprecated, the SDK emits a `DeprecationWarning` naming the sunset
  date and replacement (see §6.2). Availability and deprecation come from
  discovery; absent discovery (the Phase-1 path), the module defaults to its
  first preference, overridable via config/env, with `eo doctor` surfacing an
  unreachable choice.
- Within the chosen strategy, `api_version` then selects the route template
  (e.g. OData `$value` v1 vs a future v2). Breaking changes are absorbed inside
  the module this way; the public SDK surface does not change. A genuinely
  different download implementation is instead a new `Downloader` (§6.6) selected
  by `via=` and registerable via the `eosdk.plugins` entry points (§11).

### 6.4 Auth (`eosdk.auth`)

The auth module owns all credential lifecycles. Catalogue and download backends
declare which `CredentialsProvider` they need; the client wires it up.

**Keycloak (JWT)** — used by the catalogue, Zipper, and S3 Keys Manager:

- Login flows: username/password (resource owner) and device flow (for headless
  CLI use); endpoints from OIDC discovery.
- Refresh token cached on disk at `~/.config/eosdk/tokens/` with mode `0600`,
  keyed by profile; access tokens refreshed transparently before expiry.
- On HTTP 401 from any service: one forced refresh + retry, then `AuthError`.
- `eo auth login / logout / status`.

**S3 keys (Exos)** — via S3 Keys Manager (itself authenticated with the JWT):

- `S3KeysProvider.get_or_create(label=...)` — default policy: reuse a labeled
  key pair per profile.
- Alternative policy exposed explicitly: ephemeral keys per session with
  revoke-on-exit (context manager).
- Operations: create, list, revoke; `eo keys create --label X`, `eo keys list`,
  `eo keys revoke <id>`.
- `eo keys create --export` emits `AWS_ACCESS_KEY_ID=... / AWS_SECRET_ACCESS_KEY=...`
  lines for interop with plain `aws s3`, rclone, etc. Secrets are never printed
  otherwise (masked in logs and `status` output).

### 6.5 Catalogue (`eosdk.catalogue`)

- `Catalogue` protocol: `search(query) -> SearchResult`, `get(id) -> Product`,
  `collections() -> list[Collection]`.
- `protocol="stac" | "odata"` selectable per call; default from config.
- Both backends translate the shared `Query` object (see §5) and normalize
  results to `Product`.
- Transparent pagination in `SearchResult`; per-page fetch uses the shared
  transport (retries/backoff included).
- Escape hatch for power users: `ODataCatalogue.query_raw("Products?$filter=...")`
  and access to the raw STAC ItemSearch parameters.

### 6.6 Data access (`eosdk.eodata`)

Backends are not uniform in what they can do, so capabilities are split into one
required protocol plus optional ones; a backend (or strategy, §6.3) implements
only what it supports:

- `Downloader` **(required)** — the `download` capability: `fetch(products,
  target, *, concurrency, resume, checksum, progress)` (facade: `client.download`
  / `eo download`); `products` accepts a single `Product` or an iterable of
  `Product`.
- `Listable` *(optional)* — the `list` capability: `list(product, path="", *,
  recursive=False) -> list[Node]` — traverses a product's **internal** file tree
  (the SAFE/archive node structure), not the catalogue. Repository/product search
  stays the catalogue's job (§6.5); this is the "what files are inside this
  product" operation that enables selective and partial download.
  - **Single-level by default.** A call returns the *immediate children* of
    `path` (root when `path=""`). This is the common denominator both backends
    serve natively and cheaply: OData `Nodes(path)/Nodes` (one request per
    directory) and S3 `ListObjectsV2(prefix=path, Delimiter="/")` (one paginated
    request). The public contract is anchored here so that behaviour and cost are
    uniform across backends.
  - **Recursive is opt-in and *not* uniform cost.** `recursive=True` returns the
    whole subtree, but the two backends realize it very differently: Exos does a
    native prefix walk (~one paginated `ListObjectsV2` over the product prefix),
    while Zipper must issue **one request per directory** (BFS/DFS over the
    `Nodes` hierarchy). Each backend overrides recursion with its optimal
    strategy rather than inheriting a generic per-level fan-out; the performance
    asymmetry is documented, not hidden. `list()` never implicitly walks the full
    tree — callers ask for it explicitly.
  - **Directory semantics are normalized into `is_dir`.** S3 has no real
    directories: `is_dir=True` is derived from `CommonPrefixes` (and zero-byte
    "folder" keys); OData container nodes map directly. Corner cases (an empty
    directory that exists in OData but has no S3 representation; a name that is
    both a file and a prefix) are resolved in the backend and documented there.
  - **Path/URL construction goes through `route()`** (§6.7). OData `Nodes(name)`
    segments embed node names *inside the URL key syntax*; names containing
    spaces, parentheses, or quotes must be OData-key-encoded, never assembled
    with f-strings.
- `RandomAccess` *(optional)* — the `open` capability: `open(product, path) ->
  file-like` — ranged reads of a single file inside a product, no full-product
  transfer. **Exos-only:** it depends on HTTP `Range` / ranged S3 GETs, which
  **Zipper does not support** (neither strategy). Zipper serves whole objects
  only — a full product (one or more files delivered as a zip) or a single
  file — so `open` is never available via `via="zipper"`.

Backend chosen via `via="zipper" | "exos"`. Capability names (`download`, `list`,
`open`) are the vocabulary used in discovery `capabilities` (§6.2) and in
`UnsupportedCapability`. Capability matrix:

| Backend / strategy | `download` | `list` | `open` |
|---|---|---|---|
| Zipper `odata`     | ✓ | ✓ | - |
| Zipper `resto`     | ✓ | — | — |
| Exos (S3)          | ✓ | ✓ | ✓ |

Requesting a capability a backend lacks raises `UnsupportedCapability` (§8); for
Zipper this interacts with capability-aware strategy selection (§6.3) — `resto`
is a valid fallback for `download` but never for `list`/`open`.

Zipper's `odata` strategy realizes these capabilities over the OData routes:
full-product download via `Products({id})/$value` (not compressed product) or the equivalent
`Products({id})/$zip` for compressed products, if available; single-file download via `Products({id})/Nodes({name})/$value`
and standalone `Assets({id})/$value`; and listing via `Products({id})/Nodes`
(one request per directory, §6.3). All of these return whole objects — there is
no ranged/partial read (see `open` above).

- **`Node`** model — `name`, `path`, `size`, `is_dir`, `checksum` (optional,
  when the backend provides it), `raw` (backend payload escape hatch, mirroring
  `Product.raw`). `path` is a **logical, backend-agnostic** path within the
  product (e.g. `GRANULE/L2A_.../IMG_DATA/R10m/..._B04.jp2`); each backend is
  responsible for translating it to its own addressing — the OData
  `Nodes(a)/Nodes(b)/…` chain for Zipper, the S3 key suffix under the product
  prefix for Exos — exactly as `Product` already carries per-backend identifiers
  (§5). Returned by `list()`; the same `path` value feeds `open()` and selective
  `fetch()` identically regardless of `via=`.
- Common transfer features implemented once in `base.py`, inherited by all
  backends (distinct from the `download`/`list`/`open` **capabilities** above):
  - **Resume**: ranged multipart GET (**Exos only**). **Zipper does not support
    HTTP `Range` requests**, so an interrupted Zipper transfer is restarted from
    the beginning rather than resumed; the feature is a no-op on that backend.
  - **Retries**: exponential backoff with jitter (tenacity), idempotent-safe.
  - **Checksum verification** against catalogue metadata when available.
  - **Progress callback** (bytes done/total per product + aggregate); the CLI
    plugs a rich progress bar into it.
  - **Concurrency**: bounded worker pool across products; per-file parallel
    ranges for large objects (Exos).
- **Partial access** without full-product transfer:
  `client.open(product, path="GRANULE/.../B04.jp2", via="exos")` → file-like
  object backed by ranged S3 GETs
- Zipper backend injects the JWT per request; Exos backend receives S3
  credentials from `S3KeysProvider` and never sees Keycloak tokens.

### 6.7 Transport (`eosdk.transport`)

- Single shared `httpx` client (sync + async variants): connection pooling,
  timeouts, retry/backoff policy, optional client-side rate limiting,
  `User-Agent: eosdk/<version> (<python>; <platform>)`.
- `route(template, **params)` URL builder — the only sanctioned way to build
  service URLs (proper encoding; no scattered f-strings).

### 6.8 Doctor (`eosdk.doctor`)

- A list of small check functions returning
  `CheckResult(name, ok, detail, hint)`, grouped into sections:
  **Config** (file found, profile valid, URL syntax), **Discovery** (platform
  document fetch/cache age, API version compatibility), **Auth** (OIDC
  discovery, token cache validity, active S3 key pairs), **Services**
  (STAC landing page + conformance, OData service document, Zipper `HEAD`,
  Exos connectivity).
- Each domain module contributes its own probe, so a future access service
  brings its doctor check along with it.
- Checks degrade gracefully: a section whose subsystem is not configured or not
  yet shipped (e.g. **Discovery** / API-version checks before discovery lands in
  §13 Phase 3) reports **skipped** with a reason instead of failing, so `eo
  doctor` is useful from Phase 2 onward and never emits a false ✗.
- CLI: `eo doctor [--profile X] [--json]`; human output with ✓/✗ and hints,
  `--json` for monitoring; non-zero exit code on any failure so it works as a
  pipeline pre-flight step (`eo doctor && eo download ...`).

## 7. Public interface examples

### 7.1 Library — the 90% case

```python
from eosdk import Client

client = Client(profile="prod")          # offline: local config/env resolved & validated;
                                         # discovery-sourced endpoints resolved lazily on first use

products = client.search(
    collection="SENTINEL-2",
    bbox=(22.5, 52.9, 24.0, 53.5),
    datetime="2026-06-01/2026-06-30",
    filters={"cloudCover": "<20"},
    protocol="stac",                     # or "odata"
    limit=50,
)

client.download(products, target="./data", via="zipper",
                concurrency=4, resume=True, checksum=True)
# ...or fetch the same results over S3 instead: via="exos" (S3 keys auto-managed)

product = next(iter(products))               # SearchResult is re-iterable (pages cached)
nodes   = client.list(product, via="zipper") # files inside the product (odata strategy)
band    = next(n for n in nodes if n.path.endswith("B04.jp2"))

with client.open(product, path=band.path, via="exos") as f:
    data = f.read()                      # ranged read, no full download
```

### 7.2 Library — configuration and discovery

```python
Client()                                             # built-in defaults
Client(profile="staging")
Client(platform="https://platform.example.eu")       # single-root bootstrap
Client(profile="prod",
       endpoints={"zipper": "http://localhost:8080"})  # surgical override

client.config.resolved()      # endpoint -> (value, source) mapping
client.discovery.services()   # parsed discovery document
client.discovery.refresh()    # bust TTL cache
```

### 7.3 Library — lower level

```python
from eosdk.auth import KeycloakAuth, S3KeysProvider
from eosdk.catalogue import ODataCatalogue
from eosdk.eodata import ExosDownloader

auth = KeycloakAuth(url=..., realm=..., client_id=..., username=...)
cat  = ODataCatalogue(base_url=..., auth=auth)
raw  = cat.query_raw("Products?$filter=contains(Name,'S1A') and ...")  # escape hatch
prod = cat.get("S2B_MSIL2A_20260615T095029_...")                       # normalized Product

keys = S3KeysProvider(auth=auth).get_or_create(label="my-pipeline")
dl   = ExosDownloader(endpoint=..., credentials=keys)
dl.fetch(prod, target="/data", concurrency=8)
```

### 7.4 CLI

```bash
# configuration & profiles
eo config init                       # interactive: platform URL or manual endpoints
eo config profiles                   # list profiles, mark default
eo config show --profile prod        # resolved endpoints + their source
eo config set profiles.staging.zipper https://zipper-canary.example.eu
eo config use staging

# discovery & diagnostics
eo discover --profile prod           # fetch + print discovery document
eo discover --refresh
eo doctor --profile prod             # health checks; exit code != 0 on failure
eo doctor --json

# auth & keys
eo auth login                        # device flow or user/pass -> cached token
eo auth status
eo keys create --label my-pipeline
eo keys list
eo keys revoke <key-id>
eo keys create --export              # AWS_ACCESS_KEY_ID=... for aws/rclone interop

# search & download
eo search --collection SENTINEL-1 \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "productType=GRD" \
          --protocol odata --json

eo download S2B_MSIL2A_20260615T095029_... -o ./data --via zipper
eo search ... --json | eo download - --via exos -c 8     # pipe search -> download
```

`--json` on read commands and stdin piping (`eo download -`) make the CLI
composable in shell pipelines and cron jobs.

## 8. Error handling

Exception taxonomy (all inherit `EosdkError`):

| Exception | Raised when | Message must include |
|---|---|---|
| `AuthError` | login/refresh failure, 401 after forced refresh | realm, profile |
| `EndpointUnreachable` | connect/timeout on first use or doctor | service, URL, hint (profile/env var to check) |
| `UnsupportedApiVersion` | advertised version outside supported range | service, advertised vs supported, remediation |
| `UnsupportedQueryFeature` | Query construct not expressible in backend | backend, feature |
| `UnsupportedCapability` | no available strategy of the chosen backend supports the requested capability (`download`/`list`/`open`) — usually `list`/`open`, or `download` when every strategy is past `sunset`/unavailable | backend, capability, which strategy/endpoint would provide it |
| `ProductNotFound` | catalogue get/download miss | product id, backend |
| `DownloadError` | transfer failure after retries | product, backend, last cause |
| `QuotaExceeded` | service-side 429/quota | service, retry-after if provided |

Principles: fail early (version guard, query translation) rather than
mid-transfer; every error names the failing service and, where possible, the
config knob that fixes it.

## 9. Security considerations

- Refresh tokens and cached discovery documents stored under
  `~/.config/eosdk/` with file mode `0600`; per-profile separation.
- Secrets never printed to stdout/logs (masked), except via the explicit
  `eo keys create --export` opt-in.
- S3 key revocation available in both API and CLI; ephemeral-key policy
  revokes on context exit.
- TLS verification on by default; opt-out only via explicit
  `verify=False` / `EOSDK_TLS_VERIFY=0` for local development, with a warning.
- No telemetry in v1 (open question §14).

## 10. Technology stack

| Concern | Choice | Notes |
|---|---|---|
| HTTP | `httpx` | one library for sync + async |
| S3 | `boto3` / `aioboto3` | Exos backend |
| Models/config | `pydantic` v2 | validation; TOML read `tomllib` (3.11+; `tomli` on 3.10), write `tomlkit` (round-trips comments/layout for `eo config init`/`set`) |
| CLI | `typer` + `rich` | progress bars from the download callback |
| Retries | `tenacity` | shared policy in transport |
| STAC | thin custom client or `pystac-client` | decide in Phase 1 spike |
| Testing | `pytest`, `respx`/`responses`, `moto` | HTTP + S3 mocking |
| Packaging | `pyproject.toml`, `hatchling` (or `uv`) | PyPI, semver |

Python ≥ 3.10 (pattern matching, `tomllib` in 3.11 — vendor fallback for 3.10).

## 11. Extensibility

- Entry-point group `eosdk.plugins`: third parties can register additional
  catalogue protocols or download backends discovered at runtime.
- New access service = new module implementing `Downloader` (+ its
  `CredentialsProvider` if needed) + its doctor probe. No changes to `Client`.
- `AsyncClient` shares the domain modules via the dual sync/async transport.

## 12. Testing strategy

- **Unit**: query translation (Query → STAC / OData, golden cases incl.
  unsupported features), config resolution precedence matrix, token refresh
  state machine, resume/offset logic, doctor checks.
- **Contract/integration**: `respx` fixtures replaying real service responses
  (catalogue pages, discovery documents, Keycloak OIDC); `moto` for the Exos/S3
  path; optional live smoke suite gated by env vars against a staging platform.
- **CLI**: `typer` runner tests; `--json` output schema snapshots.
- CI: lint (ruff), type-check (mypy, strict on `eosdk/`), tests on 3.10–3.13.

## 13. Roadmap

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 — Foundations | repo scaffolding, `transport`, `config` loader + profiles, `models`, exceptions, CI | `Client(profile=...)` resolves endpoints; unit tests green |
| 1 — MVP | Keycloak auth (login/refresh/cache), STAC search, Zipper download (resume/retry/checksum/progress), CLI: `auth`, `search`, `download`, `config` | end-to-end: search → download via Zipper from CLI and library |
| 2 — Full access | OData backend, S3 Keys Manager client, Exos backend (+ `open()` partial reads), `eo keys`, `eo doctor` | feature parity across protocols/backends; doctor green on staging |
| 3 — Platform | discovery (platform doc, OIDC, STAC/OData probes, cache), version guard, `eo discover`, `AsyncClient`, plugin entry points | single-root bootstrap works; async parity |
| 4 — Hardening | docs site, live smoke tests, performance pass on bulk downloads, 1.0 release to PyPI | 1.0.0 published |

## 14. Open questions

1. **Naming** — `eosdk` is a working name; check PyPI availability and branding.
2. **Discovery document ownership** — which team publishes and maintains
   `/.well-known/eo-services.json`? Fallback plan if it cannot be added:
   per-service standard discovery only (§6.2).
3. **Default S3 key policy** — labeled-reuse vs ephemeral per session; proposal
   is labeled-reuse per profile, confirm with security.
4. **Quotas/rate limits** — do services expose limits/headers the SDK should
   respect proactively (beyond honoring 429 + Retry-After)?
5. **STAC client** — thin custom implementation vs `pystac-client` dependency
   (spike in Phase 1).
6. **Keycloak client registration** — dedicated public client for the SDK
   (device flow enabled) vs reuse of an existing client id.
7. **Telemetry** — anonymous usage metrics: out for v1, revisit later.
8. **License & repository hosting** — internal vs open source.
9. **Discovery `capabilities` granularity** — is sub-strategy capability disabling
   (turning off one operation on an otherwise-live endpoint, §6.2) a real
   operational need? Whole-strategy disable is already covered by
   `deprecated`/`sunset`. If the finer grain is not required, drop the
   `capabilities` field entirely (discovery carries only URLs + `api_version`) and
   rely on the built-in matrix; if it is, consider modelling it as opt-*out*
   (`disabled_capabilities`) so a deployment names only what it switches off and
   cannot silently omit an intrinsic capability.

---

*End of specification v0.1 — intended as the seed `SPEC.md` for the repository;
sections 6–8 should evolve into per-module design docs as implementation starts.*