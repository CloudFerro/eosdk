"""The ``Client`` facade — the one import most users need (SPEC §4.1).

Construction is offline: local config layers are resolved and URL-validated,
but nothing is fetched. Discovery-sourced endpoints stay ``<pending>`` until
first use of the owning service (SPEC §6.1). Domain modules are constructed
lazily and receive resolved endpoints + credential providers — they never see
config or environment (SPEC §4.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eosdk.config import load
from eosdk.exceptions import ConfigError, UnsupportedCapability
from eosdk.models import Query
from eosdk.transport import Transport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from eosdk.auth.keycloak import KeycloakAuth
    from eosdk.catalogue.stac import StacCatalogue
    from eosdk.config import ResolvedConfig
    from eosdk.eodata.base import DownloadReport, ProgressEvent
    from eosdk.eodata.zipper import ZipperDownloader
    from eosdk.models import Product, SearchResult


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
        self._token_cache_dir = token_cache_dir
        self._auth: KeycloakAuth | None = None
        self._stac: StacCatalogue | None = None
        self._zipper: ZipperDownloader | None = None

    # -- lazy service accessors (SPEC §6.1: nothing fetched at construction) ---

    @property
    def auth(self) -> KeycloakAuth:
        if self._auth is None:
            from eosdk.auth.keycloak import KeycloakAuth

            self._auth = KeycloakAuth(
                url=self.config.require("keycloak", service="keycloak"),
                realm=self.config.endpoints.keycloak_realm,
                client_id=self.config.endpoints.keycloak_client_id,
                transport=self._transport,
                profile=self.config.profile or "default",
                cache_dir=self._token_cache_dir,
            )
        return self._auth

    def _stac_catalogue(self) -> StacCatalogue:
        if self._stac is None:
            from eosdk.catalogue.stac import StacCatalogue

            self._stac = StacCatalogue(
                self.config.require("catalogue_stac", service="catalogue_stac"),
                transport=self._transport,
                auth=self.auth,
            )
        return self._stac

    def _zipper_downloader(self) -> ZipperDownloader:
        if self._zipper is None:
            from eosdk.eodata.zipper import ZipperDownloader

            self._zipper = ZipperDownloader(
                self.config.require("zipper", service="zipper"),
                transport=self._transport,
                auth=self.auth,
            )
        return self._zipper

    # -- public surface (SPEC §7.1) ---------------------------------------------

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
        query = Query(
            collection=collection,
            bbox=bbox,
            datetime=datetime,
            filters=dict(filters or {}),
            limit=limit,
            sort=sort,
        )
        if protocol == "stac":
            return self._stac_catalogue().search(query)
        if protocol == "odata":
            raise ConfigError(
                "the odata catalogue backend ships in a later release; use protocol='stac'"
            )
        raise ConfigError(f"unknown catalogue protocol {protocol!r}")

    def get(self, product_id: str, *, protocol: str = "stac") -> Product:
        if protocol != "stac":
            raise ConfigError(f"unknown or not-yet-available catalogue protocol {protocol!r}")
        return self._stac_catalogue().get(product_id)

    def download(
        self,
        products: Product | Iterable[Product],
        target: Path | str,
        *,
        via: str = "zipper",
        concurrency: int = 4,
        resume: bool = True,
        checksum: bool = True,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadReport]:
        if via == "zipper":
            downloader = self._zipper_downloader()
        elif via == "exos":
            raise UnsupportedCapability(
                backend="exos",
                capability="download",
                alternative="the exos backend ships in Phase 2; use via='zipper'",
            )
        else:
            raise UnsupportedCapability(backend=via, capability="download")
        return downloader.fetch(
            products,
            target,
            concurrency=concurrency,
            resume=resume,
            checksum=checksum,
            progress=progress,
        )

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
