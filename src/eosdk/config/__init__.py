"""Typed, layered configuration with named profiles (SPEC §6.1)."""

from eosdk.config.loader import DiscoveryHook, NullDiscovery, load
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
    "load",
]
