"""Catalogue search basics.

What this shows
---------------
* constructing a ``Client`` (construction is offline — nothing is fetched)
* searching with collection / bbox / time interval / attribute filters
* ``SearchResult`` is lazy and re-iterable: pages are fetched on demand and
  cached, so iterating twice performs no extra requests
* discovering the vocabulary: available collections (with product types via
  ``Collection.raw`` summaries) and filterable attributes via
  ``client.queryables()``
* switching the catalogue protocol between STAC (default) and OData

Prerequisites: none — anonymous catalogue search works against the built-in
defaults (Copernicus Data Space Ecosystem).
"""

from __future__ import annotations

from eosdk import Client


def main() -> None:
    # Uses the default profile / built-in endpoints. See 05_configuration.py
    # for profiles, env vars, and explicit endpoint overrides.
    with Client() as client:
        # -- a bounded search --------------------------------------------------
        results = client.search(
            collection="sentinel-2-l2a",
            bbox=(22.5, 52.9, 24.0, 53.5),  # minx, miny, maxx, maxy (WGS84)
            datetime="2026-06-01/2026-06-30",  # ISO interval; open ends with ".."
            filters={"cloudCover": "<20"},  # bare value = eq; <, <=, >, >=, != work too
            sort="-datetime",  # '+field' ascending, '-field' descending
            limit=25,
        )

        # Total match count as reported by the backend (may trigger page 1).
        print(f"matched: {results.matched}")

        # Iterating streams page by page; nothing was fetched until now.
        for product in results:
            date = product.datetime.isoformat() if product.datetime else "-"
            size = f"{product.size:,} B" if product.size else "?"
            cloud = f"{product.cloud_cover:.1f}%" if product.cloud_cover is not None else "-"
            print(f"{product.name}  {date}  {size}  cloud {cloud}")

        # Re-iteration is free — pages were cached above.
        first = next(iter(results), None)
        if first is not None:
            print(f"\nfirst product id: {first.id}")

            # Fetch one product by id (same normalized model from any backend).
            same = client.get(first.id)
            print(f"lookup by id -> {same.name}")

        # -- page-wise access, e.g. for batch processing ------------------------
        for page_number, page in enumerate(results.pages(), start=1):
            print(f"page {page_number}: {len(page)} products")

        # -- discovering the vocabulary: collections and queryables ---------------
        print("\ncollections:")
        for coll in client.collections()[:10]:
            print(f"  {coll.id}: {coll.title or '-'}")
            # `raw` keeps the full backend document — e.g. STAC summaries list
            # the product types a collection contains:
            types = coll.raw.get("summaries", {}).get("product:type")
            if types:
                print(f"    product types: {types}")

        # Which attributes can be filtered on, per backend vocabulary?
        # (STAC names like "eo:cloud_cover"; the aliases cloudCover/productType
        # from the filter table keep working too.)
        print("\nqueryables for sentinel-2-l2a (stac):")
        for q in client.queryables("sentinel-2-l2a"):
            print(f"  {q.name}: {q.type or '?'}")

        print("\nqueryables for SENTINEL-2 (odata):")
        for q in client.queryables("SENTINEL-2", protocol="odata"):
            print(f"  {q.name}: {q.type or '?'}")

        # -- same query over the OData backend ----------------------------------
        # `filters` are backend-neutral, but collection ids are NOT translated:
        # each backend has its own vocabulary. STAC uses product-level ids
        # ("sentinel-2-l2a"); OData uses mission-level names ("SENTINEL-2",
        # the whole mission — narrow by productType to match a STAC collection).
        odata_results = client.search(
            collection="SENTINEL-2",
            datetime="2026-06-01/2026-06-05",
            filters={"cloudCover": "<20", "productType": "S2MSI2A"},  # L2A only
            limit=5,
            protocol="odata",
        )
        for product in odata_results:
            print(f"[odata] {product.name}")


if __name__ == "__main__":
    main()
