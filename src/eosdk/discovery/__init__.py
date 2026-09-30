"""Service discovery.

Phase 1 ships only the OIDC slice; the platform document, caching, and
STAC/OData probes land in Phase 3.
"""

from eosdk.discovery.oidc import OidcEndpoints, fetch_oidc_endpoints

__all__ = ["OidcEndpoints", "fetch_oidc_endpoints"]
