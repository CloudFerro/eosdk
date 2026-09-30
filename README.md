# eosdk

Python SDK and CLI (`eo`) for Earth Observation services: unified catalogue search
(STAC / OData), EOData downloads (HTTP / S3), and fully managed authentication
(Keycloak JWT, S3 key lifecycle) behind one coherent interface — *search, then download*.

## Highlights

- **Three steps to start your EO adventure: register, install, explore.** Sign up for a
  platform account, install the `eo` CLI, and you're searching the catalogue.
- **Every EO service API in one place.** Catalogue, downloads, S3 keys, and auth sit
  behind a single interface — you never juggle individual service APIs. Need more?
  Configuration resolves per field, from a single platform root down to individually
  pinned endpoints.
- **Two modes, one eosdk.** No API knowledge needed: human-readable calls — `eo auth
  login`, `eo search`, `eo download` — cover the whole workflow, and the typed Python
  library mirrors them one-to-one (`client.auth.login()`, `client.search()`,
  `client.download()`) on the same session, config, and endpoints. On top of that, raw
  queries are available for the most demanding users.
- **Any EO platform from one URL.** Point the SDK at a platform's discovery root —
  for example CDSE's `https://discover.dataspace.copernicus.eu` — and every endpoint
  is discovered from `/.well-known/eo-services.json`; nothing is hard-coded.
- **Search, then download, as one pipeline.** `eo search --format json | eo download -`
  is the whole workflow; JSON Lines interoperate cleanly with `jq` and the Unix toolbox.
- **Auth you never think about.** Log in once; the session is cached per profile and
  reused by every later CLI *and* library call. S3 keys are minted automatically when
  needed.
- **Backend details stay out of your way.** HTTP vs. S3, STAC vs. OData — pick one flag;
  the SDK owns the protocol, retries, concurrency, checksums, resume, and credentials.
- **Typed and strict.** Pydantic models, `py.typed`, and a strict-mypy codebase.

## Quickstart (the `eo` CLI)

Examples use the Copernicus Data Space Ecosystem (CDSE) — one EO platform among
those eosdk can talk to; any platform that publishes a discovery document works the
same way, just with its own root URL and account. Search is anonymous; **downloads
require a login**.

**1 — Register** for a free account at **https://dataspace.copernicus.eu/** and confirm
your email.

