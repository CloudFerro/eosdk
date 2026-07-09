"""Platform discovery document: schema, parsing, projection (SPEC §6.2).

The top-level ``version`` identifies the document *schema*; an unrecognized
major version rejects the whole document rather than partially parsing it.
Unknown service or strategy keys are ignored (forward compatibility).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from eosdk.eodata.capabilities import BUILTIN_MATRIX, Strategy, effective_capabilities
from eosdk.exceptions import ConfigError

SUPPORTED_SCHEMA_MAJOR = 1


class DiscoveryError(ConfigError):
    """The discovery document is unusable (bad schema version, malformed)."""


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
    services: dict[str, dict[str, Any]] = {}


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

    exos = services.get("exos", {})
    if exos.get("endpoint"):
        projected["exos_endpoint"] = str(exos["endpoint"])
    if exos.get("region"):
        projected["exos_region"] = str(exos["region"])

    keys_manager = services.get("keys_manager", {})
    if keys_manager.get("url"):
        projected["keys_manager"] = str(keys_manager["url"])

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

    zipper = services.get("zipper", {})
    strategies = _service_strategies(zipper)
    if strategies:
        # base field: highest-preference strategy's URL trimmed of its route
        # suffix is deployment-specific; store the first strategy's URL as base
        # only when a single strategy exists — otherwise per-strategy URLs rule.
        first = next(iter(strategies.values()))
        projected["zipper"] = first.url or ""
        projected = {k: v for k, v in projected.items() if v}
    return projected


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
    service_map = {"zipper": document.services.get("zipper")}

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
    """(service or service/strategy) -> advertised api_version, where present."""
    versions: dict[str, str] = {}
    for service_name, service in document.services.items():
        if not isinstance(service, dict):
            continue
        if isinstance(service.get("api_version"), str):
            versions[service_name] = service["api_version"]
        for strategy_name, value in service.items():
            if isinstance(value, dict) and isinstance(value.get("api_version"), str):
                versions[f"{service_name}/{strategy_name}"] = value["api_version"]
    return versions
