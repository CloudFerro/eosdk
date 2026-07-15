# Quickstart

## Install

```bash
pip install eosdk        # or: uv add eosdk
```

## CLI in five lines

```bash
eo auth login                        # device flow -> cached session
eo search --collection sentinel-2-l2a \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "cloudCover=<20" --format json \
  | eo download - --via http -o ./data
```

`--format json` emits one product per line (JSON Lines), so search results
pipe straight into `eo download -`, `jq`, or `head`.

## Library

```python
from eosdk import Client

client = Client(profile="prod")          # construction is offline

products = client.search(
    collection="sentinel-2-l2a",
    bbox=(22.5, 52.9, 24.0, 53.5),
    datetime="2026-06-01/2026-06-30",
    filters={"cloudCover": "<20"},
    protocol="stac",                     # or "odata"
    limit=50,
)

client.download(products, target="./data", via="http",
                concurrency=4, resume=True, checksum=True)

# SearchResult is re-iterable — pages are cached, not re-queried:
product = next(iter(products))

# What files are inside the product?
nodes = client.list(product, via="http", recursive=True)
band = next(n for n in nodes if n.path.endswith("B04_10m.jp2"))

# Ranged read over S3 — no full-product transfer (S3 keys auto-managed):
with client.open(product, path=band.path, via="s3") as f:
    header = f.read(1024)
```

## Backend capabilities

| Backend | `download` | `list` | `open` (ranged reads) | resume |
|---|---|---|---|---|
| `http` | ✓ | ✓ | — | — (restarts) |
| `s3` | ✓ | ✓ | ✓ | ✓ |

Requesting a capability a backend lacks raises `UnsupportedCapability` before
any network traffic.
