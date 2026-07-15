# eosdk

Python SDK and CLI (`eo`) for Earth Observation services: unified catalogue search
(STAC / OData), EOData downloads (HTTP / S3), and fully managed authentication
(Keycloak JWT, S3 key lifecycle) behind one coherent interface — *search, then download*.

> Status: pre-release, under active development. See [SPEC.md](SPEC.md) for the full
> specification and roadmap.

## What it gives you

- **Catalogue search** — one API over both STAC and OData catalogues. Filter by
  collection, bounding box, time range and attributes (e.g. cloud cover); results
  come back as plain products you can page, sort, and pipe.
- **EOData downloads** — pull products through the HTTP backend or the S3
  S3 backend with a single call. Concurrency, checksum verification, and resume are
  handled for you.
- **Managed authentication** — Keycloak login (device flow or username/password),
  token refresh, session caching, and the full S3 access-key lifecycle, all invisible
  once you've logged in.
- **Two surfaces, one core** — a scriptable `eo` CLI whose `search --format json` pipes
  straight into `download`, and a typed Python library that shares the same session,
  config, and endpoints.
- **Config that resolves itself** — endpoints come from a platform's service-discovery
  document, per-field precedence (kwargs → env → project file → profile → discovery →
  built-in defaults), and named profiles for switching between deployments.

## Why it's cool

- **Ships pointed at CDSE out of the box.** The Copernicus Data Space Ecosystem is the
  built-in default — no endpoints to configure before your first search.
- **Search, then download, as one pipeline.** `eo search --collection … --format json |
  eo download -` is the whole workflow; JSON Lines interoperate cleanly with `jq`, `head`,
  and the rest of the Unix toolbox.
- **Auth you never think about.** Log in once; the session is cached per profile and
  reused by every later CLI *and* library call. S3 keys for the S3 backend are minted
  automatically when needed.
- **Backend details stay out of your way.** HTTP vs. S3, STAC vs. OData — pick
  one flag; the SDK owns the protocol, retries, and credential plumbing.
- **Typed and strict.** Pydantic models, `py.typed`, and a strict-mypy codebase, so the
  library is pleasant to build on.

## How to use it

The examples below use CDSE (the built-in default). Search is anonymous; **downloads
require a login**, so you'll want a Copernicus Data Space account.

### For general users (the `eo` CLI)

**Step 1 — Get a platform account.** Register for a free Copernicus Data Space
Ecosystem account at **https://dataspace.copernicus.eu/** (use the *Register* / *Sign
up* link). Confirm your email; those are the credentials you'll log in with below.

