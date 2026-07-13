"""Rate-limited ``/ready`` probes for eodata-backed services (Zipper, Exos).

Zipper and Exos expose a ``/ready`` endpoint that reports whether the eodata
store behind them is available. The endpoint must not be polled aggressively,
so every verdict is persisted on disk and re-served for ``MIN_PROBE_INTERVAL``
seconds — repeated ``eo doctor`` runs inside that window reuse the last
verdict instead of issuing another request. Probes are also single-shot
(``RetryPolicy(attempts=1)``): a 503 from ``/ready`` is an answer
("not ready"), not a transient error worth the transport's backoff loop.

Connect failures are *not* recorded: no server answered, so there is no load
to throttle, and the next run should see a fix immediately.
"""

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

from eosdk.exceptions import EndpointUnreachable, QuotaExceeded
from eosdk.transport import RetryPolicy, route

if TYPE_CHECKING:
    from eosdk.transport import Transport

logger = logging.getLogger(__name__)

MIN_PROBE_INTERVAL = 300.0  # seconds between live /ready probes per endpoint
_NO_RETRY = RetryPolicy(attempts=1)


def default_readiness_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "eosdk" / "readiness"


@dataclass(frozen=True)
class Readiness:
    ready: bool
    detail: str
    age: float | None = None  # seconds since the live probe; None -> just probed

    @property
    def cached(self) -> bool:
        return self.age is not None


class ReadinessProbe:
    """Probe ``{base}/ready`` at most once per ``interval`` per endpoint."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        interval: float = MIN_PROBE_INTERVAL,
        now: Any = time.time,
    ) -> None:
        self._dir = directory or default_readiness_dir()
        self.interval = interval
        self._now = now

    def _path(self, url: str) -> Path:
        return self._dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def _load(self, url: str) -> Readiness | None:
        try:
            data = json.loads(self._path(url).read_text())
            age = self._now() - float(data["probed_at"])
            if not 0 <= age <= self.interval:  # negative age: clock went backwards
                return None
            return Readiness(bool(data["ready"]), str(data["detail"]), age=age)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _store(self, url: str, result: Readiness) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        # mkstemp creates the file 0600 where POSIX modes exist; no fchmod (absent on Windows)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(
                    {"probed_at": self._now(), "ready": result.ready, "detail": result.detail}, fh
                )
            os.replace(tmp_name, self._path(url))
        except BaseException:
            os.unlink(tmp_name)
            raise

    def check(
        self,
        transport: Transport,
        base: str,
        *,
        service: str,
        timeout: float | None = None,
        force: bool = False,
    ) -> Readiness:
        """Return the throttled verdict; ``force`` probes live regardless.

        A forced probe still records its verdict, so it resets the interval
        for subsequent unforced checks rather than side-stepping the limit.
        """
        url = route(base, "ready")
        if not force:
            cached = self._load(url)
            if cached is not None:
                return cached

        kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            response = transport.request("GET", url, service=service, retry=_NO_RETRY, **kwargs)
            verdict = response.is_success
            state = "ready" if verdict else "not ready"
            detail = f"eodata {state} (GET {url} -> HTTP {response.status_code})"
        except EndpointUnreachable as exc:
            return Readiness(False, str(exc))  # nothing answered: nothing to throttle
        except QuotaExceeded as exc:  # 429: the strongest reason to record the verdict
            verdict, detail = False, str(exc)
        result = Readiness(verdict, detail)
        try:
            self._store(url, result)
        except OSError as exc:  # the verdict outranks the throttle bookkeeping
            logger.warning("could not persist readiness verdict for %s: %s", url, exc)
        return result
