"""Built-in defaults and the env-var mapping (single source of truth).

``BUILTIN_DEFAULTS`` holds endpoints for the primary known deployment.
TODO(deployment): fill with the real platform endpoints before the first
staging run (SPEC §6.1 step 6; plan risk R9 — needs product input).
"""

from __future__ import annotations

from eosdk.config.settings import Endpoints

# Field name -> environment variable (SPEC §6.1 step 2). Non-endpoint controls
# (EOSDK_PROFILE, EOSDK_PLATFORM, EOSDK_TLS_VERIFY) are handled by the loader.
ENV_VAR_MAP: dict[str, str] = {
    "catalogue_stac": "EOSDK_CATALOGUE_STAC_URL",
    "catalogue_odata": "EOSDK_CATALOGUE_ODATA_URL",
    "zipper": "EOSDK_ZIPPER_URL",
    "exos_endpoint": "EOSDK_EXOS_ENDPOINT",
    "exos_region": "EOSDK_EXOS_REGION",
    "keys_manager": "EOSDK_KEYS_MANAGER_URL",
    "keycloak": "EOSDK_KEYCLOAK_URL",
    "keycloak_realm": "EOSDK_KEYCLOAK_REALM",
    "keycloak_client_id": "EOSDK_KEYCLOAK_CLIENT_ID",
    "discovery_url": "EOSDK_DISCOVERY_URL",
}

ENV_PROFILE = "EOSDK_PROFILE"
ENV_PLATFORM = "EOSDK_PLATFORM"
ENV_TLS_VERIFY = "EOSDK_TLS_VERIFY"

BUILTIN_DEFAULTS = Endpoints(
    # TODO(deployment): real endpoints for the default known deployment.
    exos_region="default",
    keycloak_realm="eodata",
    keycloak_client_id="eosdk",
)
