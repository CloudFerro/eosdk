"""Raw queries — the escape hatches.

What this shows
---------------
The high-level ``client.search()`` covers the common 95%: one backend-neutral
query translated per protocol. For the rest, the SDK deliberately leaves
three doors open:

* ``Product.raw`` — every model keeps the untouched backend payload, so
  platform-specific attributes are never lost in normalization
* ``ODataCatalogue.query_raw()`` — run an OData path+query string verbatim
  (``$filter``, ``$orderby``, ``$expand``, ... anything the server accepts)
* ``StacCatalogue.raw_search()`` — POST an arbitrary STAC ItemSearch body
  (CQL2 filters, ``intersects`` geometries, fields extension, ...)

The raw methods return plain ``dict`` JSON — you opt out of normalized
models, pagination helpers, and portability across backends. Prefer
``client.search()`` unless you need something it cannot express.

Prerequisites: none — anonymous catalogue search works against the defaults.
"""

from __future__ import annotations

from eosdk import Client
from eosdk.catalogue.odata import ODataCatalogue
from eosdk.catalogue.stac import StacCatalogue
from eosdk.transport import Transport


def product_raw_payload(client: Client) -> None:
    """The gentlest escape hatch: normalized models keep the raw item."""
    results = client.search(collection="sentinel-2-l2a", limit=1)
    product = next(iter(results), None)
    if product is None:
        return
    # `raw` is the backend item exactly as received (here: the STAC Item),
    # so anything the normalized Product doesn't surface is still there.
    properties = product.raw.get("properties", {})
    print(f"{product.name}")
    print(f"  platform-specific keys: {sorted(properties)[:8]} ...")


def odata_raw_query(client: Client) -> None:
    """Full OData power: hand-written $filter / $orderby / $expand."""
    # The escape hatches live on the backend classes (SPEC §7.3), which are
    # constructed directly; endpoints can be borrowed from a resolved config.
    base_url = client.config.endpoints.catalogue_odata
    assert base_url is not None

    with Transport(timeout=30.0) as transport:
        catalogue = ODataCatalogue(base_url, transport=transport)

        # Passed through verbatim — nothing is escaped or rewritten for you.
        document = catalogue.query_raw(
            "Products?"
            "$filter=contains(Name,'S1A') and ContentLength gt 1000000000"
            "&$orderby=ContentDate/Start desc"
            "&$top=3"
        )
        for entry in document.get("value", []):
            print(f"[odata raw] {entry.get('Name')}  {entry.get('ContentLength'):,} B")

        # Normalized access still works on the same instance. Note that
        # `collections()` is *not* one of the options here: the CSC/OData API
        # exposes no collection-enumeration endpoint, so the backend refuses it
        # with UnsupportedQueryFeature — discover collection ids over STAC.
        queryables = catalogue.queryables("SENTINEL-1")
        print(f"[odata] {len(queryables)} queryable attributes on SENTINEL-1")


def stac_raw_search(client: Client) -> None:
    """Arbitrary STAC ItemSearch bodies: geometries, CQL2, extensions."""
    base_url = client.config.endpoints.catalogue_stac
    assert base_url is not None

    with Transport(timeout=30.0) as transport:
        catalogue = StacCatalogue(base_url, transport=transport)

        # An `intersects` polygon — something the high-level bbox= can't express.
        document = catalogue.raw_search(
            {
                "collections": ["sentinel-2-l2a"],
                "datetime": "2026-06-01T00:00:00Z/2026-06-30T23:59:59Z",
                "intersects": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [22.5, 52.9],
                            [24.0, 52.9],
                            [23.3, 53.5],
                            [22.5, 52.9],
                        ]
                    ],
                },
                "limit": 3,
            }
        )
        for feature in document.get("features", []):
            print(f"[stac raw] {feature['id']}")


def main() -> None:
    with Client() as client:
        product_raw_payload(client)
        odata_raw_query(client)
        stac_raw_search(client)


if __name__ == "__main__":
    main()