**2 — Install** the `eo` CLI from PyPI (Python 3.10+; see
[Installation](#installation) for pip, the library, and installing from source):

```bash
uv tool install eosdk   # or: pipx install eosdk — makes `eo` available everywhere
eo --help
```

**3 — Connect to the platform.** One command discovers every endpoint from the
platform root and saves them as your default profile:

```bash
eo config init --platform https://discover.dataspace.copernicus.eu
eo config show   # every endpoint, with the source that provided it
```

**4 — Log in** with username/password (the password is prompted, so it never lands in
your shell history):

```bash
eo auth login --username you@example.com
eo auth status
```

> The bare `eo auth login` (OAuth device flow) is the default when a realm supports it,
> but CDSE's public client currently has that grant disabled — use `--username` on CDSE.
> For CI, pipe the password in with `--password-stdin`.

**5 — Search.** A human-readable table by default:

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
> (`--protocol odata`). List a catalogue's collection ids with `eo collections`, and a
> collection's filterable attributes with `eo queryables <collection>`.

**6 — Download.** Pipe `search --format json` (one product per line) straight into
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
themselves. Add it to a project with `uv add eosdk` (or `pip install eosdk`).

```python
import getpass
from eosdk import Client

client = Client()  # your default profile (eo config init); or Client(platform=...) / Client(profile=...)

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

See [examples/python/](https://github.com/CloudFerro/eosdk/tree/master/examples/python)
for ranged reads, S3 keys, error handling, raw-query escape hatches, and plugin
backends.

## Setting up the platform

A "platform" is a deployment of EO services (catalogue, download, keys, Keycloak).
The SDK ships no built-in endpoints — you tell it which platform to talk to, once,
and endpoints resolve **per field**, most specific source wins:

```
Client kwargs  >  EOSDK_* env  >  ./eosdk.toml  >  user profile  >  discovery
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
[src/eosdk/config/defaults.py](https://github.com/CloudFerro/eosdk/blob/master/src/eosdk/config/defaults.py)):

```bash
export EOSDK_PLATFORM=https://platform.example.eu   # platform root; endpoints discovered
export EOSDK_PROFILE=staging                        # select a profile
export EOSDK_EODATA_HTTP_URL=http://localhost:8082  # override one endpoint only
```

**In code** — `Client(...)` kwargs win over everything:
`Client(platform=...)`, `Client(profile="prod")`,
`Client(endpoints={"eodata_http": ...})`.

**Where the discovery document lives** — a dedicated subdomain (recommended, servable
from a static bucket/CDN), the main domain root, or any URL via `discovery_url` /
`EOSDK_DISCOVERY_URL`.

> CDSE's discovery document is live at
> **`https://discover.dataspace.copernicus.eu`** —
> `eo config init --platform https://discover.dataspace.copernicus.eu` bootstraps a
> ready-to-use profile from it.

## Installation

eosdk is published on PyPI as [`eosdk`](https://pypi.org/project/eosdk/) and requires
Python 3.10 or newer. One package ships both the `eo` CLI and the Python library.

**The `eo` CLI** — install it as a standalone tool in its own isolated environment,
available on your `PATH` everywhere:

```bash
uv tool install eosdk   # or: pipx install eosdk
uv tool upgrade eosdk   # later, to update (pipx: pipx upgrade eosdk)
```

To try it without installing anything, `uvx --from eosdk eo --help` runs it from a
throwaway environment.

**The Python library** — add it to your project's dependencies; the `eo` command is
installed into that environment too:

```bash
uv add eosdk            # or: pip install eosdk
```

Pre-1.0, a minor release may contain breaking changes (always called out in the
[changelog](https://github.com/CloudFerro/eosdk/blob/master/CHANGELOG.md)), so pin the
minor series in projects: `uv add "eosdk~=0.5.0"` (that is, `>=0.5.0, <0.6`).

**From source** — for development, or to run unreleased changes. From a clone, either
install the CLI globally:

```bash
git clone https://github.com/CloudFerro/eosdk && cd eosdk
uv tool install --editable .   # `--editable` picks up local code changes
```

or work inside the project virtualenv:

```bash
uv sync
uv run eo --help               # or: source .venv/bin/activate && eo --help
```

## Examples

[examples/](https://github.com/CloudFerro/eosdk/tree/master/examples) contains
runnable, commented examples for both surfaces:
[examples/python/](https://github.com/CloudFerro/eosdk/tree/master/examples/python)
covers search, downloads, ranged reads, auth and S3 keys, configuration, error
handling, raw queries, and plugin backends;
[examples/cli/](https://github.com/CloudFerro/eosdk/tree/master/examples/cli) are
annotated `eo` walkthroughs.

## Local discovery endpoint (Docker)

[docker/discovery/](https://github.com/CloudFerro/eosdk/tree/master/docker/discovery)
serves a sample discovery document at the well-known path — useful for developing
against discovery without a real platform (run from a clone):

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

## Documentation

- **[docs/](https://github.com/CloudFerro/eosdk/tree/master/docs)** — user guide
  built with [MkDocs](https://www.mkdocs.org/):
  [quickstart](https://github.com/CloudFerro/eosdk/blob/master/docs/quickstart.md),
  [configuration](https://github.com/CloudFerro/eosdk/blob/master/docs/configuration.md),
  [CLI reference](https://github.com/CloudFerro/eosdk/blob/master/docs/cli.md),
  [errors](https://github.com/CloudFerro/eosdk/blob/master/docs/errors.md), and the
  [Python API reference](https://github.com/CloudFerro/eosdk/blob/master/docs/reference.md).
  Build and preview locally with `uv run mkdocs serve`.
- **[CHANGELOG.md](https://github.com/CloudFerro/eosdk/blob/master/CHANGELOG.md)** —
  notable changes per release, following
  [Keep a Changelog](https://keepachangelog.com/); the project uses
  [Semantic Versioning](https://semver.org/).

## Acknowledgements

eosdk stands on a small set of excellent open-source libraries —
[httpx](https://www.python-httpx.org/) for HTTP,
[boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html) for S3,
[pydantic](https://docs.pydantic.dev/) for typed models,
[typer](https://typer.tiangolo.com/) and [rich](https://rich.readthedocs.io/) for the
CLI, and [tenacity](https://tenacity.readthedocs.io/) for retries — and interoperates
with the wider [STAC](https://stacspec.org/) and OData ecosystems. Thank you to their
maintainers.

## Authors

eosdk is developed and maintained by
**[CloudFerro](https://cloudferro.com/)**.

## License

eosdk is licensed under the **Apache License, Version 2.0**. See the
[LICENSE](https://github.com/CloudFerro/eosdk/blob/master/LICENSE) file for the full text.
