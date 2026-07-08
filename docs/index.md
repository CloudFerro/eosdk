# eosdk

One Python SDK — and one CLI, `eo` — for Earth Observation platform services:

- **Search** the catalogue over STAC or OData with one query model.
- **Download** products via Zipper (HTTP) or Exos (S3) behind one interface,
  with retries, checksums, progress, and resume where the backend supports it.
- **Auth handled invisibly**: Keycloak JWT lifecycle and S3 key management are
  owned by the SDK; your code never touches raw credentials.

```python
from eosdk import Client

client = Client()  # built-in CDSE defaults; profiles/env override everything

products = client.search(
    collection="sentinel-2-l2a",
    bbox=(22.5, 52.9, 24.0, 53.5),
    datetime="2026-06-01/2026-06-30",
    filters={"cloudCover": "<20"},
    limit=50,
)

client.auth.login("user", "pass")  # or: eo auth login (device flow)
client.download(products, target="./data", via="zipper", concurrency=4)
```

Searching public catalogues needs no login at all.

## Where things live

| Concern | Module | CLI |
|---|---|---|
| Search | `eosdk.catalogue` (STAC + OData) | `eo search` |
| Download / list / open | `eosdk.eodata` (Zipper + Exos) | `eo download` |
| Auth & S3 keys | `eosdk.auth` | `eo auth`, `eo keys` |
| Configuration | `eosdk.config` | `eo config` |
| Service discovery | `eosdk.discovery` | `eo discover` |
| Health checks | `eosdk.doctor` | `eo doctor` |
