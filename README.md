# eosdk

Python SDK and CLI (`eo`) for Earth Observation services: unified catalogue search
(STAC / OData), EOData downloads (Zipper / Exos S3), and fully managed authentication
(Keycloak JWT, S3 key lifecycle) behind one coherent interface — *search, then download*.

> Status: pre-release, under active development. See [SPEC.md](SPEC.md) for the full
> specification and roadmap.

## Quickstart (library)

```python
from eosdk import Client

client = Client(profile="prod")

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

```bash
eo auth login
eo search --collection SENTINEL-1 --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 --json \
  | eo download - --via zipper -o ./data
```

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
