import pytest

from eosdk import exceptions
from eosdk.exceptions import (
    AuthError,
    CollectionNotFound,
    ConfigError,
    DownloadError,
    EndpointUnreachable,
    EosdkError,
    ProductNotFound,
    QuotaExceeded,
    S3KeyLimitReached,
    UnsupportedApiVersion,
    UnsupportedCapability,
    UnsupportedQueryFeature,
)

ALL_ERRORS = [
    ConfigError,
    AuthError,
    EndpointUnreachable,
    UnsupportedApiVersion,
    UnsupportedQueryFeature,
    UnsupportedCapability,
    CollectionNotFound,
    ProductNotFound,
    DownloadError,
    QuotaExceeded,
    S3KeyLimitReached,
]


@pytest.mark.parametrize("exc", ALL_ERRORS)
def test_inherits_base(exc: type[Exception]) -> None:
    assert issubclass(exc, EosdkError)


def test_all_public_exceptions_covered() -> None:
    public = {
        obj
        for name, obj in vars(exceptions).items()
        if isinstance(obj, type) and issubclass(obj, EosdkError) and obj is not EosdkError
    }
    assert public == set(ALL_ERRORS)


def test_auth_error_names_realm_and_profile() -> None:
    err = AuthError("refresh failed", realm="eodata", profile="prod")
    assert "eodata" in str(err)
    assert "prod" in str(err)
    assert err.realm == "eodata"


def test_endpoint_unreachable_names_service_url_hint() -> None:
    err = EndpointUnreachable(
        service="eodata_http", url="https://z.example.eu", hint="check EOSDK_EODATA_HTTP_URL"
    )
    msg = str(err)
    assert "eodata_http" in msg
    assert "https://z.example.eu" in msg
    assert "EOSDK_EODATA_HTTP_URL" in msg


def test_unsupported_api_version_message() -> None:
    err = UnsupportedApiVersion(
        service="eodata_http",
        advertised="v3",
        supported="v1-v2",
        remediation="upgrade eosdk or pin EOSDK_EODATA_HTTP_URL",
    )
    msg = str(err)
    assert "v3" in msg
    assert "v1-v2" in msg
    assert "upgrade eosdk" in msg


def test_unsupported_query_feature_names_backend() -> None:
    err = UnsupportedQueryFeature(backend="odata", feature="free-text search")
    assert "odata" in str(err)
    assert "free-text search" in str(err)


def test_unsupported_capability_names_alternative() -> None:
    err = UnsupportedCapability(backend="http", capability="open", alternative="use via='s3'")
    assert "http" in str(err)
    assert "open" in str(err)
    assert "s3" in str(err)


def test_collection_not_found_lists_suggestions_and_hint() -> None:
    err = CollectionNotFound(
        collection="SENTINEL-1",
        backend="stac",
        suggestions=["sentinel-1-grd", "sentinel-1-slc"],
        hint="mission-level names are OData vocabulary",
    )
    msg = str(err)
    assert "SENTINEL-1" in msg
    assert "sentinel-1-grd" in msg
    assert "OData" in msg
    assert err.suggestions == ["sentinel-1-grd", "sentinel-1-slc"]


def test_product_not_found() -> None:
    err = ProductNotFound(product_id="S2B_X", backend="stac")
    assert "S2B_X" in str(err)
    assert err.backend == "stac"


def test_download_error_carries_cause() -> None:
    cause = OSError("boom")
    err = DownloadError("transfer failed", product_id="S2B_X", backend="http", cause=cause)
    assert err.cause is cause
    assert "S2B_X" in str(err)
    assert "boom" in str(err)


def test_quota_exceeded_retry_after() -> None:
    err = QuotaExceeded(service="eodata_http", retry_after=30.0)
    assert "429" in str(err)
    assert "30" in str(err)


def test_s3_key_limit_names_remediation() -> None:
    err = S3KeyLimitReached(detail="Max number of credentials reached.")
    msg = str(err)
    assert "Max number of credentials reached." in msg
    assert "eo keys revoke" in msg
    assert "get_or_create" in msg


def test_config_error_hint() -> None:
    err = ConfigError("eodata_http endpoint is not configured", hint="set EOSDK_EODATA_HTTP_URL")
    assert "EOSDK_EODATA_HTTP_URL" in str(err)
