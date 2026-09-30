"""Discovery resolver: the object behind ``client.discovery``.

Receives only the bootstrap ``platform``/``discovery_url`` strings and a
transport — never the Settings object (cycle break). Fetches and
memoizes the platform document on first use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eosdk.discovery.cache import DiscoveryCache, fetch_document
from eosdk.discovery.models import (
    api_versions,
    derive_discovery_url,
    project_endpoints,
    strategy_sets,
)
from eosdk.eodata.capabilities import BUILTIN_MATRIX
from eosdk.exceptions import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from eosdk.discovery.models import DiscoveryDocument
    from eosdk.eodata.capabilities import Strategy
    from eosdk.transport import Transport


class DiscoveryResolver:
    def __init__(
        self,
        *,
        platform: str | None,
        discovery_url: str | None,
        transport: Transport,
        cache_dir: Path | None = None,
        on_document: Callable[[DiscoveryDocument], None] | None = None,
    ) -> None:
        self._platform = platform
        self._discovery_url = discovery_url
        self._transport = transport
        self._cache = DiscoveryCache(cache_dir)
        self._document: DiscoveryDocument | None = None
        self._on_document = on_document

    @property
    def configured(self) -> bool:
        return self._platform is not None or self._discovery_url is not None

    @property
    def url(self) -> str | None:
        if not self.configured:
            return None
        return derive_discovery_url(self._platform or "", self._discovery_url)

    def document(self, *, refresh: bool = False) -> DiscoveryDocument:
        if self._document is None or refresh:
            url = self.url
            if url is None:
                raise ConfigError(
                    "discovery is not configured",
                    hint="set a platform root or EOSDK_DISCOVERY_URL",
                )
            self._document = fetch_document(url, self._cache, self._transport, refresh=refresh)
            if self._on_document is not None:
                self._on_document(self._document)
        return self._document

    def services(self) -> dict[str, Any]:
        """The parsed discovery document's services mapping."""
        return dict(self.document().services)

    def refresh(self) -> DiscoveryDocument:
        """Bust the TTL cache and re-fetch."""
        return self.document(refresh=True)

    def endpoints(self) -> dict[str, str]:
        """Projected flat endpoint fields."""
        return project_endpoints(self.document())

    def strategies_for(self, backend: str) -> tuple[Strategy, ...]:
        """Discovery-merged strategies; built-ins when no platform is configured."""
        if not self.configured:
            return BUILTIN_MATRIX[backend]
        return strategy_sets(self.document())[backend]

    def api_version_for(self, key: str) -> str | None:
        """Advertised api_version for ``service`` or ``service/strategy``, if any."""
        if not self.configured:
            return None
        return api_versions(self.document()).get(key)
