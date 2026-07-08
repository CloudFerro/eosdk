"""Configuration resolution chain (SPEC §6.1).

Per-field precedence, most specific wins:

1. explicit kwargs to ``Client(...)``
2. environment variables ``EOSDK_*``
3. project-local ``./eosdk.toml``
4. user config ``~/.config/eosdk/config.toml`` (selected profile)
5. remote discovery document (stubbed until Phase 3 — fields resolve to
   ``<pending>`` when a ``platform`` root is configured)
6. built-in defaults

The ``platform`` root itself is resolved from local layers only (steps 1-4/6),
never from discovery — this breaks the config→discovery cycle (SPEC §4.2).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from eosdk.config.defaults import (
    BUILTIN_DEFAULTS,
    ENV_PLATFORM,
    ENV_PROFILE,
    ENV_TLS_VERIFY,
    ENV_VAR_MAP,
)
from eosdk.config.settings import (
    URL_ADAPTER,
    URL_FIELDS,
    ConfigFile,
    Endpoints,
    Profile,
    ResolvedConfig,
    ResolvedValue,
)
from eosdk.exceptions import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class DiscoveryHook(Protocol):
    """Seam for SPEC §6.1 precedence step 5; the real resolver lands in Phase 3."""

    def resolve(self, platform: str) -> dict[str, str]:
        """Return field -> endpoint URL for services the platform advertises."""
        ...


class NullDiscovery:
    """Phase 0-2 stand-in: no remote discovery; pending fields stay pending."""

    def resolve(self, platform: str) -> dict[str, str]:
        return {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed TOML in {path}: {exc}") from exc


def _load_config_file(path: Path) -> ConfigFile:
    try:
        return ConfigFile.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(f"invalid config file {path}: {exc}") from exc


def user_config_path() -> Path:
    """``~/.config/eosdk/config.toml``, honoring ``$XDG_CONFIG_HOME``."""
    import os

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "eosdk" / "config.toml"


def _validate_url(fieldname: str, value: str, *, source: str) -> str:
    if fieldname not in URL_FIELDS and fieldname != "platform":
        return value
    try:
        return URL_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ConfigError(f"invalid URL for {fieldname} (from {source}): {value!r}") from exc


def _select_profile(
    profile_kwarg: str | None,
    env: Mapping[str, str],
    local_file: ConfigFile | None,
    user_file: ConfigFile | None,
) -> str | None:
    if profile_kwarg is not None:
        return profile_kwarg
    if ENV_PROFILE in env:
        return env[ENV_PROFILE]
    if local_file is not None and local_file.default_profile is not None:
        return local_file.default_profile
    if user_file is not None and user_file.default_profile is not None:
        return user_file.default_profile
    return None


def _profile_from(
    config: ConfigFile | None, name: str | None, path: Path | None
) -> tuple[Profile | None, str]:
    if config is None or name is None:
        return None, ""
    profile = config.profiles.get(name)
    label = f"profile:{name}({path})"
    return profile, label


def load(
    *,
    kwargs_endpoints: Mapping[str, str] | None = None,
    platform: str | None = None,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    user_config: Path | None = None,
    discovery: DiscoveryHook | None = None,
) -> ResolvedConfig:
    """Resolve endpoints per field through the six-layer precedence chain."""
    import os

    env = os.environ if env is None else env
    kwargs_endpoints = dict(kwargs_endpoints or {})
    discovery = discovery if discovery is not None else NullDiscovery()

    unknown = set(kwargs_endpoints) - set(Endpoints.model_fields)
    if unknown:
        raise ConfigError(f"unknown endpoint kwargs: {sorted(unknown)}")

    local_path = (cwd or Path.cwd()) / "eosdk.toml"
    local_file = _load_config_file(local_path) if local_path.is_file() else None
    user_path = user_config or user_config_path()
    user_file = _load_config_file(user_path) if user_path.is_file() else None

    profile_name = _select_profile(profile, env, local_file, user_file)
    local_profile, local_label = _profile_from(local_file, profile_name, local_path)
    user_profile, user_label = _profile_from(user_file, profile_name, user_path)
    if profile_name is not None and local_profile is None and user_profile is None:
        known = sorted(
            set(local_file.profiles if local_file else {})
            | set(user_file.profiles if user_file else {})
        )
        raise ConfigError(
            f"unknown profile {profile_name!r}",
            hint=f"available profiles: {known or 'none'}",
        )

    def local_layers(fieldname: str) -> ResolvedValue | None:
        """Layers 1-4 for one field; returns None when locally unresolved."""
        if fieldname in kwargs_endpoints:
            return ResolvedValue(
                _validate_url(fieldname, kwargs_endpoints[fieldname], source="kwargs"),
                "kwargs",
            )
        env_var = ENV_VAR_MAP.get(fieldname) if fieldname != "platform" else ENV_PLATFORM
        if env_var and env_var in env:
            return ResolvedValue(
                _validate_url(fieldname, env[env_var], source=env_var), f"env:{env_var}"
            )
        for prof, label in ((local_profile, local_label), (user_profile, user_label)):
            if prof is not None:
                value = getattr(prof, fieldname, None)
                if value is not None:
                    return ResolvedValue(_validate_url(fieldname, value, source=label), label)
        return None

    # The platform root: local layers only (never discovery), no built-in default.
    platform_resolved = (
        ResolvedValue(_validate_url("platform", platform, source="kwargs"), "kwargs")
        if platform is not None
        else local_layers("platform")
    )
    platform_value = platform_resolved.value if platform_resolved else None

    discovered = discovery.resolve(platform_value) if platform_value else {}

    sources: dict[str, ResolvedValue] = {}
    values: dict[str, Any] = {}
    for fieldname in Endpoints.model_fields:
        resolved = local_layers(fieldname)
        if resolved is None and platform_value is not None:
            if fieldname in discovered:
                resolved = ResolvedValue(
                    _validate_url(fieldname, discovered[fieldname], source="discovery"),
                    "discovery",
                )
            else:
                resolved = ResolvedValue(None, "discovery")
        if resolved is None:
            default = getattr(BUILTIN_DEFAULTS, fieldname)
            resolved = ResolvedValue(default, "default" if default is not None else "unset")
        sources[fieldname] = resolved
        if resolved.value is not None:
            values[fieldname] = resolved.value

    verify_tls = env.get(ENV_TLS_VERIFY, "1").strip().lower() not in {"0", "false", "no"}

    return ResolvedConfig(
        endpoints=Endpoints.model_validate(values),
        sources=sources,
        profile=profile_name,
        platform=platform_value,
        verify_tls=verify_tls,
    )
