"""The env-var mapping for endpoint fields (single source of truth).

There are no built-in endpoint defaults: a deployment is configured locally
(kwargs, ``EOSDK_*`` env vars, ``eosdk.toml``, user profiles) or bootstrapped
from a platform's discovery document at
``{platform}/.well-known/eo-services.json`` (SPEC §6.2).
"""

from __future__ import annotations

# Field name -> environment variable (SPEC §6.1 step 2). Non-endpoint controls
# (EOSDK_PROFILE, EOSDK_PLATFORM, EOSDK_TLS_VERIFY) are handled by the loader.
ENV_VAR_MAP: dict[str, str] = {
    "catalogue_stac": "EOSDK_CATALOGUE_STAC_URL",
    "catalogue_odata": "EOSDK_CATALOGUE_ODATA_URL",
    "eodata_http": "EOSDK_EODATA_HTTP_URL",
    "s3_endpoint": "EOSDK_S3_ENDPOINT",
    "s3_region": "EOSDK_S3_REGION",
    "s3_credentials": "EOSDK_S3_CREDENTIALS_URL",
    "keycloak": "EOSDK_KEYCLOAK_URL",
    "keycloak_realm": "EOSDK_KEYCLOAK_REALM",
    "keycloak_client_id": "EOSDK_KEYCLOAK_CLIENT_ID",
    "discovery_url": "EOSDK_DISCOVERY_URL",
}

ENV_PROFILE = "EOSDK_PROFILE"
ENV_PLATFORM = "EOSDK_PLATFORM"
ENV_TLS_VERIFY = "EOSDK_TLS_VERIFY"
