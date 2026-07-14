"""Downloading products.

What this shows
---------------
* downloading a whole search result (or a single ``Product``)
* choosing the backend with ``via=``: ``http`` (HTTP zip) or ``s3`` (S3)
* concurrency, resume, and checksum verification switches
* a progress callback receiving ``ProgressEvent``s
* reading the returned ``DownloadReport``s

Prerequisites: a logged-in session (``eo auth login``) — downloads are
authenticated. The ``s3`` backend additionally mints S3 keys on first use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eosdk import Client

if TYPE_CHECKING:
    from eosdk.eodata import ProgressEvent


def on_progress(event: ProgressEvent) -> None:
    """Called from worker threads for every start/chunk/retry/done/error."""
    if event.kind == "chunk" and event.bytes_total:
        pct = 100 * event.bytes_done / event.bytes_total
        print(f"\r{event.product_id}: {pct:5.1f}%", end="", flush=True)
    elif event.kind == "retry":
        print(f"\n{event.product_id}: transfer hiccup, retrying...")
    elif event.kind == "done":
        print(f"\n{event.product_id}: done")


def main() -> None:
    with Client() as client:
        products = client.search(
            collection="sentinel-2-l2a",
            bbox=(22.5, 52.9, 24.0, 53.5),
            datetime="2026-06-01/2026-06-30",
            filters={"cloudCover": "<10"},
            limit=2,  # keep the example small
        )

        # -- bulk download over the HTTP backend ---------------------------------
        # The http backend streams a zip over HTTP; it cannot resume (restarts on retry).
        reports = client.download(
            products,
            target="./data",
            via="http",
            concurrency=4,  # parallel product transfers
            checksum=True,  # verify against the catalogue checksum
            progress=on_progress,
        )

        for report in reports:
            verified = {
                True: "checksum ok",
                False: "CHECKSUM MISMATCH",
                None: "no catalogue checksum",
            }[report.checksum_verified]
            print(f"{report.path}  {report.bytes:,} B  {verified}  ({report.attempts} attempt(s))")

        # -- single product over S3 ----------------------------------------------
        # The s3 backend supports resume: interrupted transfers continue from the last byte.
        # S3 credentials are minted and cached automatically on first use.
        first = next(iter(products), None)
        if first is not None:
            client.download(first, target="./data", via="s3", resume=True)


if __name__ == "__main__":
    main()