**Step 2 — Install the CLI.** Requires [uv](https://docs.astral.sh/uv/):

```bash
git clone <repo-url> && cd eosdk
uv tool install --editable .   # makes `eo` available everywhere
eo --help
```

(See [Installation](#installation) for virtualenv-only alternatives.)

**Step 3 — Set up the platform (optional on CDSE).** CDSE is the default deployment and
its endpoints ship built in, so you can skip straight to logging in. To target a
different platform, or just to see what endpoints resolve to, create a profile from a
platform root — the rest of the endpoints are discovered from it:

```bash
eo config init --platform https://platform.example.eu   # discover endpoints from a root
eo config show                                          # every endpoint + its source
```

> CDSE's dedicated discovery document will be published at
> **`https://discover.dataspace.copernicus.eu`** (not live yet — until then the built-in
> defaults cover CDSE). Once available you'll be able to bootstrap explicitly with
> `eo config init --platform https://discover.dataspace.copernicus.eu`.

See [Setting up the platform](#setting-up-the-platform) for profiles, env-var overrides,
project-local config, and pointing at other deployments.

**Step 4 — Log in.** Use username/password against CDSE (the password is prompted, so
it never lands in your shell history):

```bash
eo auth login --username you@example.com   # prompts for the password
eo auth status                             # confirms the cached session
```

> The bare `eo auth login` (OAuth device flow) is the default when a realm supports it,
> but CDSE's public client currently has that grant disabled (Keycloak returns HTTP 400
> `unauthorized_client`) — use `--username` on CDSE. For CI/services, pipe the password
> in with `--password-stdin`. See [examples/cli/02_auth.sh](examples/cli/02_auth.sh).

**Step 5 — Search the catalogue.** A human-readable table by default:

```bash
eo search \
  --collection sentinel-2-l2a \
  --bbox 22.5,52.9,24.0,53.5 \
  --from 2026-06-01 --to 2026-06-30 \
  --filter "cloudCover=<20" \
  --sort "-datetime" \
  --limit 10
```

> Unbounded searches are refused — give at least one of
> `--collection` / `--bbox` / `--from`+`--to` / `--filter`.

**Step 6 — Download.** Pipe `search --format json` (one product per line) straight
into `download`:

```bash
eo search --collection sentinel-2-l2a \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "cloudCover=<10" \
          --limit 2 --format json \
  | eo download - --via http --output ./data --concurrency 4
```

Stop a running download with `Ctrl+C`: queued products are dropped and in-flight
transfers abort. Re-running the same command picks the batch up again — `--via s3`
resumes partially downloaded files, `--via http` restarts them from scratch.

Collection ids are backend vocabulary. STAC (the default) uses product-level ids like
`sentinel-1-grd` or `sentinel-2-l2a`; mission-level names like `SENTINEL-1` belong to
OData (`--protocol odata`). List what a catalogue offers with `eo discover`.

### For developers (the Python library)

The library reuses the session cached by `eo auth login`, so most programs never log in
themselves — the same six steps apply, in code.

**Step 1 — Get a platform account.** Same as above: register at
**https://dataspace.copernicus.eu/**.

**Step 2 — Install into your project.** Requires [uv](https://docs.astral.sh/uv/):

```bash
git clone <repo-url> && cd eosdk
uv sync            # create venv + install dependencies
```

**Step 3 — Point the client at a platform (optional on CDSE).** `Client()` defaults to
CDSE. To use another deployment, pass its root and endpoints are discovered from it:

```python
from eosdk import Client

client = Client()                                        # CDSE defaults
# client = Client(platform="https://platform.example.eu")  # another deployment
# client = Client(profile="prod")                          # a saved profile
```

See [Setting up the platform](#setting-up-the-platform) for all the ways to point the
SDK at a deployment (profiles, env vars, project config, in-code kwargs).

**Step 4 — Log in (only if there's no cached CLI session).**

```python
import getpass
from eosdk import Client

client = Client()

if not client.auth.status().logged_in:
    client.auth.login("you@example.com", getpass.getpass())  # cached under ~/.config/eosdk/
```

**Step 5 — Search the catalogue.**

```python
products = client.search(
    collection="sentinel-2-l2a",
    bbox=(22.5, 52.9, 24.0, 53.5),
    datetime="2026-06-01/2026-06-30",
    filters={"cloudCover": "<20"},
    limit=50,
)
```

> STAC is the default protocol; pass `protocol="odata"` for mission-level collections.
> List what a catalogue offers with `client.collections()`.

**Step 6 — Download.**

```python
client.download(products, target="./data", via="http", concurrency=4)
```

See [examples/python/](examples/python/) for ranged reads, S3 keys, error handling,
raw-query escape hatches, and plugin backends.

## Setting up the platform

A "platform" is a deployment of EO services (catalogue, download, keys, Keycloak).
Endpoints resolve **per field**, most specific source wins:

```
Client kwargs  >  EOSDK_* env  >  ./eosdk.toml  >  user profile  >  discovery  >  built-in defaults
```

You rarely set every endpoint — point the SDK at a **platform root** and the rest are
discovered from `{platform}/.well-known/eo-services.json`. The options below go from
zero-config to fully manual; mix and match, since precedence is per field. Inspect the
result at any time with `eo config show` (or `client.config.resolved()`), which prints
every endpoint with the source that provided it.

### Option 1 — Built-in default (CDSE), nothing to configure

CDSE is the built-in deployment, so a fresh install already resolves to its catalogue,
download, keys, and Keycloak endpoints. Just log in and search.

```bash
eo config show    # shows the CDSE endpoints with source "default"
```

> A dedicated CDSE discovery document is coming at
> **`https://discover.dataspace.copernicus.eu`** (`{platform}/.well-known/eo-services.json`).
> It is not live yet, so the built-in defaults are what cover CDSE today; once it is
> published you can bootstrap from it explicitly with
> `--platform https://discover.dataspace.copernicus.eu` (or `EOSDK_PLATFORM` / the
> `platform` kwarg).

### Option 2 — A named profile from a platform root (recommended)

Create a profile from a single root URL; discovery fills in the rest. Profiles live in
`~/.config/eosdk/config.toml` and let you switch deployments by name.

```bash
eo config init --name prod --platform https://platform.example.eu   # discover the rest
eo config use prod                                                  # make it the default
eo --profile staging search ...                                     # or select per-invocation
eo config profiles                                                  # list; default is marked
```

### Option 3 — Pin or override individual endpoints

Pin a subset of endpoints (pins beat discovery); the rest still come from the platform
root. `eo config set` preserves file comments and layout.

```bash
eo config set profiles.staging.platform https://staging.example.eu
eo config set profiles.staging.eodata_http https://download-canary.example.eu  # pin beats discovery
eo config set default_profile staging
```

Equivalent hand-written `~/.config/eosdk/config.toml`:

```toml
default_profile = "prod"

[profiles.prod]
platform = "https://platform.example.eu"          # everything discovered from the root

[profiles.staging]
platform = "https://staging.example.eu"
eodata_http = "https://download-canary.example.eu" # pinned; rest discovered

[profiles.local]                                   # fully manual, no discovery
catalogue_stac  = "http://localhost:8081/stac"
catalogue_odata = "http://localhost:8081/odata"
eodata_http     = "http://localhost:8082"
s3_endpoint     = "http://localhost:9000"
s3_credentials  = "http://localhost:8083/api"
keycloak        = "http://localhost:8180"
keycloak_realm  = "eodata"
```

### Option 4 — Project-local `./eosdk.toml`

A file in the working directory beats the user profile (and loses to env vars) — handy
for pinning a repo to one deployment without touching global config:

```toml
# ./eosdk.toml
[profiles.default]
platform = "https://platform.example.eu"
```

### Option 5 — Environment variables (highest, after code)

Env vars override the config files — good for CI and surgical, one-off overrides:

```bash
export EOSDK_PLATFORM=https://platform.example.eu   # platform root; endpoints discovered
export EOSDK_PROFILE=staging                        # select a profile
export EOSDK_EODATA_HTTP_URL=http://localhost:8082  # override one endpoint only
```

Every endpoint has an `EOSDK_*_URL` variable (see
[src/eosdk/config/defaults.py](src/eosdk/config/defaults.py) for the full map).

### Option 6 — Directly in code (developers)

`Client(...)` kwargs win over everything else:

```python
from eosdk import Client

Client(platform="https://platform.example.eu")   # single-root bootstrap
Client(profile="prod")                            # a saved profile
Client(eodata_http="http://localhost:8082")       # pin one endpoint
```

### Where the discovery document lives

When you configure a `platform` root, the SDK fetches
`{platform}/.well-known/eo-services.json`. Three hosting setups, one derivation rule:

- **Dedicated subdomain** (recommended) — e.g. `https://discovery.example.eu`; the
  subdomain owns its root, so the well-known path is unambiguous and can be served from
  a static bucket or CDN. This is the pattern CDSE will use, at
  `https://discover.dataspace.copernicus.eu` (coming soon).
- **Main domain root** — e.g. `https://platform.example.eu` with the file at its
  `/.well-known/` path.
- **Explicit override** — set `discovery_url` (config) or `EOSDK_DISCOVERY_URL` (env) to
  any stable, unauthenticated URL; this bypasses derivation entirely.

To develop against discovery without a real platform, run the local Docker endpoint
described in [Local discovery endpoint (Docker)](#local-discovery-endpoint-docker) and
point the SDK at `http://localhost:8080`.

## Installation

Not yet published to PyPI. Requires [uv](https://docs.astral.sh/uv/).

Install `eo` as a uv tool to make it available everywhere (`--editable` picks up local
code changes without reinstalling):

```bash
git clone <repo-url> && cd eosdk
uv tool install --editable .
eo --help
```

Or install into the project virtualenv and run the CLI through `uv`:

```bash
uv sync
uv run eo --help
```

Or activate the virtualenv to call `eo` directly for the session:

```bash
source .venv/bin/activate
eo --help
```

## Examples

[examples/](examples/) contains runnable, commented examples for both surfaces:
[examples/python/](examples/python/) covers search, downloads, ranged reads,
auth and S3 keys, configuration, error handling, raw-query escape hatches, and
plugin backends; [examples/cli/](examples/cli/) are annotated `eo` walkthroughs
(search-pipe-download, auth, keys, profiles, discovery/doctor).

## Local discovery endpoint (Docker)

[docker/discovery/](docker/discovery/) contains an nginx image that serves a sample
service-discovery document at the well-known path
(`/.well-known/eo-services.json`) — useful for developing against discovery
without a real platform.

```bash
docker build -t eosdk-discovery docker/discovery
docker run --rm -p 8080:80 eosdk-discovery
curl http://localhost:8080/.well-known/eo-services.json
```

Point the SDK at it via the platform root:

```python
from eosdk import Client

client = Client(platform="http://localhost:8080")
```

To serve your own document instead of the baked-in sample
([docker/discovery/eo-services.json](docker/discovery/eo-services.json)), mount it over
the well-known path:

```bash
docker run --rm -p 8080:80 \
  -v "$(pwd)/my-services.json:/usr/share/nginx/html/.well-known/eo-services.json:ro" \
  eosdk-discovery
```

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync              # create venv + install all dependency groups
uv run pytest        # tests
uv run ruff check .  # lint
uv run mypy          # type check (strict)
```

Run smoke tests:

```bash
EOSDK_SMOKE=1 uv run pytest tests/smoke/
```

Note that the authenticated ones will also need real credentials configured:

```bash
EOSDK_SMOKE=1 \
EOSDK_SMOKE_USERNAME=you@example.com \
EOSDK_SMOKE_PASSWORD=... \
uv run pytest tests/smoke/
```

Attention: smoke tests create credentials in some tests; tear down step is included, but
it should not be run frequently as S3 credentials are not meant to be temporary.
