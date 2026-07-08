"""API version guard (SPEC §6.3).

Each version-aware module pins the versions it implements; the guard compares
them with what discovery advertises and fails early — at first use, never
mid-download — with an actionable message. An absent advertisement selects the
module's default (its newest implemented version).
"""

from __future__ import annotations

from eosdk.exceptions import UnsupportedApiVersion

# service key (matching discovery api_versions keys) -> (supported, default)
SUPPORTED_VERSIONS: dict[str, tuple[frozenset[str], str]] = {
    "zipper/odata": (frozenset({"v1"}), "v1"),
    "catalogue/odata": (frozenset({"v1"}), "v1"),
    "keys_manager": (frozenset({"v1"}), "v1"),
}

# discovery key -> the env var that pins the endpoint manually
_PIN_HINTS = {
    "zipper/odata": "EOSDK_ZIPPER_URL",
    "catalogue/odata": "EOSDK_CATALOGUE_ODATA_URL",
    "keys_manager": "EOSDK_KEYS_MANAGER_URL",
}


def select_version(service_key: str, advertised: str | None) -> str:
    """Pick the route-template version for a service (SPEC §6.3).

    ``advertised=None`` (no discovery, or the field omitted) falls back to the
    module's default supported version — also the pre-discovery path.
    """
    supported, default = SUPPORTED_VERSIONS[service_key]
    if advertised is None:
        return default
    if advertised in supported:
        return advertised
    versions = "-".join(sorted(supported))
    pin = _PIN_HINTS.get(service_key, "the service URL")
    raise UnsupportedApiVersion(
        service=service_key,
        advertised=advertised,
        supported=versions,
        remediation=f"upgrade eosdk or pin {pin}",
    )
