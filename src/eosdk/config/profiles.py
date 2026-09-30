"""Profile CRUD for the config file.

Writes go through tomlkit so comments and layout round-trip, are
validated *before* touching the file, and land atomically (tmp + rename).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import tomlkit
from pydantic import ValidationError

from eosdk.config.loader import _load_config_file
from eosdk.config.settings import URL_ADAPTER, URL_FIELDS, Profile
from eosdk.exceptions import ConfigError


def list_profiles(path: Path) -> dict[str, Profile]:
    if not path.is_file():
        return {}
    return _load_config_file(path).profiles


def get_default_profile(path: Path) -> str | None:
    if not path.is_file():
        return None
    return _load_config_file(path).default_profile


def _read_document(path: Path) -> tomlkit.TOMLDocument:
    if path.is_file():
        return tomlkit.parse(path.read_text())
    return tomlkit.document()


def _write_document(path: Path, document: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(tomlkit.dumps(document))
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink()
        raise


def _validate_field(fieldname: str, value: str) -> str:
    if fieldname not in Profile.model_fields:
        known = sorted(Profile.model_fields)
        raise ConfigError(f"unknown profile key {fieldname!r}", hint=f"known keys: {known}")
    if fieldname in URL_FIELDS or fieldname == "platform":
        try:
            return URL_ADAPTER.validate_python(value)
        except ValidationError as exc:
            raise ConfigError(f"invalid URL for {fieldname}: {value!r}") from exc
    return value


def set_value(path: Path, dotted_key: str, value: str) -> None:
    """Set ``profiles.<name>.<field>`` or ``default_profile``.

    Paths are deliberately constrained to those two shapes (plan risk R10).
    """
    parts = dotted_key.split(".")
    if parts == ["default_profile"]:
        set_default_profile(path, value)
        return
    if len(parts) != 3 or parts[0] != "profiles":
        raise ConfigError(
            f"unsupported config key {dotted_key!r}",
            hint="use profiles.<name>.<field> or default_profile",
        )
    _, profile_name, fieldname = parts
    validated = _validate_field(fieldname, value)
    document = _read_document(path)
    profiles = document.setdefault("profiles", tomlkit.table(is_super_table=True))
    profile = profiles.setdefault(profile_name, tomlkit.table())
    profile[fieldname] = validated
    _write_document(path, document)


def set_default_profile(path: Path, name: str) -> None:
    document = _read_document(path)
    profiles = document.get("profiles", {})
    if name not in profiles:
        raise ConfigError(
            f"unknown profile {name!r}", hint=f"available profiles: {sorted(profiles)}"
        )
    document["default_profile"] = name
    _write_document(path, document)


def save_discovered_profile(
    path: Path,
    name: str,
    *,
    values: dict[str, str],
    discovered_from: str,
) -> str:
    """Persist a discovery snapshot as ``profiles.<name>``.

    Returns ``"created"``, ``"updated"``, ``"unchanged"`` or ``"conflict"``.
    A same-named profile carrying ``discovered_from`` is a managed mirror and
    is resynced wholesale; one without the marker is user-owned and reported
    as a conflict instead of being overwritten.
    """
    payload = dict(values)
    payload["discovered_from"] = discovered_from
    validated = {field: _validate_field(field, value) for field, value in payload.items()}
    document = _read_document(path)
    profiles = document.setdefault("profiles", tomlkit.table(is_super_table=True))
    existing = profiles.get(name)
    if existing is not None and "discovered_from" not in existing:
        return "conflict"
    if existing is not None and {key: existing[key] for key in existing} == validated:
        return "unchanged"
    table = tomlkit.table()
    for field, value in validated.items():
        table[field] = value
    profiles[name] = table
    if "default_profile" not in document:
        document["default_profile"] = name
    _write_document(path, document)
    return "created" if existing is None else "updated"


def init_profile(
    path: Path,
    profile_name: str,
    values: dict[str, str],
    *,
    force: bool = False,
    make_default: bool | None = None,
) -> None:
    """Create a profile; refuses to clobber an existing one without ``force``."""
    validated = {field: _validate_field(field, value) for field, value in values.items()}
    document = _read_document(path)
    profiles = document.setdefault("profiles", tomlkit.table(is_super_table=True))
    if profile_name in profiles and not force:
        raise ConfigError(
            f"profile {profile_name!r} already exists", hint="pass --force to overwrite"
        )
    table = tomlkit.table()
    for field, value in validated.items():
        table[field] = value
    profiles[profile_name] = table
    if make_default or (make_default is None and "default_profile" not in document):
        document["default_profile"] = profile_name
    _write_document(path, document)
