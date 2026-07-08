"""Health checks (SPEC §6.8): config, auth, and service probes.

Checks degrade gracefully: a subsystem that is not configured (or not shipped
yet — Discovery before Phase 3) reports *skipped* with a reason, never a false
failure. ``ok`` is ``True``/``False``/``None`` (skipped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eosdk.exceptions import ConfigError, EosdkError
from eosdk.transport import route

if TYPE_CHECKING:
    from collections.abc import Callable

    from eosdk.client import Client

PROBE_TIMEOUT = 5.0  # doctor must never hang
_VERSION_SEGMENT = re.compile(r"/v\d+/?$")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool | None  # None -> skipped
    detail: str
    hint: str | None = None


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
        return CheckResult(name, False, str(exc), hint)
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
        if fieldname in URL_FIELDS and _VERSION_SEGMENT.search(value.value):
            section.results.append(
                CheckResult(
                    label,
                    False,
                    f"base URL ends in a version segment: {value.value}",
                    hint="bases must be version-free; the SDK appends /vN itself (SPEC §6.3)",
                )
            )
        else:
            section.results.append(CheckResult(label, True, f"{value.display} ({value.source})"))
    return section


def _auth_section(client: Client) -> Section:
    section = Section("Auth")
    try:
        client.config.require("keycloak", service="keycloak")
    except ConfigError:
        section.results.append(_skip("OIDC discovery", "keycloak endpoint not configured"))
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


def _services_section(client: Client) -> Section:
    section = Section("Services")

    def stac() -> str:
        capabilities = client._stac_catalogue().capabilities()
        return f"item-search at {capabilities.search_url}"

    def odata() -> str:
        base = client.config.require("catalogue_odata", service="catalogue_odata")
        response = client._transport.request(
            "GET", route(base, "odata/v1"), service="catalogue_odata", timeout=PROBE_TIMEOUT
        )
        return f"service document HTTP {response.status_code}"

    def zipper() -> str:
        base = client.config.require("zipper", service="zipper")
        response = client._transport.request("HEAD", base, service="zipper", timeout=PROBE_TIMEOUT)
        return f"HEAD {base} -> HTTP {response.status_code}"

    def exos() -> str:
        client._exos_downloader()._s3().list_buckets()
        return "S3 endpoint reachable with managed keys"

    for fieldname, name, probe in (
        ("catalogue_stac", "STAC catalogue", stac),
        ("catalogue_odata", "OData catalogue", odata),
        ("zipper", "Zipper", zipper),
        ("exos_endpoint", "Exos (S3)", exos),
    ):
        try:
            client.config.require(fieldname, service=fieldname)
        except ConfigError as exc:
            section.results.append(_skip(name, str(exc)))
            continue
        section.results.append(
            _probe(name, probe, hint=f"check the {fieldname} endpoint in your profile")
        )
    return section


def _discovery_section(client: Client) -> Section:
    section = Section("Discovery")
    section.results.append(
        _skip("platform document", "remote discovery ships in a later release (Phase 3)")
    )
    return section


def run_doctor(client: Client) -> list[Section]:
    return [
        _config_section(client),
        _discovery_section(client),
        _auth_section(client),
        _services_section(client),
    ]
