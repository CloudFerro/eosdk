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

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync              # create venv + install all dependency groups
uv run pytest        # tests
uv run ruff check .  # lint
uv run mypy          # type check (strict)
```
