# eosdk

Python SDK and CLI (`eo`) for Earth Observation services: unified catalogue search
(STAC / OData), EOData downloads (Zipper / Exos S3), and fully managed authentication
(Keycloak JWT, S3 key lifecycle) behind one coherent interface — *search, then download*.

> Status: pre-release, under active development. See [SPEC.md](SPEC.md) for the full
> specification and roadmap.

## Installation

Not yet published to PyPI. Requires [uv](https://docs.astral.sh/uv/).

For development, install into the project virtualenv and run the CLI through `uv`:

```bash
git clone <repo-url> && cd eosdk
uv sync
uv run eo --help
```

Alternatively, activate the virtualenv to call `eo` directly for the session:

```bash
source .venv/bin/activate
eo --help
```

To make `eo` available everywhere, install it as a uv tool (`--editable` picks up
local code changes without reinstalling):

```bash
uv tool install --editable .
eo --help
```

## Quickstart (library)

The library reuses the session cached by `eo auth login` (see the CLI quickstart
below), so most programs never log in themselves. To authenticate from code
instead:

```python
import getpass

from eosdk import Client

client = Client(profile="prod")

if not client.auth.status().logged_in:
    client.auth.login("you@example.com", getpass.getpass())  # cached under ~/.config/eosdk/

products = client.search(
    collection="SENTINEL-2",
    bbox=(22.5, 52.9, 24.0, 53.5),
    datetime="2026-06-01/2026-06-30",
    filters={"cloudCover": "<20"},
    limit=50,
)

client.download(products, target="./data", via="zipper", concurrency=4)
```

## Quickstart (CLI)

Log in once — the session is cached per profile under `~/.config/eosdk/` and
reused by every later `eo` or library call:

```bash
eo auth login --username you@example.com   # prompts for the password
eo search --collection SENTINEL-1 --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 --json \
  | eo download - --via zipper -o ./data
```

> Note: bare `eo auth login` uses the OAuth device flow, but the default CDSE
> deployment currently has that grant disabled for its public client
> (Keycloak returns HTTP 400 `unauthorized_client`) — use `--username` (or
> `--password-stdin` in CI) against CDSE. For services/CI, see
> [examples/cli/02_auth.sh](examples/cli/02_auth.sh).

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
Attention: smoke tests create credentials in some tests; tear down step is included, but it should not be run frequently as S3 credentials are not meant to be temporary
