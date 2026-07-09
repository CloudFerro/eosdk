"""Extending the SDK with plugins (SPEC §11).

What this shows
---------------
Third parties can add catalogue protocols (``protocol="..."``) and download
backends (``via="..."``) without touching eosdk. A plugin package exposes a
``PluginSpec`` through the ``eosdk.plugins`` entry-point group:

    # pyproject.toml of *your* plugin package
    [project.entry-points."eosdk.plugins"]
    my-backend = "my_pkg.eosdk_plugin:PLUGIN"

Rules of the game:

* the factory is called lazily with the ``Client`` — reuse its resolved
  config and auth rather than inventing your own
* downloader plugins declare ``strategies`` (name + capabilities), so the
  capability gate (`download`/`list`/`open`) works before any network I/O
* built-ins win name collisions; a broken plugin is skipped with a warning,
  never crashing ``Client``

This file is both a template for a real plugin module and a runnable demo
(it registers the plugin in-process instead of installing a package).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from eosdk import Client
from eosdk.eodata.capabilities import Capability, Strategy
from eosdk.models import Collection, Page, Product, Queryable, SearchResult
from eosdk.plugins import PluginSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from eosdk.eodata.base import DownloadReport, ProgressEvent
    from eosdk.models import Query


# -- 1. a downloader plugin: `client.download(..., via="localfs")` ----------------
#
# A toy backend that "downloads" by touching files in the target directory.
# A real one would subclass eosdk.eodata.base.BaseDownloader to inherit the
# shared retry/checksum/concurrency machinery and only implement _open_stream.


class LocalFsDownloader:
    backend = "localfs"

    def __init__(self, client: Client) -> None:
        # The factory hands us the live Client: config, auth, and transport
        # are already resolved — e.g. client.auth.access_token() for tokens.
        self._client = client

    def fetch(
        self,
        products: Product | Iterable[Product],
        target: Path | str,
        *,
        concurrency: int = 4,
        resume: bool = True,
        checksum: bool = True,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadReport]:
        from eosdk.eodata.base import DownloadReport

        items = [products] if isinstance(products, Product) else list(products)
        target_dir = Path(target)
        target_dir.mkdir(parents=True, exist_ok=True)
        reports = []
        for product in items:
            path = target_dir / f"{product.name}.placeholder"
            path.write_text(f"pretend-download of {product.id}\n")
            reports.append(
                DownloadReport(
                    product_id=product.id,
                    path=path,
                    bytes=path.stat().st_size,
                    checksum_verified=None,
                    attempts=1,
                )
            )
        return reports


# -- 2. a catalogue plugin: `client.search(..., protocol="static")` -----------------
#
# Implements the Catalogue protocol (search / get / collections) over a
# hard-coded list — stand-in for any non-STAC/OData catalogue API.

_ITEMS = [
    Product(id="demo-1", name="DEMO_PRODUCT_1", collection="demo"),
    Product(id="demo-2", name="DEMO_PRODUCT_2", collection="demo"),
]


class StaticCatalogue:
    backend = "static"

    def __init__(self, client: Client) -> None:
        self._client = client

    def search(self, query: Query) -> SearchResult:
        hits = [p for p in _ITEMS if query.collection in (None, p.collection)]
        if query.limit is not None:
            hits = hits[: query.limit]
        # One-page result; multi-page backends return a next_token per page.
        return SearchResult(lambda token: Page(hits, next_token=None), matched=len(hits))

    def get(self, product_id: str) -> Product:
        from eosdk.exceptions import ProductNotFound

        for product in _ITEMS:
            if product.id == product_id:
                return product
        raise ProductNotFound(product_id=product_id, backend=self.backend)

    def collections(self) -> list[Collection]:
        return [Collection(id="demo", title="Demo collection")]

    def queryables(self, collection: str) -> list[Queryable]:
        return [Queryable(name="cloudCover", type="number")]


# -- 3. the specs a plugin package exports via its entry points ----------------------

DOWNLOADER_PLUGIN = PluginSpec(
    name="localfs",                     # the value users pass as via="localfs"
    kind="downloader",
    factory=LocalFsDownloader,          # called lazily with (client)
    strategies=(
        # Declares what the backend can do; client.open(..., via="localfs")
        # is rejected up-front because OPEN is not in this set.
        Strategy("native", frozenset({Capability.DOWNLOAD})),
    ),
)

CATALOGUE_PLUGIN = PluginSpec(
    name="static",                      # the value users pass as protocol="static"
    kind="catalogue",
    factory=StaticCatalogue,
)


# -- 4. runnable demo ------------------------------------------------------------------
#
# Real plugins are picked up automatically once their package is installed.
# For this self-contained demo we register the specs in-process instead.


def _register_in_process(*specs: PluginSpec) -> None:
    """Demo-only shim standing in for installed entry points."""
    import eosdk.plugins as plugins_module

    class _FakeEntryPoint:
        def __init__(self, spec: PluginSpec) -> None:
            self.name = spec.name
            self.value = f"{__name__}:{spec.name}"
            self._spec = spec

        def load(self) -> Any:
            return self._spec

    fakes = [_FakeEntryPoint(spec) for spec in specs]
    plugins_module.entry_points = lambda group: fakes  # type: ignore[assignment]


def main() -> None:
    _register_in_process(DOWNLOADER_PLUGIN, CATALOGUE_PLUGIN)

    with Client() as client:
        # The plugin catalogue behaves exactly like the built-in protocols.
        results = client.search(collection="demo", protocol="static")
        print(f"static catalogue matched {results.matched}:")
        for product in results:
            print(f"  {product.id}  {product.name}")

        # And the plugin downloader plugs into client.download()/list()/open().
        reports = client.download(list(results), target="./data", via="localfs")
        for report in reports:
            print(f"  wrote {report.path}")

        # Capability gate in action: this plugin never declared OPEN.
        from eosdk.exceptions import UnsupportedCapability

        try:
            client.open(_ITEMS[0], path="x/y", via="localfs")
        except UnsupportedCapability as exc:
            print(f"  as expected: {exc}")


if __name__ == "__main__":
    main()
