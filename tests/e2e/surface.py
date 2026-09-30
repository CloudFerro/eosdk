"""One journey, two surfaces.

A :class:`Surface` is the smallest adapter that lets the same journey run
against ``Client`` and against ``eo``, returning normalized Python values from
both. Every test written against ``surface`` is therefore also a CLI↔library
parity test — the cheapest way to hold the contract that the two surfaces stay
1:1 (SPEC §7.4).

The CLI side deliberately goes through the machine-readable output
(``--format json`` / ``--json``) rather than reaching into the library: that is
what a user scripting ``eo`` sees, so drift in the JSON projection shows up
here instead of in production.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from eosdk.models import Collection, Node, Product, Queryable

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from eosdk.client import Client


@dataclass(frozen=True)
class Transfer:
    """The subset of ``DownloadReport`` both surfaces can report."""

    path: Path
    bytes: int
    checksum_verified: bool | None
    product_id: str | None = None


@dataclass
class CliResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_bytes: bytes
    exception: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def json_lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stdout.splitlines() if line.strip()]

    def json(self) -> Any:
        return json.loads(self.stdout)


Invoke = Callable[..., CliResult]

_DONE_LINE = re.compile(r"^done (?P<path>.+) \((?P<bytes>[\d,]+) B, (?P<verified>[^)]+)\)$")


class Surface(Protocol):
    name: str

    def login(self, username: str, password: str) -> None: ...

    def search(self, **kwargs: Any) -> list[Product]: ...

    def get(self, product_id: str, *, protocol: str = "stac") -> Product: ...

    def collections(self, *, protocol: str = "stac") -> list[Collection]: ...

    def queryables(self, collection: str, *, protocol: str = "stac") -> list[Queryable]: ...

    def download(
        self,
        products: Sequence[Product],
        target: Path,
        *,
        via: str = "http",
        concurrency: int = 4,
        checksum: bool = True,
        resume: bool = True,
    ) -> list[Transfer]: ...

    def list(
        self, product: Product, path: str = "", *, via: str = "http", recursive: bool = False
    ) -> list[Node]: ...

    def read(self, product: Product, path: str, *, via: str = "s3") -> bytes: ...


def product_ref(product: Product, via: str) -> str:
    """What the CLI accepts for this product on this backend (SPEC §6.6)."""
    if via == "s3":
        if not product.s3_path:
            raise AssertionError(f"product {product.name} has no s3_path for --via s3")
        return product.s3_path
    return product.id


class LibrarySurface:
    name = "library"

    def __init__(self, client: Client) -> None:
        self.client = client

    def login(self, username: str, password: str) -> None:
        self.client.auth.login(username, password)

    def search(self, **kwargs: Any) -> list[Product]:
        return list(self.client.search(**kwargs))

    def get(self, product_id: str, *, protocol: str = "stac") -> Product:
        return self.client.get(product_id, protocol=protocol)

    def collections(self, *, protocol: str = "stac") -> list[Collection]:
        return self.client.collections(protocol=protocol)

    def queryables(self, collection: str, *, protocol: str = "stac") -> list[Queryable]:
        return self.client.queryables(collection, protocol=protocol)

    def download(
        self,
        products: Sequence[Product],
        target: Path,
        *,
        via: str = "http",
        concurrency: int = 4,
        checksum: bool = True,
        resume: bool = True,
    ) -> list[Transfer]:
        reports = self.client.download(
            list(products),
            target=target,
            via=via,
            concurrency=concurrency,
            checksum=checksum,
            resume=resume,
        )
        return [
            Transfer(
                path=report.path,
                bytes=report.bytes,
                checksum_verified=report.checksum_verified,
                product_id=report.product_id,
            )
            for report in reports
        ]

    def list(
        self, product: Product, path: str = "", *, via: str = "http", recursive: bool = False
    ) -> list[Node]:
        return self.client.list(product, path, via=via, recursive=recursive)

    def read(self, product: Product, path: str, *, via: str = "s3") -> bytes:
        with self.client.open(product, path, via=via) as handle:
            return handle.read()


class CliSurface:
    name = "cli"

    def __init__(self, invoke: Invoke) -> None:
        self.invoke = invoke

    # -- helpers ---------------------------------------------------------------

    def run(self, *args: str, **kwargs: Any) -> CliResult:
        result = self.invoke(*args, **kwargs)
        if not result.ok:
            raise AssertionError(
                f"`eo {' '.join(args)}` exited {result.exit_code}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    # -- surface ---------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        self.run("auth", "login", "--username", username, "--password", password)

    def search(self, **kwargs: Any) -> list[Product]:
        args = ["search", "--format", "json"]
        args += _search_args(kwargs)
        return [Product.model_validate(entry) for entry in self.run(*args).json_lines()]

    def get(self, product_id: str, *, protocol: str = "stac") -> Product:
        result = self.run("get", product_id, "--protocol", protocol, "--json")
        return Product.model_validate(result.json())

    def collections(self, *, protocol: str = "stac") -> list[Collection]:
        result = self.run("collections", "--protocol", protocol, "--json")
        return [Collection.model_validate(entry) for entry in result.json()]

    def queryables(self, collection: str, *, protocol: str = "stac") -> list[Queryable]:
        result = self.run("queryables", collection, "--protocol", protocol, "--json")
        return [Queryable.model_validate(entry) for entry in result.json()]

    def download(
        self,
        products: Sequence[Product],
        target: Path,
        *,
        via: str = "http",
        concurrency: int = 4,
        checksum: bool = True,
        resume: bool = True,
    ) -> list[Transfer]:
        payload = "\n".join(product.model_dump_json() for product in products)
        args = [
            "download",
            "-",
            "--output",
            str(target),
            "--via",
            via,
            "--concurrency",
            str(concurrency),
            "--checksum" if checksum else "--no-checksum",
            "--resume" if resume else "--no-resume",
        ]
        result = self.run(*args, input=payload)
        return _parse_done_lines(result.stdout)

    def list(
        self, product: Product, path: str = "", *, via: str = "http", recursive: bool = False
    ) -> list[Node]:
        args = ["list", product_ref(product, via), path, "--via", via, "--json"]
        if recursive:
            args.append("--recursive")
        return [Node.model_validate(entry) for entry in self.run(*args).json()]

    def read(self, product: Product, path: str, *, via: str = "s3") -> bytes:
        return self.run("cat", product_ref(product, via), path, "--via", via).stdout_bytes


def _search_args(kwargs: Mapping[str, Any]) -> list[str]:
    args: list[str] = []
    if kwargs.get("collection") is not None:
        args += ["--collection", str(kwargs["collection"])]
    if kwargs.get("bbox") is not None:
        args += ["--bbox", ",".join(str(value) for value in kwargs["bbox"])]
    interval = kwargs.get("datetime")
    if interval:
        start, _, end = str(interval).partition("/")
        if start and start != "..":
            args += ["--from", start]
        if end and end != "..":
            args += ["--to", end]
    for key, value in (kwargs.get("filters") or {}).items():
        args += ["--filter", f"{key}={value}"]
    if kwargs.get("limit") is not None:
        args += ["--limit", str(kwargs["limit"])]
    if kwargs.get("sort") is not None:
        args += ["--sort", str(kwargs["sort"])]
    args += ["--protocol", str(kwargs.get("protocol", "stac"))]
    return args


def _parse_done_lines(stdout: str) -> list[Transfer]:
    transfers: list[Transfer] = []
    for line in stdout.splitlines():
        match = _DONE_LINE.match(line.strip())
        if match is None:
            continue
        transfers.append(
            Transfer(
                path=Path(match.group("path")),
                bytes=int(match.group("bytes").replace(",", "")),
                checksum_verified=match.group("verified") == "verified",
            )
        )
    return transfers


def normalized_products(products: Iterable[Product]) -> list[dict[str, Any]]:
    """Comparable projection of a product list, independent of surface/protocol."""
    return sorted(
        (
            {
                "id": product.id,
                "name": product.name,
                "collection": product.collection,
                "datetime": product.datetime.isoformat() if product.datetime else None,
                "size": product.size,
                "cloud_cover": product.cloud_cover,
            }
            for product in products
        ),
        key=lambda entry: str(entry["name"]),
    )


def normalized_nodes(nodes: Iterable[Node]) -> set[tuple[str, str, bool, int | None]]:
    return {(node.path, node.name, node.is_dir, node.size) for node in nodes}
