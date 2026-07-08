"""Keycloak OIDC endpoint resolution (SPEC §6.2, per-service discovery).

Config holds only the Keycloak base URL and realm; everything else comes from
the realm's ``openid-configuration`` document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eosdk.exceptions import AuthError
from eosdk.transport import route

if TYPE_CHECKING:
    from eosdk.transport import Transport


@dataclass(frozen=True)
class OidcEndpoints:
    issuer: str
    token_endpoint: str
    device_authorization_endpoint: str | None


def fetch_oidc_endpoints(transport: Transport, keycloak_base: str, realm: str) -> OidcEndpoints:
    url = route(keycloak_base, "realms/{realm}/.well-known/openid-configuration", realm=realm)
    response = transport.request("GET", url, service="keycloak")
    if response.status_code != 200:
        raise AuthError(
            f"OIDC discovery failed with HTTP {response.status_code} at {url}", realm=realm
        )
    document = response.json()
    token_endpoint = document.get("token_endpoint")
    if not token_endpoint:
        raise AuthError(f"OIDC discovery document at {url} has no token_endpoint", realm=realm)
    return OidcEndpoints(
        issuer=document.get("issuer", ""),
        token_endpoint=token_endpoint,
        device_authorization_endpoint=document.get("device_authorization_endpoint"),
    )
