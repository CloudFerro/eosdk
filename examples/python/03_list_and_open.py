"""Inspecting a product's file tree and reading single files.

What this shows
---------------
* ``client.list()`` — the file/directory tree inside a product (SAFE archive)
* ``client.open()`` — ranged reads of one file over S3, without downloading
  the whole product (only the ``s3`` backend has this capability)
* capability gating: asking a backend for something it cannot do raises
  ``UnsupportedCapability`` *before* any network traffic

Backend capabilities:

    backend   download   list   open (ranged reads)   resume
    http         yes      yes          no             no (restarts)
    s3           yes      yes          yes            yes

Prerequisites: a logged-in session (``eo auth login``).
"""

from __future__ import annotations

from eosdk import Client
from eosdk.exceptions import UnsupportedCapability


def main() -> None:
    with Client() as client:
        results = client.search(
            collection="sentinel-2-l2a",
            bbox=(22.5, 52.9, 24.0, 53.5),
            datetime="2026-06-01/2026-06-30",
            limit=1,
        )
        product = next(iter(results), None)
        if product is None:
            print("no products matched — widen the search")
            return

        # -- list the internal tree ----------------------------------------------
        # `path` is logical (relative to the product root) and backend-agnostic.
        print(f"contents of {product.name}:")
        for node in client.list(product, via="http"):
            kind = "dir " if node.is_dir else "file"
            size = f"{node.size:,} B" if node.size is not None else ""
            print(f"  {kind}  {node.path}  {size}")

        # Recursive listing to find one specific band file.
        nodes = client.list(product, via="s3", recursive=True)
        band = next((n for n in nodes if n.path.endswith("B04_10m.jp2")), None)
        if band is None:
            print("no 10m red band in this product")
            return

        # -- ranged read over S3: grab just the file header ------------------------
        # No full-product transfer; S3 keys are minted/cached automatically.
        with client.open(product, path=band.path, via="s3") as fh:
            header = fh.read(1024)
        print(f"\n{band.path}: first {len(header)} bytes -> {header[:16].hex()}...")

        # -- capability gating ------------------------------------------------------
        # The http backend cannot do ranged reads; the SDK refuses before touching the network.
        try:
            client.open(product, path=band.path, via="http")
        except UnsupportedCapability as exc:
            print(f"\nas expected: {exc}")


if __name__ == "__main__":
    main()
