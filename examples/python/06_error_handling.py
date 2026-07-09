"""Error handling.

What this shows
---------------
The exception taxonomy — everything the SDK raises derives from
``EosdkError``, so one ``except`` clause catches it all, while specific
subclasses let a pipeline react differently to each failure mode:

* ``ConfigError``            — missing/invalid configuration (carries a hint)
* ``AuthError``              — login failed, session expired
* ``EndpointUnreachable``    — network/DNS/TLS trouble reaching a service
* ``UnsupportedApiVersion``  — the platform speaks an API we don't
* ``UnsupportedQueryFeature``— a filter the chosen backend can't translate
* ``UnsupportedCapability``  — e.g. ranged reads over the zipper backend
* ``ProductNotFound``        — bad id / product removed
* ``QuotaExceeded``          — rate limited (carries ``retry_after`` seconds)
* ``DownloadError``          — transfer failed after retries

Prerequisites: none (this example provokes errors on purpose).
"""

from __future__ import annotations

import time

from eosdk import Client
from eosdk.exceptions import (
    ConfigError,
    EosdkError,
    ProductNotFound,
    QuotaExceeded,
    UnsupportedQueryFeature,
)


def main() -> None:
    with Client() as client:
        # -- unknown product id --------------------------------------------------
        try:
            client.get("this-product-does-not-exist")
        except ProductNotFound as exc:
            print(f"not found: {exc}")

        # -- query features are validated per backend, before any HTTP -------------
        try:
            client.search(collection="SENTINEL-2", filters={"name": "~fuzzy~"}, limit=1)
        except UnsupportedQueryFeature as exc:
            print(f"bad filter: {exc}")

        # -- config errors carry an actionable hint --------------------------------
        try:
            Client(profile="no-such-profile")
        except ConfigError as exc:
            print(f"config: {exc}")
            if exc.hint:
                print(f"  hint: {exc.hint}")

        # -- a robust download loop for pipelines -----------------------------------
        products = client.search(collection="sentinel-2-l2a", limit=1)
        for product in products:
            try:
                client.download(product, target="./data")
            except QuotaExceeded as exc:
                # Back off exactly as the platform asked, then retry.
                wait = exc.retry_after or 60.0
                print(f"rate limited; sleeping {wait:.0f}s")
                time.sleep(wait)
                client.download(product, target="./data")
            except EosdkError as exc:
                # Catch-all: log and move on to the next product instead of dying.
                print(f"skipping {product.id}: {exc}")


if __name__ == "__main__":
    main()
