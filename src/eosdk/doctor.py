"""Health checks: config, auth, and service probes.

Checks degrade gracefully: a subsystem that is not configured (or not shipped
yet — Discovery before Phase 3) reports *skipped* with a reason, never a false
failure. ``ok`` is ``True``/``False``/``None`` (skipped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eosdk.exceptions import AuthError, EosdkError, UnsupportedApiVersion
from eosdk.transport import route

if TYPE_CHECKING:
    from collections.abc import Callable

    from eosdk.client import Client

PROBE_TIMEOUT = 5.0  # doctor must never hang
_VERSION_SEGMENT = re.compile(r"/v\d+/?$")
# catalogue_stac is the URL of a self-describing STAC landing page, used
# verbatim (the SDK never appends /vN to it) — and CDSE's landing page
# itself lives under /v1, so a version segment there is not a mistake.
_VERSION_CHECK_EXEMPT = frozenset({"catalogue_stac"})
# S3 error codes that indicate the managed key itself is bad — e.g. a key the
# credentials service still lists but the S3 endpoint never provisioned (seen
# when an account holds too many keys). The endpoint hint would misdirect here.
_S3_CREDENTIAL_ERRORS = frozenset(
    {"InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "ExpiredToken"}
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool | None  # None -> skipped
    detail: str
    hint: str | None = None


class _ProbeFailure(EosdkError):
    """A probe failure that diagnosed its own cause; its hint beats the generic one."""

    def __init__(self, message: str, *, hint: str) -> None:
        self.hint = hint
        super().__init__(message)


@dataclass
class Section:
    name: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(result.ok is False for result in self.results)


def _skip(name: str, reason: str) -> CheckResult:
    return CheckResult(name, None, reason)


def _probe(name: str, action: Callable[[], str], hint: str | None = None) -> CheckResult:
    try:
        return CheckResult(name, True, action())
    except EosdkError as exc:
        return CheckResult(name, False, str(exc), getattr(exc, "hint", None) or hint)
    except Exception as exc:  # a doctor check must never crash the run
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}", hint)


# -- sections -------------------------------------------------------------------


def _config_section(client: Client) -> Section:
    section = Section("Config")
    resolved = client.config.resolved()
    profile = client.config.profile
    section.results.append(
        CheckResult("profile", True, f"using profile {profile!r}" if profile else "no profile")
    )
    from eosdk.config.settings import URL_FIELDS

    for fieldname, value in resolved.items():
        if value.value is None:
            continue
        label = f"{fieldname} URL" if fieldname in URL_FIELDS else fieldname
        if (
            fieldname in URL_FIELDS
            and fieldname not in _VERSION_CHECK_EXEMPT
            and _VERSION_SEGMENT.search(value.value)
        ):
            section.results.append(
                CheckResult(
                    label,
                    False,
                    f"base URL ends in a version segment: {value.value}",
                    hint="bases must be version-free; the SDK appends /vN itself",
                )
            )
        else:
            section.results.append(CheckResult(label, True, f"{value.display} ({value.source})"))
    return section


def _auth_section(client: Client) -> Section:
    section = Section("Auth")
    try:
        # _endpoint (not config.require): resolves discovery-pending fields by
        # fetching the platform document on demand, like any service call would.
        client._endpoint("keycloak", service="keycloak")
    except EosdkError as exc:
        section.results.append(_skip("OIDC discovery", str(exc)))
        return section

    def check_oidc() -> str:
        endpoints = client.auth._endpoints()
        return f"token endpoint at {endpoints.token_endpoint}"

    section.results.append(
        _probe("OIDC discovery", check_oidc, hint="check keycloak URL and realm")
    )
    status = client.auth.status()
    if status.logged_in:
        section.results.append(CheckResult("session", True, f"logged in ({status.profile})"))
    else:
        section.results.append(_skip("session", "not logged in — run `eo auth login`"))
    return section


def _services_section(client: Client, *, force_ready: bool = False) -> Section:
    from eosdk.readiness import ReadinessProbe

    section = Section("Services")
    readiness = ReadinessProbe(client._readiness_cache_dir)

    def logged_in() -> bool:
        # Cache-only, no network. If auth isn't even configured there can be no
        # session, so treat that as not-logged-in too.
        try:
            client.config.require("keycloak", service="keycloak")
            return client.auth.status().logged_in
        except EosdkError:
            return False

    def eodata_ready(fieldname: str) -> Callable[[], str]:
        def check() -> str:
            base = client.config.require(fieldname, service=fieldname)
            status = readiness.check(
                client._transport,
                base,
                service=fieldname,
                timeout=PROBE_TIMEOUT,
                force=force_ready,
            )
            detail = status.detail
            if status.cached:
                wait = readiness.interval - (status.age or 0.0)
                detail += f" [cached {status.age:.0f}s ago; next live probe in {wait:.0f}s]"
            if not status.ready:
                raise EosdkError(detail)
            return detail

        return check

    def stac() -> str:
        capabilities = client._stac_catalogue().capabilities()
        return f"item-search at {capabilities.search_url}"

    def odata() -> str:
        # CDSE serves no service document at odata/v1 (it 404s), so probe the
        # Products resource itself with a minimal query.
        base = client.config.require("catalogue_odata", service="catalogue_odata")
        response = client._transport.request(
            "GET",
            route(base, "odata/v1/Products"),
            service="catalogue_odata",
            params={"$top": "1"},
            timeout=PROBE_TIMEOUT,
        )
        if not response.is_success:
            raise EosdkError(f"Products endpoint returned HTTP {response.status_code}")
        return f"Products endpoint HTTP {response.status_code}"

    def s3_credentials() -> str:
        try:
            client._s3_downloader()._s3().list_buckets()
        except AuthError as exc:
            # Minting a managed key needs a valid session. Point at auth, not the
            # endpoint — the endpoint hint would misdirect. Covers the credentials
            # service rejecting the request (401/403) and _BearerAuth refusing an
            # anonymous call before any status is known (status_code is None); a
            # genuine server error (e.g. 500) is not an auth problem, so re-raise.
            if exc.status_code in (None, 401, 403):
                where = f" (HTTP {exc.status_code})" if exc.status_code else ""
                raise _ProbeFailure(
                    f"the S3 credentials service rejected the request{where}",
                    hint=(
                        "not authenticated — run `eo auth login` (minting S3 keys needs a session)"
                    ),
                ) from exc
            raise
        except Exception as exc:
            code = getattr(exc, "response", None)
            code = code.get("Error", {}).get("Code", "") if isinstance(code, dict) else ""
            if code in _S3_CREDENTIAL_ERRORS:
                raise _ProbeFailure(
                    f"S3 rejected the SDK's managed key ({code})",
                    hint=(
                        "this is a credentials problem, not an endpoint problem — it often "
                        "means the account holds too many keys in the credentials service; "
                        "revoke unused ones (`eo keys list`, `eo keys revoke <access-id>`) "
                        "and retry"
                    ),
                ) from exc
            raise
        return "S3 endpoint reachable with managed keys"

    ready_hint = (
        "the eodata store behind this service reports itself unavailable — "
        "this is service-side, not a config problem; probes are rate-limited, retry later"
    )
    for fieldname, name, probe, hint, needs_session in (
        ("catalogue_stac", "Catalogue (STAC)", stac, None, False),
        ("catalogue_odata", "Catalogue (OData)", odata, None, False),
        ("eodata_http", "EOData (HTTP)", eodata_ready("eodata_http"), ready_hint, False),
        ("s3_endpoint", "S3 credentials", s3_credentials, None, True),
        ("s3_endpoint", "EOData (S3)", eodata_ready("s3_endpoint"), ready_hint, False),
    ):
        try:
            # _endpoint triggers lazy discovery for <pending> fields; a field
            # that stays unresolved (or an unreachable platform document —
            # already reported as a failure in the Discovery section) skips
            # the probe rather than crashing the run.
            client._endpoint(fieldname, service=fieldname)
        except EosdkError as exc:
            section.results.append(_skip(name, str(exc)))
            continue
        # Minting a managed key needs a session; without one the probe can only
        # fail, so skip rather than raise a false failure (SPEC §6.8).
        if needs_session and not logged_in():
            section.results.append(_skip(name, "not logged in — run `eo auth login`"))
            continue
        section.results.append(
            _probe(name, probe, hint=hint or f"check the {fieldname} endpoint in your profile")
        )
    return section


def _discovery_section(client: Client) -> Section:
    section = Section("Discovery")
    if not client.discovery.configured:
        section.results.append(
            _skip("platform document", "no platform root configured (manual endpoints)")
        )
        return section

    def fetch() -> str:
        document = client.discovery.document()
        count = len(document.services)
        return f"schema {document.version}, {count} services at {client.discovery.url}"

    section.results.append(
        _probe("platform document", fetch, hint="check platform / EOSDK_DISCOVERY_URL")
    )
    try:
        from eosdk.versions import SUPPORTED_VERSIONS, select_version

        for service_key in SUPPORTED_VERSIONS:
            advertised = client.discovery.api_version_for(service_key)
            if advertised is None:
                continue
            select_version(service_key, advertised)
            section.results.append(
                CheckResult(f"{service_key} api", True, f"advertised {advertised} is supported")
            )
    except UnsupportedApiVersion as exc:
        section.results.append(
            CheckResult("api versions", False, str(exc), hint="upgrade eosdk or pin the URL")
        )
    except EosdkError:  # document unreachable: already reported above
        pass
    return section


def run_doctor(client: Client, *, force_ready: bool = False) -> list[Section]:
    return [
        _config_section(client),
        _discovery_section(client),
        _auth_section(client),
        _services_section(client, force_ready=force_ready),
    ]
