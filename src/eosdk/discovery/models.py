"""Platform discovery document: schema, parsing, projection (SPEC §6.2).

The top-level ``version`` identifies the document *schema*; an unrecognized
major version rejects the whole document rather than partially parsing it.
Unknown service or strategy keys are ignored (forward compatibility).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eosdk.eodata.capabilities import BUILTIN_MATRIX, Strategy, effective_capabilities
from eosdk.exceptions import ConfigError

SUPPORTED_SCHEMA_MAJOR = 1

_PROFILE_UNSAFE = re.compile(r"[^a-z0-9._-]+")


class DiscoveryError(ConfigError):
    """The discovery document is unusable (bad schema version, malformed)."""


class PlatformInfo(BaseModel):
    """Top-level platform identity block (optional, SPEC §6.2).

    ``name`` doubles as the config-profile name the SDK snapshots the
    discovered configuration under; :attr:`profile_name` normalizes it.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None

    @property
    def profile_name(self) -> str | None:
        """The platform name as a profile-safe slug; None when nothing survives."""
        slug = _PROFILE_UNSAFE.sub("-", self.name.strip().lower()).strip("-._")
        return slug or None


class StrategyInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    api_version: str | None = None
    capabilities: list[str] | None = None
    deprecated: bool = False
    sunset: dt.date | None = None
    replacement: str | None = None


class DiscoveryDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str
    platform: PlatformInfo | None = None
    services: dict[str, dict[str, Any]] = Field(default_factory=dict)


def parse_document(raw: dict[str, Any]) -> DiscoveryDocument:
    try:
        document = DiscoveryDocument.model_validate(raw)
    except ValidationError as exc:
        raise DiscoveryError(f"malformed discovery document: {exc}") from exc
    major = document.version.split(".", 1)[0]
    if not major.isdigit() or int(major) != SUPPORTED_SCHEMA_MAJOR:
        raise DiscoveryError(
            f"unsupported discovery document schema version {document.version!r}; "
            f"this eosdk supports {SUPPORTED_SCHEMA_MAJOR}.x"
        )
    return document


def derive_discovery_url(platform: str, discovery_url: str | None = None) -> str:
    """SPEC §6.2 resolution: explicit ``discovery_url`` wins, else well-known."""
    if discovery_url:
        return discovery_url
    return f"{platform.rstrip('/')}/.well-known/eo-services.json"


def project_endpoints(document: DiscoveryDocument) -> dict[str, str]:
    """Project the nested document onto the flat ``Endpoints`` fields (SPEC §6.2).

    Multi-strategy services collapse onto their single base field; per-strategy
    URLs are consumed by :func:`strategy_sets` instead.
    """
    services = document.services
    projected: dict[str, str] = {}

    catalogue = services.get("catalogue", {})
    if isinstance(catalogue.get("stac"), dict) and catalogue["stac"].get("url"):
        projected["catalogue_stac"] = str(catalogue["stac"]["url"])
    if isinstance(catalogue.get("odata"), dict) and catalogue["odata"].get("url"):
        projected["catalogue_odata"] = str(catalogue["odata"]["url"])

    data_access = services.get("data_access", {})
    s3 = data_access.get("s3", {}) if isinstance(data_access, dict) else {}
    if isinstance(s3, dict):
        if s3.get("endpoint"):
            projected["s3_endpoint"] = str(s3["endpoint"])
        if s3.get("region"):
            projected["s3_region"] = str(s3["region"])
        credentials = s3.get("credentials", {})
        if isinstance(credentials, dict) and credentials.get("url"):
            projected["s3_credentials"] = str(credentials["url"])

    auth = services.get("auth", {})
    issuer = auth.get("issuer")
    if issuer:
        base, separator, realm = str(issuer).partition("/realms/")
        if not separator or not realm:
            raise DiscoveryError(
                f"auth issuer {issuer!r} has no '/realms/' segment; cannot derive realm"
            )
        projected["keycloak"] = base
        projected["keycloak_realm"] = realm.strip("/")
    if auth.get("client_id"):
        # Public-client id only — this document must never carry a secret.
        projected["keycloak_client_id"] = str(auth["client_id"])

    http = data_access.get("http", {}) if isinstance(data_access, dict) else {}
    strategies = _service_strategies(http) if isinstance(http, dict) else {}
    if strategies:
        # base field: highest-preference strategy's URL trimmed of its route
        # suffix is deployment-specific; store the first strategy's URL as base
        # only when a single strategy exists — otherwise per-strategy URLs rule.
        first = next(iter(strategies.values()))
        projected["eodata_http"] = first.url or ""
    return {k: v for k, v in projected.items() if v}


def _service_strategies(service: dict[str, Any]) -> dict[str, StrategyInfo]:
    strategies: dict[str, StrategyInfo] = {}
    for key, value in service.items():
        if isinstance(value, dict) and "url" in value:
            try:
                strategies[key] = StrategyInfo.model_validate(value)
            except ValidationError:
                continue  # unknown/invalid strategy entries are ignored
    return strategies


def strategy_sets(document: DiscoveryDocument) -> dict[str, tuple[Strategy, ...]]:
    """Merge discovery info onto the built-in matrix (SPEC §6.2/§6.3).

    Order stays the SDK's built-in preference. Discovery may mark strategies
    deprecated/sunset, carry per-strategy URLs and api_versions, and *restrict*
    capabilities (intersection gate) — never extend them. Strategies the SDK
    does not implement are ignored; built-in strategies absent from the
    document keep their defaults when the document does not describe the
    service at all, and become unavailable when it describes the service but
    omits them.
    """
    result: dict[str, tuple[Strategy, ...]] = {}
    data_access = document.services.get("data_access")
    service_map = {
        "http": data_access.get("http") if isinstance(data_access, dict) else None,
    }

    for backend, builtin_strategies in BUILTIN_MATRIX.items():
        service = service_map.get(backend)
        if not isinstance(service, dict):
            result[backend] = builtin_strategies  # document says nothing: built-ins
            continue
        advertised = _service_strategies(service)
        merged: list[Strategy] = []
        for builtin in builtin_strategies:
            info = advertised.get(builtin.name)
            if info is None:
                merged.append(
                    Strategy(
                        builtin.name,
                        builtin.capabilities,
                        available=False,  # service described, strategy not offered
                    )
                )
                continue
            merged.append(
                Strategy(
                    builtin.name,
                    effective_capabilities(builtin.capabilities, info.capabilities),
                    deprecated=info.deprecated,
                    sunset=info.sunset,
                    replacement=info.replacement,
                    url=info.url,
                )
            )
        result[backend] = tuple(merged)
    return result


def api_versions(document: DiscoveryDocument) -> dict[str, str]:
    """Slash-joined document path -> advertised api_version, where present.

    Keys mirror the document's nesting, e.g. ``catalogue/odata``,
    ``data_access/http/odata``, ``data_access/s3/credentials``.
    """
    versions: dict[str, str] = {}

    def walk(prefix: str, mapping: dict[str, Any]) -> None:
        for key, value in mapping.items():
            if not isinstance(value, dict):
                continue
            path = f"{prefix}/{key}" if prefix else key
            if isinstance(value.get("api_version"), str):
                versions[path] = value["api_version"]
            walk(path, value)

    walk("", document.services)
    return versions
