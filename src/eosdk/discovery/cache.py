"""On-disk TTL cache for discovery documents with offline fallback (SPEC §6.2)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eosdk.discovery.models import DiscoveryDocument, DiscoveryError, parse_document
from eosdk.exceptions import EndpointUnreachable

if TYPE_CHECKING:
    from eosdk.transport import Transport

logger = logging.getLogger(__name__)

DEFAULT_TTL = 24 * 3600.0  # seconds


def default_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "eosdk" / "discovery"


@dataclass(frozen=True)
class CachedDocument:
    raw: dict[str, Any]
    fetched_at: float
    stale: bool


class DiscoveryCache:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        ttl: float = DEFAULT_TTL,
        now: Any = time.time,
    ) -> None:
        self._dir = directory or default_cache_dir()
        self._ttl = ttl
        self._now = now

    def _path(self, url: str) -> Path:
        return self._dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def get(self, url: str) -> CachedDocument | None:
        """Return the cached document even when stale — the caller decides."""
        try:
            data = json.loads(self._path(url).read_text())
            fetched_at = float(data["fetched_at"])
            return CachedDocument(
                raw=data["document"],
                fetched_at=fetched_at,
                stale=self._now() - fetched_at > self._ttl,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, url: str, raw: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"fetched_at": self._now(), "document": raw}, fh)
            os.replace(tmp_name, self._path(url))
        except BaseException:
            os.unlink(tmp_name)
            raise

    def invalidate(self, url: str) -> None:
        self._path(url).unlink(missing_ok=True)


def fetch_document(
    url: str,
    cache: DiscoveryCache,
    transport: Transport,
    *,
    refresh: bool = False,
) -> DiscoveryDocument:
    """Serve fresh cache; else fetch; else fall back to a stale cache (offline)."""
    cached = cache.get(url)
    if cached is not None and not cached.stale and not refresh:
        return parse_document(cached.raw)

    try:
        response = transport.request("GET", url, service="discovery")
        response.raise_for_status()
        raw = response.json()
        document = parse_document(raw)  # validate before caching
        cache.put(url, raw)
        return document
    except DiscoveryError:
        raise  # a served-but-unusable document is a real error, not an outage
    except Exception as exc:
        if cached is not None:  # offline fallback: last known good document
            age_hours = (time.time() - cached.fetched_at) / 3600
            logger.warning(
                "discovery fetch failed (%s); using cached document from %.1f h ago",
                exc,
                age_hours,
            )
            return parse_document(cached.raw)
        raise EndpointUnreachable(
            service="discovery",
            url=url,
            hint="check the platform root / EOSDK_DISCOVERY_URL, or pin endpoints manually",
        ) from exc
