"""Endpoint and profile models plus the resolved-config introspection surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter

from eosdk.exceptions import ConfigError

PENDING = "<pending>"


def _url_str(value: HttpUrl) -> str:
    return str(value).rstrip("/")


# Endpoint URLs are stored as plain strings after HttpUrl syntax validation, so
# downstream code (route(), httpx) never deals with pydantic URL objects.
Url = Annotated[HttpUrl, AfterValidator(_url_str)]

URL_ADAPTER: TypeAdapter[str] = TypeAdapter(Url)


class Endpoints(BaseModel):
    """Per-deployment service endpoints.

    All fields optional here: required-ness is enforced at first use of the
    owning service via :meth:`ResolvedConfig.require`, because some fields may
    legitimately stay unresolved (discovery-pending) at construction time.

    ``keycloak_client_id`` is an addition over the original endpoint model:
    auth cannot work without it.
    """

    model_config = ConfigDict(extra="forbid")

    catalogue_stac: str | None = None
    catalogue_odata: str | None = None
    eodata_http: str | None = None
    s3_endpoint: str | None = None
    s3_region: str = "default"
    s3_credentials: str | None = None
    keycloak: str | None = None
    keycloak_realm: str | None = None
    keycloak_client_id: str | None = None
    discovery_url: str | None = None


URL_FIELDS = frozenset(
    {
        "catalogue_stac",
        "catalogue_odata",
        "eodata_http",
        "s3_endpoint",
        "s3_credentials",
        "keycloak",
        "discovery_url",
    }
)


class Profile(BaseModel):
    """A named endpoint set in the config file; any subset may be pinned.

    ``discovered_from`` marks a profile the SDK materialized from a platform
    discovery document: it holds the discovery URL, and its
    presence is what allows the SDK to resync the profile on re-fetch.
    Profiles without it are user-owned and never touched.
    """

    model_config = ConfigDict(extra="forbid")

    platform: str | None = None
    description: str | None = None
    discovered_from: str | None = None
    catalogue_stac: str | None = None
    catalogue_odata: str | None = None
    eodata_http: str | None = None
    s3_endpoint: str | None = None
    s3_region: str | None = None
    s3_credentials: str | None = None
    keycloak: str | None = None
    keycloak_realm: str | None = None
    keycloak_client_id: str | None = None
    discovery_url: str | None = None


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str | None = None
    profiles: dict[str, Profile] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedValue:
    """An endpoint value together with the source layer that provided it."""

    value: str | None
    # "kwargs" | "env:EOSDK_X" | "profile:name(path)" | "discovery" | "default" | "unset"
    source: str

    @property
    def display(self) -> str:
        if self.value is not None:
            return self.value
        if self.source == "discovery":
            return PENDING
        return "<unset>"


@dataclass
class ResolvedConfig:
    """Output of the resolution chain; what ``client.config`` exposes."""

    endpoints: Endpoints
    sources: dict[str, ResolvedValue] = field(default_factory=dict)
    profile: str | None = None
    platform: str | None = None
    verify_tls: bool = True

    def resolved(self) -> dict[str, ResolvedValue]:
        """Endpoint -> (value, source) mapping, for config introspection."""
        return dict(self.sources)

    def apply_discovered(self, projected: dict[str, str]) -> None:
        """Fill ``<pending>`` fields from a fetched discovery document.

        Only fields whose source is ``discovery`` and value is still unset are
        touched — locally pinned values are final and never re-consulted
        (per-service precedence).
        """
        for fieldname, value in projected.items():
            current = self.sources.get(fieldname)
            if current is None or current.source != "discovery" or current.value is not None:
                continue
            validated = URL_ADAPTER.validate_python(value) if fieldname in URL_FIELDS else value
            self.sources[fieldname] = ResolvedValue(validated, "discovery")
            setattr(self.endpoints, fieldname, validated)

    def require(self, fieldname: str, *, service: str) -> str:
        """Return the endpoint for ``service`` or raise with the exact fix knob."""
        resolved = self.sources.get(fieldname)
        value = getattr(self.endpoints, fieldname, None)
        if value is not None:
            return str(value)
        from eosdk.config.defaults import ENV_VAR_MAP  # local import: defaults imports settings

        env_var = ENV_VAR_MAP.get(fieldname, f"EOSDK_{fieldname.upper()}")
        knob = f"pin {env_var} or profiles.<name>.{fieldname}"
        if resolved is not None and resolved.source == "discovery":
            raise ConfigError(
                f"{service} endpoint ({fieldname}) is discovery-sourced and remote discovery "
                f"is not available yet",
                hint=knob,
            )
        raise ConfigError(f"{service} endpoint ({fieldname}) is not configured", hint=knob)
