"""The real built-in deployment defaults (CDSE), asserted explicitly.

Every other test runs with BUILTIN_DEFAULTS blanked (autouse fixture in the
root conftest) so nothing accidentally resolves to live endpoints.
"""

from eosdk.config import defaults
from eosdk.config.settings import Endpoints
from tests.conftest import REAL_DEFAULTS


def test_cdse_defaults() -> None:
    d = REAL_DEFAULTS
    assert isinstance(d, Endpoints)
    assert d.catalogue_stac == "https://stac.dataspace.copernicus.eu/v1"
    assert d.catalogue_odata == "https://catalogue.dataspace.copernicus.eu"
    assert d.zipper == "https://download.dataspace.copernicus.eu"
    assert d.exos_endpoint == "https://eodata.dataspace.copernicus.eu"
    assert d.keys_manager == "https://s3-keys-manager.cloudferro.com/api/user"
    assert d.keycloak == "https://identity.dataspace.copernicus.eu/auth"
    assert d.keycloak_realm == "CDSE"
    assert d.keycloak_client_id == "cdse-public"


def test_env_var_map_matches_model() -> None:
    assert set(defaults.ENV_VAR_MAP) == set(Endpoints.model_fields)
