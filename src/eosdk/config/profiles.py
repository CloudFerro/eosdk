"""Profile read helpers; the tomlkit write side lands with ``eo config`` (Phase 1.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eosdk.config.loader import _load_config_file

if TYPE_CHECKING:
    from pathlib import Path

    from eosdk.config.settings import Profile


def list_profiles(path: Path) -> dict[str, Profile]:
    if not path.is_file():
        return {}
    return _load_config_file(path).profiles


def get_default_profile(path: Path) -> str | None:
    if not path.is_file():
        return None
    return _load_config_file(path).default_profile
