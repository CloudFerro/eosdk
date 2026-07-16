# eosdk

Python SDK and CLI (`eo`) for Earth Observation services: unified catalogue search
(STAC / OData), EOData downloads (HTTP / S3), and fully managed authentication
(Keycloak JWT, S3 key lifecycle) behind one coherent interface — *search, then download*.

> Status: pre-release, under active development. See [SPEC.md](SPEC.md) for the full
> specification and roadmap.

## Highlights

- **Three steps to start your EO adventure: register, install, explore.** Sign up for a
  platform account, install the `eo` CLI, and you're searching the catalogue.
- **Every EO service API in one place.** Catalogue, downloads, S3 keys, and auth sit
  behind a single interface — you never juggle individual service APIs. Need more?
  Configuration resolves per field, from a single platform root down to individually
  pinned endpoints.
- **Two modes, one eosdk.** No API knowledge needed: human-readable calls — `eo auth
  login`, `eo search`, `eo download` — cover the whole workflow. The most demanding
  users drop down to raw queries and the typed Python library, which shares the same
  session, config, and endpoints.
- **CDSE out of the box.** The Copernicus Data Space Ecosystem is the built-in default —
  no endpoints to configure before your first search.
- **Search, then download, as one pipeline.** `eo search --format json | eo download -`
  is the whole workflow; JSON Lines interoperate cleanly with `jq` and the Unix toolbox.
- **Auth you never think about.** Log in once; the session is cached per profile and
  reused by every later CLI *and* library call. S3 keys are minted automatically when
  needed.
- **Backend details stay out of your way.** HTTP vs. S3, STAC vs. OData — pick one flag;
  the SDK owns the protocol, retries, concurrency, checksums, resume, and credentials.
- **Typed and strict.** Pydantic models, `py.typed`, and a strict-mypy codebase.

## Quickstart (the `eo` CLI)

Examples use CDSE, the built-in default. Search is anonymous; **downloads require a
login**.

**1 — Register** for a free account at **https://dataspace.copernicus.eu/** and confirm
your email.

