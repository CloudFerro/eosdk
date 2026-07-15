"""Bulk-download benchmark matrix (plan step 4.3). Live network; run manually.

Usage:
    EOSDK_SMOKE_USERNAME=... EOSDK_SMOKE_PASSWORD=... \
        uv run python benchmarks/bench_download.py [--collection sentinel-2-l2a]

Measures wall time and throughput for {http, s3} x {1, 2, 4, 8, 10} concurrency
over a small batch of products. Record results in benchmarks/RESULTS.md and
tune part_size / max_ranges_per_file / default concurrency from them.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

from eosdk import Client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="sentinel-2-l2a")
    parser.add_argument("--products", type=int, default=2)
    parser.add_argument("--concurrency", type=int, nargs="*", default=[1, 2, 4, 8, 10])
    parser.add_argument("--via", nargs="*", default=["http", "s3"])
    args = parser.parse_args()

    username = os.environ.get("EOSDK_SMOKE_USERNAME")
    password = os.environ.get("EOSDK_SMOKE_PASSWORD")
    if not username or not password:
        print("set EOSDK_SMOKE_USERNAME / EOSDK_SMOKE_PASSWORD", file=sys.stderr)
        return 2

    with Client() as client:
        client.auth.login(username, password)
        products = list(
            client.search(collection=args.collection, limit=args.products, sort="-datetime")
        )
        total_bytes = sum(p.size or 0 for p in products)
        print(f"benchmarking {len(products)} products, {total_bytes / 1e6:.0f} MB total")

        for via in args.via:
            for concurrency in args.concurrency:
                target = tempfile.mkdtemp(prefix="eosdk-bench-")
                start = time.perf_counter()
                try:
                    reports = client.download(
                        products, target=target, via=via, concurrency=concurrency
                    )
                    elapsed = time.perf_counter() - start
                    downloaded = sum(r.bytes for r in reports)
                    print(
                        f"via={via:<7} c={concurrency:<2} "
                        f"{elapsed:8.1f}s {downloaded / elapsed / 1e6:8.1f} MB/s"
                    )
                except Exception as exc:
                    print(f"via={via:<7} c={concurrency:<2} FAILED: {exc}")
                finally:
                    shutil.rmtree(target, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
