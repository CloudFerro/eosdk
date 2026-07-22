"""Shared HTTP transport (SPEC §6.7).

One ``httpx`` client behind a thin wrapper adding the shared retry/backoff
policy, quota handling, User-Agent, and the :func:`route` URL builder — the
only sanctioned way to assemble service URLs (no scattered f-strings).

Everything stateless (``route``, ``odata_key``, :class:`RetryPolicy`, the retry
predicates) is module-level and will be reused verbatim by the async transport.
"""

from __future__ import annotations

import platform as _platform
import random
import string
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from eosdk import __version__
from eosdk.exceptions import EndpointUnreachable, QuotaExceeded, ServiceTimeout

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_FORMATTER = string.Formatter()


def route(base: str, template: str, /, **params: str) -> str:
    """Build a service URL from a version-free base and a path template.

    Placeholders are percent-encoded (``safe=""``); the template's own path
    structure is preserved. Missing or unknown parameters raise ``ValueError``
    at call time rather than producing a broken URL.
    """
    fields = {name for _, name, _, _ in _FORMATTER.parse(template) if name is not None}
    if "" in fields:
        raise ValueError(f"route template {template!r} has a positional placeholder")
    missing = fields - params.keys()
    if missing:
        raise ValueError(f"route template {template!r} missing params: {sorted(missing)}")
    unknown = params.keys() - fields
    if unknown:
        raise ValueError(f"route template {template!r} got unknown params: {sorted(unknown)}")
    encoded = {name: quote(value, safe="") for name, value in params.items()}
    return f"{base.rstrip('/')}/{template.format(**encoded).lstrip('/')}"


def odata_key(value: str) -> str:
    """Quote a string as an OData key literal: ``'...'`` with ``'`` doubled.

    Required for ``Nodes({name})`` segments whose names may contain spaces,
    parentheses, or quotes (SPEC §6.6) — never assemble these with f-strings.
    """
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    jitter: bool = True
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})
    retry_methods: frozenset[str] = frozenset({"GET", "HEAD"})

    def backoff(self, attempt: int) -> float:
        """Delay before retry ``attempt`` (1-based), exponential with jitter."""
        delay = min(self.backoff_base * (2.0 ** (attempt - 1)), self.backoff_max)
        if self.jitter:
            delay *= random.uniform(0.5, 1.0)
        return delay


def user_agent() -> str:
    return (
        f"eosdk/{__version__} "
        f"(CPython/{_platform.python_version()}; {_platform.system()}-{_platform.machine()})"
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to backoff


@dataclass
class Transport:
    """Sync HTTP transport shared by all domain modules.

    ``auth`` is passed per request, never client-wide: different services carry
    different credential providers, and the S3 path must never see JWTs.
    """

    timeout: float = 30.0
    verify: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if not self.verify:
            warnings.warn(
                "TLS verification is disabled (verify=False / EOSDK_TLS_VERIFY=0); "
                "use only for local development",
                stacklevel=3,
            )
        self._client = httpx.Client(
            timeout=self.timeout,
            verify=self.verify,
            follow_redirects=True,
            headers={"User-Agent": user_agent()},
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        service: str,
        auth: httpx.Auth | None = None,
        retry: RetryPolicy | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return self._send(
            method, url, service=service, auth=auth, retry=retry, stream=False, **kwargs
        )

    @contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        service: str,
        auth: httpx.Auth | None = None,
        retry: RetryPolicy | None = None,
        **kwargs: Any,
    ) -> Iterator[httpx.Response]:
        response = self._send(
            method, url, service=service, auth=auth, retry=retry, stream=True, **kwargs
        )
        try:
            yield response
        finally:
            response.close()

    def _send(
        self,
        method: str,
        url: str,
        *,
        service: str,
        auth: httpx.Auth | None,
        stream: bool,
        retry: RetryPolicy | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        # per-call override, like timeout: health probes treat 503 as an
        # answer ("not ready"), not as an invitation to back off and retry
        policy = self.retry if retry is None else retry
        retryable_method = method.upper() in policy.retry_methods
        last_status: httpx.Response | None = None
        for attempt in range(1, policy.attempts + 1):
            try:
                request = self._client.build_request(method, url, **kwargs)
                response = self._client.send(
                    request, auth=auth or httpx.USE_CLIENT_DEFAULT, stream=stream
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if retryable_method and attempt < policy.attempts:
                    self.sleep(policy.backoff(attempt))
                    continue
                raise EndpointUnreachable(
                    service=service,
                    url=url,
                    hint="check the endpoint URL for this service in your profile or EOSDK_* env",
                ) from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                # the connection was established; the server just never answered
                # in time — a bad URL is not the cause, so don't point there.
                if retryable_method and attempt < policy.attempts:
                    self.sleep(policy.backoff(attempt))
                    continue
                deadline = kwargs.get("timeout", self.timeout)
                raise ServiceTimeout(
                    service=service,
                    url=url,
                    timeout=deadline if isinstance(deadline, (int, float)) else None,
                ) from exc

            # taxonomy mapping (429 -> QuotaExceeded below) applies to every
            # method; only the retry loop is restricted to idempotent ones
            if response.status_code in policy.retry_statuses:
                last_status = response
                if retryable_method and attempt < policy.attempts:
                    retry_after = (
                        _retry_after_seconds(response) if response.status_code == 429 else None
                    )
                    if stream:
                        response.close()
                    else:
                        response.read()
                    self.sleep(max(retry_after or 0.0, policy.backoff(attempt)))
                    continue
            break

        if last_status is not None and response.status_code == 429:
            if stream:
                response.close()
            raise QuotaExceeded(service=service, retry_after=_retry_after_seconds(response))
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
