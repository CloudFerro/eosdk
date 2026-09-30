"""Typed, layered configuration with named profiles.

Profile management mirrors the ``eo config`` commands: :func:`list_profiles`
and :func:`get_default_profile` read, while :func:`init_profile`,
:func:`set_value`, :func:`set_default_profile`, and :func:`save_discovered_profile`
write (comment-preserving, atomic). :func:`user_config_path` gives the default
config file location so a library caller can drive them without the CLI.
"""

from eosdk.config.loader import DiscoveryHook, NullDiscovery, load, user_config_path
from eosdk.config.profiles import (
    get_default_profile,
    init_profile,
    list_profiles,
    save_discovered_profile,
    set_default_profile,
    set_value,
)
from eosdk.config.settings import (
    ConfigFile,
    Endpoints,
    Profile,
    ResolvedConfig,
    ResolvedValue,
)

__all__ = [
    "ConfigFile",
    "DiscoveryHook",
    "Endpoints",
    "NullDiscovery",
    "Profile",
    "ResolvedConfig",
    "ResolvedValue",
    "get_default_profile",
    "init_profile",
    "list_profiles",
    "load",
    "save_discovered_profile",
    "set_default_profile",
    "set_value",
    "user_config_path",
]
