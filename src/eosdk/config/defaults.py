"""Built-in defaults and the env-var mapping (single source of truth).

``BUILTIN_DEFAULTS`` holds the primary known deployment: the Copernicus Data
Space Ecosystem (CDSE) with the CloudFerro S3 Keys Manager — verified against
the live services and their published API docs (2026-07).
"""

from __future__ import annotations

from eosdk.config.settings import Endpoints

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

# The default known deployment: CDSE (SPEC §6.1 precedence step 6).
BUILTIN_DEFAULTS = Endpoints(
    catalogue_stac="https://stac.dataspace.copernicus.eu/v1",
    catalogue_odata="https://catalogue.dataspace.copernicus.eu",
    eodata_http="https://download.dataspace.copernicus.eu",
    s3_endpoint="https://eodata.dataspace.copernicus.eu",
    s3_region="default",
    s3_credentials="https://s3-keys-manager.cloudferro.com/api/user",
    keycloak="https://identity.dataspace.copernicus.eu/auth",
    keycloak_realm="CDSE",
    keycloak_client_id="cdse-public",
)
