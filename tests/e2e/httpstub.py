"""A real socket in front of :class:`~tests.e2e.platform.FakePlatform`.

respx patches httpx inside *this* process, so it cannot serve a subprocess. The
router in ``platform.py`` was written transport-agnostic for exactly this
reason: ``FakePlatform.handle(method, url, headers, body)`` is pure, and this
module is the second adapter for it — an ordinary ``http.server`` on an
ephemeral port, with every service behind one socket
(``/auth``, ``/stac``, ``/odata``, ``/eodata``, ``/keys``, and the well-known
discovery path at the root).

S3 needs a socket too; ``moto.server.ThreadedMotoServer`` provides it and its
endpoint is injected into the discovery document like any other.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from tests.e2e.platform import FakePlatform, urls_for_port

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from tests.e2e.platform import FakeCollection, FakeProduct

DEFAULT_CHUNK = 1 << 16


class _StubServer(ThreadingHTTPServer):
    """Carries the platform and the pacing knobs the handler reads."""

    daemon_threads = True
    allow_reuse_address = True

    platform: FakePlatform
    chunk_delay: float = 0.0
    chunk_size: int = DEFAULT_CHUNK

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client hanging up mid-response is the scenario, not a server fault.

        Ctrl+C during a download and a closed pipe both surface here as
        ConnectionResetError/BrokenPipeError; anything else is re-raised so a
        genuine stub bug still shows up.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _StubServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = f"http://{self.headers.get('Host', 'localhost')}{self.path}"
        response = self.server.platform.handle(
            self.command, url, headers=dict(self.headers), body=body
        )

        truncated = False
        payload = response.content
        if response.stream_factory is not None:
            collected = bytearray()
            try:
                for chunk in response.stream_factory():
                    collected.extend(chunk)
            except Exception:  # the stream is *meant* to die halfway
                truncated = True
            payload = bytes(collected)

        headers = {
            key: value for key, value in response.headers.items() if key.lower() != "content-length"
        }
        declared = response.headers.get("Content-Length")
        self.send_response(response.status)
        for key, value in headers.items():
            self.send_header(key, value)
        # A truncated body keeps the *declared* length and drops the connection,
        # which is what a real interrupted transfer looks like on the wire.
        self.send_header("Content-Length", declared if truncated else str(len(payload)))
        if truncated:
            self.send_header("Connection", "close")
        self.end_headers()
        self._write(payload)
        if truncated:
            self.close_connection = True

    def _write(self, payload: bytes) -> None:
        delay = self.server.chunk_delay
        size = self.server.chunk_size
        if delay <= 0 or len(payload) <= size:
            self.wfile.write(payload)
            return
        for start in range(0, len(payload), size):
            self.wfile.write(payload[start : start + size])
            self.wfile.flush()
            time.sleep(delay)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch
    do_HEAD = _dispatch
    do_PATCH = _dispatch


@contextmanager
def serve(
    products: Sequence[FakeProduct] | None = None,
    collections: Sequence[FakeCollection] | None = None,
    *,
    s3_endpoint: str | None = None,
    s3_region: str = "us-east-1",
    page_size: int = 2,
) -> Iterator[tuple[FakePlatform, str, _StubServer]]:
    """Run a FakePlatform on a real port; yields (platform, base url, server)."""
    server = _StubServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    urls = urls_for_port(port)
    if s3_endpoint is not None:
        urls = replace(urls, s3_endpoint=s3_endpoint, s3_region=s3_region)
    server.platform = FakePlatform(products, collections, urls=urls, page_size=page_size)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.platform, f"http://127.0.0.1:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def s3_server() -> Iterator[str]:
    """A real S3 on a real port, for subprocesses that cannot see moto's patches."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    _, port = server.get_host_and_port()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def seed_remote_s3(platform: FakePlatform) -> None:
    """Put every product's tree into the standalone S3 server."""
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=platform.urls.s3_endpoint,
        region_name=platform.urls.s3_region,
        aws_access_key_id="seed",
        aws_secret_access_key="seed",
    )
    for bucket in {product.bucket for product in platform.products if product.in_s3}:
        client.create_bucket(Bucket=bucket)
    for product in platform.products:
        if not product.in_s3:
            continue
        for logical, content in product.tree.items():
            client.put_object(
                Bucket=product.bucket, Key=f"{product.s3_prefix}/{logical}", Body=content
            )
