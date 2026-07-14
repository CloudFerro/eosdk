"""Discovery resolver: the object behind ``client.discovery`` (SPEC §6.2).

Receives only the bootstrap ``platform``/``discovery_url`` strings and a
transport — never the Settings object (cycle break, SPEC §4.2). Fetches and
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
            assert url is not None, "discovery is not configured (no platform root)"
            self._document = fetch_document(url, self._cache, self._transport, refresh=refresh)
            if self._on_document is not None:
                self._on_document(self._document)
        return self._document

    def services(self) -> dict[str, Any]:
        """The parsed discovery document's services mapping (SPEC §7.2)."""
        return dict(self.document().services)

    def refresh(self) -> DiscoveryDocument:
        """Bust the TTL cache and re-fetch (SPEC §6.2)."""
        return self.document(refresh=True)

    def endpoints(self) -> dict[str, str]:
        """Projected flat endpoint fields (SPEC §6.2 mapping)."""
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
