"""Endpoint and profile models plus the resolved-config introspection surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, HttpUrl, TypeAdapter

from eosdk.exceptions import ConfigError

PENDING = "<pending>"


def _url_str(value: HttpUrl) -> str:
    return str(value).rstrip("/")


# Endpoint URLs are stored as plain strings after HttpUrl syntax validation, so
# downstream code (route(), httpx) never deals with pydantic URL objects.
Url = Annotated[HttpUrl, AfterValidator(_url_str)]

URL_ADAPTER: TypeAdapter[str] = TypeAdapter(Url)


class Endpoints(BaseModel):
    """Per-deployment service endpoints (SPEC §6.1).

    All fields optional here: required-ness is enforced at first use of the
    owning service via :meth:`ResolvedConfig.require`, because some fields may
    legitimately stay unresolved (discovery-pending) at construction time.

    ``keycloak_client_id`` is an addition over the SPEC §6.1 model: auth cannot
    work without it (SPEC §14.6 is unresolved; default is a placeholder).
    """

    model_config = ConfigDict(extra="forbid")

    catalogue_stac: str | None = None
    catalogue_odata: str | None = None
    zipper: str | None = None
    exos_endpoint: str | None = None
    exos_region: str = "default"
    keys_manager: str | None = None
    keycloak: str | None = None
    keycloak_realm: str = "eodata"
    keycloak_client_id: str = "eosdk"
    discovery_url: str | None = None


URL_FIELDS = frozenset(
    {
        "catalogue_stac",
        "catalogue_odata",
        "zipper",
        "exos_endpoint",
        "keys_manager",
        "keycloak",
        "discovery_url",
    }
)


class Profile(BaseModel):
    """A named endpoint set in the config file; any subset may be pinned."""

    model_config = ConfigDict(extra="forbid")

    platform: str | None = None
    catalogue_stac: str | None = None
    catalogue_odata: str | None = None
    zipper: str | None = None
    exos_endpoint: str | None = None
    exos_region: str | None = None
    keys_manager: str | None = None
    keycloak: str | None = None
    keycloak_realm: str | None = None
    keycloak_client_id: str | None = None
    discovery_url: str | None = None


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str | None = None
    profiles: dict[str, Profile] = {}


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
        """Endpoint -> (value, source) mapping (SPEC §6.1 introspection)."""
        return dict(self.sources)

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
