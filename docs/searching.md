# Searching the catalogue

One backend-neutral query, translated per protocol. `client.search()` and
`eo search` take the same inputs and return the same normalized models over
either STAC (default) or OData — public catalogues need no login.

```python
from eosdk import Client

client = Client()

results = client.search(
    collection="sentinel-2-l2a",
    bbox=(22.5, 52.9, 24.0, 53.5),      # minx, miny, maxx, maxy (WGS84)
    datetime="2026-06-01/2026-06-30",   # ISO interval; open-ended with ".."
    filters={"cloudCover": "<20"},      # bare value = eq; <, <=, >, >=, != work too
    sort="-datetime",                   # '+field' ascending, '-field' descending
    limit=25,
    protocol="stac",                    # or "odata"
)
```

```bash
eo search --collection sentinel-2-l2a \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "cloudCover=<20" \
          --sort -datetime --limit 25 --protocol stac
```

## Query parameters

Every parameter is optional, but a search with **none** of `collection`,
`bbox`, `datetime`, or `filters` is refused (`ConfigError`) on both surfaces —
narrow it before it fans out over the whole catalogue.

| Parameter | Library | CLI | Meaning |
|---|---|---|---|
| Collection | `collection=` | `--collection` / `-c` | Backend-native collection id (see below) |
| Bounding box | `bbox=(minx,miny,maxx,maxy)` | `--bbox minx,miny,maxx,maxy` | WGS84; `min < max`, latitudes in [-90, 90] |
| Time interval | `datetime="from/to"` | `--from` / `--to` | ISO interval; `..` for an open end |
| Attribute filters | `filters={...}` | `--filter` / `-f` (repeatable) | Attribute comparisons (below) |
| Sort | `sort="-field"` | `--sort` | `+field` ascending, `-field` descending |
| Limit | `limit=N` | `--limit` / `-n` | Max products to return (must be positive) |
| Protocol | `protocol="stac"` | `--protocol` | `stac` (default) or `odata` |

## Filters

Filter values carry an operator prefix; a bare value means equality:

| Prefix | Operator | Example |
|---|---|---|
| (none) | equals | `productType=S2MSI2A` |
| `!=` | not equal | `productType=!=GRD` |
| `<` `<=` | less than / or equal | `cloudCover=<20` |
| `>` `>=` | greater than / or equal | `cloudCover=>=5` |

Attribute names are backend-native, but a few common ones are aliased so the
same key works on both protocols: `cloudCover`/`cloud_cover` →
`eo:cloud_cover` (STAC) and `productType` → `product:type`. Ask a collection
what it accepts with `client.queryables(...)` / `eo queryables` — STAC names
like `eo:cloud_cover` and the aliases both work.

An empty value, or an operator the backend can't express, raises
`UnsupportedQueryFeature`.

## Collection ids differ per backend

`filters`, `bbox`, `datetime`, and `sort` are backend-neutral, but collection
ids are **not** translated — each protocol has its own vocabulary. STAC uses
product-level ids (`sentinel-2-l2a`); OData uses mission-level names
(`SENTINEL-2`, the whole mission — narrow with `productType` to match a STAC
collection):

```python
client.search(collection="sentinel-2-l2a", ..., protocol="stac")
client.search(collection="SENTINEL-2",
              filters={"productType": "S2MSI2A"}, ..., protocol="odata")
```

Naming a collection the catalogue doesn't know raises `CollectionNotFound`,
which carries `suggestions`. List valid ids with `client.collections()` /
`eo collections`.

## Working with results

`search()` returns a `SearchResult`: a lazy, re-iterable, thread-safe sequence
of `Product`. Nothing is fetched until you iterate; pages are fetched on demand
and **cached**, so iterating twice makes no extra requests.

```python
print(results.matched)          # total count reported by the backend (may fetch page 1)

for product in results:         # streams page by page
    print(product.name, product.datetime, product.cloud_cover)

for page in results.pages():    # page-wise, e.g. for batch work
    process(page)

first = next(iter(results))     # free — pages were cached above
same = client.get(first.id)     # fetch one product by id (same model, any backend)
```

`Product` normalizes the fields every backend shares (`id`, `name`,
`collection`, `size`, `datetime`, `cloud_cover`, `geometry`, `checksum`,
`s3_path`); `product.raw` keeps the untouched backend item, so
platform-specific attributes are never lost.

Search results pipe straight into download. In the CLI, `--format json`
emits one product per line (JSON Lines); `--format id` / `--format s3` print
one uuid or S3 path per line:

```bash
eo search ... --format json | eo download - --via http -o ./data
eo download $(eo search ... --format id) -o ./data
```

## Discovering the vocabulary

```python
client.collections()                    # collection ids the catalogue offers
client.queryables("sentinel-2-l2a")     # filterable attribute names for a collection
```

```bash
eo collections [--protocol stac|odata] [--json]
eo queryables <collection> [--protocol stac|odata] [--json]
```

`Collection.raw` keeps the full backend document — e.g. STAC summaries list
the product types a collection contains under `summaries.product:type`.

## Raw queries — the escape hatch

`client.search()` covers the common case. When you need something the neutral
query can't express — an `intersects` polygon, CQL2, `$expand`, arbitrary
`$orderby` — drop to the backend classes directly (SPEC §7.3). These return
plain `dict` JSON: you opt out of normalized models and pagination helpers.

```python
from eosdk.catalogue.odata import ODataCatalogue
from eosdk.catalogue.stac import StacCatalogue
from eosdk.transport import Transport

with Transport(timeout=30.0) as transport:
    odata = ODataCatalogue(client.config.endpoints.catalogue_odata, transport=transport)
    doc = odata.query_raw(
        "Products?$filter=contains(Name,'S1A')&$orderby=ContentDate/Start desc&$top=3"
    )

    stac = StacCatalogue(client.config.endpoints.catalogue_stac, transport=transport)
    doc = stac.raw_search({
        "collections": ["sentinel-2-l2a"],
        "intersects": {"type": "Polygon", "coordinates": [[[22.5, 52.9], [24.0, 52.9],
                                                            [23.3, 53.5], [22.5, 52.9]]]},
        "limit": 3,
    })
```

These two methods are the only deliberate library-only surface; everything
else on `Client` has a matching `eo` verb.