**2 — Install** (requires [uv](https://docs.astral.sh/uv/); see
[Installation](#installation) for alternatives):

```bash
git clone <repo-url> && cd eosdk
uv tool install --editable .   # makes `eo` available everywhere
eo --help
```

**3 — Log in** with username/password (the password is prompted, so it never lands in
your shell history):

```bash
eo auth login --username you@example.com
eo auth status
```

> The bare `eo auth login` (OAuth device flow) is the default when a realm supports it,
> but CDSE's public client currently has that grant disabled — use `--username` on CDSE.
> For CI, pipe the password in with `--password-stdin`.

**4 — Search.** A human-readable table by default:

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
> Collection ids are backend vocabulary: STAC (the default) uses product-level ids like
> `sentinel-2-l2a`; mission-level names like `SENTINEL-1` belong to OData
> (`--protocol odata`). List what a catalogue offers with `eo discover`.

**5 — Download.** Pipe `search --format json` (one product per line) straight into
`download`:

```bash
eo search --collection sentinel-2-l2a \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "cloudCover=<10" \
          --limit 2 --format json \
  | eo download - --via http --output ./data --concurrency 4
```

`Ctrl+C` stops the batch; re-running the same command picks it up again — `--via s3`
resumes partial files, `--via http` restarts them.

## The Python library

The library reuses the session cached by `eo auth login`, so most programs never log in
themselves. Install into a project with `uv sync` (or add the package as a dependency).

```python
import getpass
from eosdk import Client

client = Client()  # CDSE defaults; or Client(platform=...) / Client(profile=...)

if not client.auth.status().logged_in:
    client.auth.login("you@example.com", getpass.getpass())  # cached under ~/.config/eosdk/

products = client.search(
    collection="sentinel-2-l2a",           # protocol="odata" for mission-level ids
    bbox=(22.5, 52.9, 24.0, 53.5),
    datetime="2026-06-01/2026-06-30",
    filters={"cloudCover": "<20"},
    limit=50,
)

client.download(products, target="./data", via="http", concurrency=4)
```

See [examples/python/](examples/python/) for ranged reads, S3 keys, error handling,
raw-query escape hatches, and plugin backends.

## Setting up the platform

CDSE works with zero configuration. A "platform" is a deployment of EO services
(catalogue, download, keys, Keycloak); endpoints resolve **per field**, most specific
source wins:

```
Client kwargs  >  EOSDK_* env  >  ./eosdk.toml  >  user profile  >  discovery  >  built-in defaults
```

You rarely set every endpoint — point the SDK at a **platform root** and the rest are
discovered from `{platform}/.well-known/eo-services.json`. Inspect the result at any
time with `eo config show` (or `client.config.resolved()`), which prints every endpoint
with the source that provided it.

**Named profiles** (recommended) — create one from a root URL; discovery fills in the
rest. Profiles live in `~/.config/eosdk/config.toml`:

```bash
eo config init --name prod --platform https://platform.example.eu
eo config use prod                   # make it the default
eo --profile staging search ...      # or select per-invocation
eo config set profiles.staging.eodata_http https://canary.example.eu  # pin beats discovery
```

Equivalent hand-written config:

```toml
default_profile = "prod"

[profiles.prod]
platform = "https://platform.example.eu"           # everything discovered from the root

[profiles.staging]
platform = "https://staging.example.eu"
eodata_http = "https://canary.example.eu"          # pinned; rest discovered

[profiles.local]                                   # fully manual, no discovery
catalogue_stac  = "http://localhost:8081/stac"
catalogue_odata = "http://localhost:8081/odata"
eodata_http     = "http://localhost:8082"
s3_endpoint     = "http://localhost:9000"
s3_credentials  = "http://localhost:8083/api"
keycloak        = "http://localhost:8180"
keycloak_realm  = "eodata"
```

**Project-local `./eosdk.toml`** — the same `[profiles.*]` syntax in the working
directory beats the user profile (and loses to env vars); handy for pinning a repo to
one deployment.

**Environment variables** — good for CI and one-off overrides. Every endpoint has an
`EOSDK_*_URL` variable (full map in
[src/eosdk/config/defaults.py](src/eosdk/config/defaults.py)):

```bash
export EOSDK_PLATFORM=https://platform.example.eu   # platform root; endpoints discovered
export EOSDK_PROFILE=staging                        # select a profile
export EOSDK_EODATA_HTTP_URL=http://localhost:8082  # override one endpoint only
```

**In code** — `Client(...)` kwargs win over everything:
`Client(platform=...)`, `Client(profile="prod")`, `Client(eodata_http=...)`.

**Where the discovery document lives** — a dedicated subdomain (recommended, servable
from a static bucket/CDN), the main domain root, or any URL via `discovery_url` /
`EOSDK_DISCOVERY_URL`.

> CDSE's discovery document will be published at
> **`https://discover.dataspace.copernicus.eu`**. It is not live yet — until then the
> built-in defaults cover CDSE; once published you can bootstrap from it explicitly with
> `eo config init --platform https://discover.dataspace.copernicus.eu`.

## Installation

Not yet published to PyPI. Requires [uv](https://docs.astral.sh/uv/). From a clone,
either install the CLI globally:

```bash
uv tool install --editable .   # `--editable` picks up local code changes
```

or work inside the project virtualenv:

```bash
uv sync
uv run eo --help               # or: source .venv/bin/activate && eo --help
```

## Examples

[examples/](examples/) contains runnable, commented examples for both surfaces:
[examples/python/](examples/python/) covers search, downloads, ranged reads, auth and
S3 keys, configuration, error handling, raw queries, and plugin backends;
[examples/cli/](examples/cli/) are annotated `eo` walkthroughs.

## Local discovery endpoint (Docker)

[docker/discovery/](docker/discovery/) serves a sample discovery document at the
well-known path — useful for developing against discovery without a real platform:

```bash
docker build -t eosdk-discovery docker/discovery
docker run --rm -p 8080:80 eosdk-discovery
# then: Client(platform="http://localhost:8080")
```

Mount your own document over
`/usr/share/nginx/html/.well-known/eo-services.json` to replace the baked-in sample.

## Development

```bash
uv sync              # create venv + install all dependency groups
uv run pytest        # tests
uv run ruff check .  # lint
uv run mypy          # type check (strict)
```

Smoke tests hit real services and need credentials for the authenticated ones:

```bash
EOSDK_SMOKE=1 \
EOSDK_SMOKE_USERNAME=you@example.com \
EOSDK_SMOKE_PASSWORD=... \
uv run pytest tests/smoke/
```

Attention: some smoke tests create S3 credentials (with teardown); don't run them
frequently, as S3 credentials are not meant to be temporary.
