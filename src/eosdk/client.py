"""The ``Client`` facade — the one import most users need.

Construction is offline: local config layers are resolved and URL-validated,
but nothing is fetched. Discovery-sourced endpoints stay ``<pending>`` until
first use of the owning service. Domain modules are constructed
lazily and receive resolved endpoints + credential providers — they never see
config or environment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from eosdk.config import load
from eosdk.exceptions import ConfigError
from eosdk.models import Query
from eosdk.transport import Transport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path
    from typing import IO

    from eosdk.auth.keycloak import KeycloakAuth
    from eosdk.auth.s3_keys import S3KeysProvider
    from eosdk.catalogue.base import Catalogue
    from eosdk.catalogue.odata import ODataCatalogue
    from eosdk.catalogue.stac import StacCatalogue
    from eosdk.config import ResolvedConfig
    from eosdk.discovery.models import DiscoveryDocument
    from eosdk.discovery.resolver import DiscoveryResolver
    from eosdk.eodata.base import DownloadReport, ProgressEvent
    from eosdk.eodata.capabilities import Capability
    from eosdk.eodata.http import HttpDownloader
    from eosdk.eodata.s3 import S3Downloader
    from eosdk.models import Collection, Node, Product, Queryable, SearchResult
    from eosdk.plugins import PluginSpec

logger = logging.getLogger(__name__)


class Client:
    def __init__(
        self,
        *,
        profile: str | None = None,
        platform: str | None = None,
        endpoints: Mapping[str, str] | None = None,
        verify: bool | None = None,
        timeout: float = 30.0,
        cwd: Path | None = None,
        user_config: Path | None = None,
        token_cache_dir: Path | None = None,
        keys_cache_dir: Path | None = None,
        discovery_cache_dir: Path | None = None,
        readiness_cache_dir: Path | None = None,
    ) -> None:
        self.config: ResolvedConfig = load(
            kwargs_endpoints=endpoints,
            platform=platform,
            profile=profile,
            cwd=cwd,
            user_config=user_config,
        )
        self._transport = Transport(
            timeout=timeout,
            verify=self.config.verify_tls if verify is None else verify,
        )
        self._user_config = user_config
        self._token_cache_dir = token_cache_dir
        self._keys_cache_dir = keys_cache_dir
        self._discovery_cache_dir = discovery_cache_dir
        self._readiness_cache_dir = readiness_cache_dir
        # (profile name, status) of the last discovery snapshot save, if any
        self.discovered_profile: tuple[str, str] | None = None
        self._discovery: DiscoveryResolver | None = None
        self._auth: KeycloakAuth | None = None
        self._stac: StacCatalogue | None = None
        self._odata: ODataCatalogue | None = None
        self._http: HttpDownloader | None = None
        self._s3: S3Downloader | None = None
        self._keys: S3KeysProvider | None = None
        self._plugin_registry: dict[str, PluginSpec] | None = None

    # -- discovery (SPEC §6.2): fetched lazily on first pending-endpoint use ---

    @property
    def discovery(self) -> DiscoveryResolver:
        if self._discovery is None:
            from eosdk.discovery.resolver import DiscoveryResolver

            self._discovery = DiscoveryResolver(
                platform=self.config.platform,
                discovery_url=self.config.endpoints.discovery_url,
                transport=self._transport,
                cache_dir=self._discovery_cache_dir,
                on_document=self._snapshot_discovered_profile,
            )
        return self._discovery

    def _snapshot_discovered_profile(self, document: DiscoveryDocument) -> None:
        """Save the online configuration as a profile named after the platform.

        Runs on every document load, so a changed online document resyncs the
        managed profile; a same-named user-owned profile is never overwritten.
        Persistence failures must not break the API call that triggered
        discovery — they are logged and swallowed.
        """
        from eosdk.config.loader import user_config_path
        from eosdk.config.profiles import save_discovered_profile
        from eosdk.discovery.models import project_endpoints

        info = document.platform
        name = info.profile_name if info is not None else None
        if info is None or name is None:
            return
        values = dict(project_endpoints(document))
        if self.config.platform:
            values["platform"] = self.config.platform
        if self.config.endpoints.discovery_url:
            values["discovery_url"] = self.config.endpoints.discovery_url
        if info.description:
            values["description"] = info.description
        path = self._user_config or user_config_path()
        try:
            status = save_discovered_profile(
                path, name, values=values, discovered_from=self.discovery.url or ""
            )
        except Exception as exc:
            logger.warning("could not save discovered profile %r to %s: %s", name, path, exc)
            return
        self.discovered_profile = (name, status)
        if status == "conflict":
            logger.warning(
                "profile %r in %s already exists and was not created from discovery; "
                "leaving it untouched — rename or delete it to let eosdk save the "
                "%s platform snapshot under that name",
                name,
                path,
                info.name,
            )
        elif status in {"created", "updated"}:
            logger.info(
                "%s profile %r in %s from the %s discovery document",
                status,
                name,
                path,
                info.name,
            )

    def _endpoint(self, fieldname: str, *, service: str) -> str:
        """Resolve an endpoint, triggering lazy discovery for <pending> fields."""
        try:
            return self.config.require(fieldname, service=service)
        except ConfigError:
            if not self.discovery.configured:
                raise
            self.config.apply_discovered(self.discovery.endpoints())
            return self.config.require(fieldname, service=service)

    def _check_api_version(self, service_key: str) -> None:
        from eosdk.versions import select_version

        advertised = (
            self.discovery.api_version_for(service_key) if self.discovery.configured else None
        )
        select_version(service_key, advertised)

    # -- lazy service accessors (SPEC §6.1: nothing fetched at construction) ---

    @property
    def auth(self) -> KeycloakAuth:
        if self._auth is None:
            from eosdk.auth.keycloak import KeycloakAuth

            # realm and client_id have no built-in default: they resolve from
            # local config or the platform's discovery document, like the URL.
            self._auth = KeycloakAuth(
                url=self._endpoint("keycloak", service="keycloak"),
                realm=self._endpoint("keycloak_realm", service="keycloak"),
                client_id=self._endpoint("keycloak_client_id", service="keycloak"),
                transport=self._transport,
                profile=self.config.profile or "default",
                cache_dir=self._token_cache_dir,
            )
        return self._auth

    def _stac_catalogue(self) -> StacCatalogue:
        if self._stac is None:
            from eosdk.catalogue.stac import StacCatalogue

            self._stac = StacCatalogue(
                self._endpoint("catalogue_stac", service="catalogue_stac"),
                transport=self._transport,
                auth=self.auth,
            )
        return self._stac

    def _odata_catalogue(self) -> ODataCatalogue:
        if self._odata is None:
            from eosdk.catalogue.odata import ODataCatalogue

            self._check_api_version("catalogue/odata")
            self._odata = ODataCatalogue(
                self._endpoint("catalogue_odata", service="catalogue_odata"),
                transport=self._transport,
                auth=self.auth,
            )
        return self._odata

    def _http_downloader(self, strategy: str | None = None) -> HttpDownloader:
        if self._http is None:
            from eosdk.eodata.http import HttpDownloader

            self._check_api_version("data_access/http/odata")
            self._http = HttpDownloader(
                self._endpoint("eodata_http", service="eodata_http"),
                transport=self._transport,
                auth=self.auth,
                strategy=strategy,
            )
        return self._http

    def _keys_provider(self) -> S3KeysProvider:
        if self._keys is None:
            from eosdk.auth.s3_keys import S3KeysProvider

            self._check_api_version("data_access/s3/credentials")

            self._keys = S3KeysProvider(
                auth=self.auth,
                base_url=self._endpoint("s3_credentials", service="s3_credentials"),
                transport=self._transport,
                profile=self.config.profile or "default",
                cache_dir=self._keys_cache_dir,
            )
        return self._keys

    def _s3_downloader(self) -> S3Downloader:
        # Lazy on purpose: S3 keys must not be minted until first S3 use.
        if self._s3 is None:
            from eosdk.eodata.s3 import S3Downloader

            self._s3 = S3Downloader(
                self._endpoint("s3_endpoint", service="s3"),
                region=self.config.endpoints.s3_region,
                credentials=self._keys_provider(),
                verify=self.config.verify_tls,
            )
        return self._s3

    # -- public surface (SPEC §7.1) ---------------------------------------------

    def _plugins(self) -> dict[str, PluginSpec]:
        if self._plugin_registry is None:
            from eosdk.plugins import load_plugins

            self._plugin_registry = load_plugins()
        return self._plugin_registry

    def _catalogue(self, protocol: str) -> Catalogue:
        if protocol == "stac":
            return self._stac_catalogue()
        if protocol == "odata":
            return self._odata_catalogue()
        plugin = self._plugins().get(protocol)
        if plugin is not None and plugin.kind == "catalogue":
            return plugin.factory(self)  # type: ignore[no-any-return]
        raise ConfigError(f"unknown catalogue protocol {protocol!r}", hint="use stac or odata")

    def _downloader(self, via: str, capability: Capability) -> Any:
        """Capability-first dispatch: select the backend before any network I/O."""
        from eosdk.eodata.capabilities import BUILTIN_MATRIX, select_strategy

        if via not in BUILTIN_MATRIX:
            plugin = self._plugins().get(via)
            if plugin is not None and plugin.kind == "downloader":
                if plugin.strategies:
                    select_strategy(via, capability, plugin.strategies)
                return plugin.factory(self)
            raise ConfigError(f"unknown download backend {via!r}", hint="use http or s3")
        strategies = (
            self.discovery.strategies_for(via) if self.discovery.configured else BUILTIN_MATRIX[via]
        )
        chosen = select_strategy(via, capability, strategies)
        if via == "http":
            return self._http_downloader(strategy=chosen.name)
        return self._s3_downloader()

    def search(
        self,
        *,
        collection: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        datetime: str | None = None,
        filters: Mapping[str, str] | None = None,
        limit: int | None = None,
        sort: str | None = None,
        protocol: str = "stac",
    ) -> SearchResult:
        # Refuse an unbounded search on both surfaces (SPEC §6.5): the CLI and
        # the library share this guard so identical inputs behave identically,
        # and it also settles the STAC-permits/OData-refuses split below it.
        if collection is None and bbox is None and not datetime and not filters:
            raise ConfigError(
                "refusing an unbounded search",
                hint="narrow it with a collection, bbox, datetime range, or filter",
            )
        query = Query(
            collection=collection,
            bbox=bbox,
            datetime=datetime,
            filters=dict(filters or {}),
            limit=limit,
            sort=sort,
        )
        return self._catalogue(protocol).search(query)

    def get(self, product_id: str, *, protocol: str = "stac") -> Product:
        return self._catalogue(protocol).get(product_id)

    def collections(self, *, protocol: str = "stac") -> list[Collection]:
        return self._catalogue(protocol).collections()

    def queryables(self, collection: str, *, protocol: str = "stac") -> list[Queryable]:
        """Filterable attributes a collection advertises.

        Names are backend-native and usable directly as ``filters`` keys for
        that protocol. Raises ``UnsupportedQueryFeature`` when the backend
        (or the given collection) does not advertise queryables.
        """
        return self._catalogue(protocol).queryables(collection)

    def download(
        self,
        products: Product | Iterable[Product],
        target: Path | str,
        *,
        via: str = "http",
        concurrency: int = 4,
        resume: bool = True,
        checksum: bool = True,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadReport]:
        from eosdk.eodata.capabilities import Capability

        downloader = self._downloader(via, Capability.DOWNLOAD)
        return downloader.fetch(  # type: ignore[no-any-return]
            products,
            target,
            concurrency=concurrency,
            resume=resume,
            checksum=checksum,
            progress=progress,
        )

    def list(
        self, product: Product, path: str = "", *, via: str = "http", recursive: bool = False
    ) -> list[Node]:
        """Files inside a product (the ``Listable`` capability)."""
        from eosdk.eodata.capabilities import Capability

        backend = self._downloader(via, Capability.LIST)
        return backend.list(product, path, recursive=recursive)  # type: ignore[no-any-return]

    def open(self, product: Product, path: str, *, via: str = "s3") -> IO[bytes]:
        """Ranged reads of one file inside a product (the ``RandomAccess`` capability)."""
        from eosdk.eodata.capabilities import Capability

        backend = self._downloader(via, Capability.OPEN)
        return backend.open(product, path)  # type: ignore[no-any-return]

    @property
    def keys(self) -> S3KeysProvider:
        """The S3 credentials (keys) client."""
        return self._keys_provider()

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
