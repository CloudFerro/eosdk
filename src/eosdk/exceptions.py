"""Exception taxonomy for eosdk (SPEC §8).

Every exception inherits :class:`EosdkError` and carries structured attributes;
messages always name the failing service and, where possible, the config knob
that fixes the problem.
"""

from __future__ import annotations


class EosdkError(Exception):
    """Base class for all eosdk errors."""


class ConfigError(EosdkError):
    """Configuration problem: bad TOML, unknown profile, missing/pending endpoint."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.hint = hint
        super().__init__(message if hint is None else f"{message} ({hint})")


class AuthError(EosdkError):
    """Login/refresh failure, or HTTP 401 persisting after a forced refresh.

    ``code`` carries the OAuth 2.0 ``error`` code from the token endpoint
    (e.g. ``authorization_pending``, ``invalid_grant``) when one was returned.
    """

    def __init__(
        self,
        message: str,
        *,
        realm: str | None = None,
        profile: str | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.realm = realm
        self.profile = profile
        self.code = code
        self.status_code = status_code
        parts = [message]
        if realm is not None:
            parts.append(f"realm={realm!r}")
        if profile is not None:
            parts.append(f"profile={profile!r}")
        super().__init__(" | ".join(parts))


class EndpointUnreachable(EosdkError):
    """Connect/timeout failure on first use of a service or during doctor checks."""

    def __init__(
        self,
        *,
        service: str,
        url: str,
        hint: str | None = None,
    ) -> None:
        self.service = service
        self.url = url
        self.hint = hint
        message = f"{service} is unreachable at {url}"
        if hint is not None:
            message = f"{message} ({hint})"
        super().__init__(message)


class UnsupportedApiVersion(EosdkError):
    """A service advertises an API version outside the range this SDK supports."""

    def __init__(
        self,
        *,
        service: str,
        advertised: str,
        supported: str,
        remediation: str | None = None,
    ) -> None:
        self.service = service
        self.advertised = advertised
        self.supported = supported
        message = f"{service} advertises API version {advertised}; this eosdk supports {supported}"
        if remediation is not None:
            message = f"{message} — {remediation}"
        super().__init__(message)


class UnsupportedQueryFeature(EosdkError):
    """A Query construct is not expressible in the selected catalogue backend."""

    def __init__(self, *, backend: str, feature: str) -> None:
        self.backend = backend
        self.feature = feature
        super().__init__(f"the {backend!r} catalogue backend does not support: {feature}")


class UnsupportedCapability(EosdkError):
    """No available strategy of the chosen backend supports the requested capability."""

    def __init__(
        self,
        *,
        backend: str,
        capability: str,
        alternative: str | None = None,
    ) -> None:
        self.backend = backend
        self.capability = capability
        self.alternative = alternative
        message = f"backend {backend!r} does not support the {capability!r} capability"
        if alternative is not None:
            message = f"{message}; {alternative}"
        super().__init__(message)


class CollectionNotFound(EosdkError):
    """A search referenced a collection the catalogue does not know.

    Raised instead of silently returning an empty result (STAC servers answer
    an unknown collection with an empty FeatureCollection, not an error)."""

    def __init__(
        self,
        *,
        collection: str,
        backend: str,
        suggestions: list[str] | None = None,
        hint: str | None = None,
    ) -> None:
        self.collection = collection
        self.backend = backend
        self.suggestions = suggestions or []
        self.hint = hint
        message = f"collection {collection!r} does not exist in the {backend} catalogue"
        if self.suggestions:
            message = f"{message} — did you mean: {', '.join(self.suggestions)}?"
        if hint is not None:
            message = f"{message} ({hint})"
        super().__init__(message)


class ProductNotFound(EosdkError):
    """Catalogue get or download referenced a product the backend does not know."""

    def __init__(self, *, product_id: str, backend: str) -> None:
        self.product_id = product_id
        self.backend = backend
        super().__init__(f"product {product_id!r} not found ({backend})")


class DownloadError(EosdkError):
    """Transfer failed after retries were exhausted."""

    def __init__(
        self,
        message: str,
        *,
        product_id: str,
        backend: str,
        cause: BaseException | None = None,
    ) -> None:
        self.product_id = product_id
        self.backend = backend
        self.cause = cause
        detail = f"{message} (product={product_id!r}, backend={backend})"
        if cause is not None:
            detail = f"{detail}: {cause!r}"
        super().__init__(detail)


class S3KeyLimitReached(EosdkError):
    """The S3 credentials service refused to create a key pair: the account's cap on
    concurrent keys is reached. Distinct from :class:`QuotaExceeded` (429 rate
    limiting): this is a resource cap that only revoking a key can clear."""

    def __init__(
        self,
        *,
        service: str = "s3_credentials",
        detail: str | None = None,
        limit: int | None = None,
    ) -> None:
        self.service = service
        self.detail = detail
        self.limit = limit
        message = f"{service} refused to create an S3 key: the account's key limit is reached"
        if limit is not None:
            message = f"{message} (limit={limit})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(
            f"{message} — revoke an unused key (`eo keys list`, `eo keys revoke <access_id>`) "
            "or reuse an existing labeled key (`get_or_create(label=...)`)"
        )


class S3KeyNotActive(EosdkError):
    """A freshly created S3 key was not accepted by the S3 endpoint in time.

    Key pairs propagate from the credentials service to the S3 gateway
    asynchronously; normally that takes seconds. A key the gateway never
    accepts usually means the account holds too many keys — the credentials
    service lists it, but the gateway never provisioned it.
    """

    def __init__(self, *, access_key: str, waited: float, code: str) -> None:
        self.access_key = access_key
        self.waited = waited
        self.code = code
        super().__init__(
            f"the S3 endpoint did not accept freshly created key {access_key!r} "
            f"within {waited:.0f}s ({code}) — propagation normally takes seconds; "
            "a key that never activates usually means the account holds too many "
            "keys — revoke unused ones (`eo keys list`, `eo keys revoke <access_id>`) "
            "and retry"
        )


class QuotaExceeded(EosdkError):
    """The service answered 429 / quota exhausted."""

    def __init__(self, *, service: str, retry_after: float | None = None) -> None:
        self.service = service
        self.retry_after = retry_after
        message = f"{service} quota exceeded (HTTP 429)"
        if retry_after is not None:
            message = f"{message}, retry after {retry_after:g}s"
        super().__init__(message)
