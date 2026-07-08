import pytest

from eosdk import exceptions
from eosdk.exceptions import (
    AuthError,
    ConfigError,
    DownloadError,
    EndpointUnreachable,
    EosdkError,
    ProductNotFound,
    QuotaExceeded,
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
    ProductNotFound,
    DownloadError,
    QuotaExceeded,
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
        service="zipper", url="https://z.example.eu", hint="check EOSDK_ZIPPER_URL"
    )
    msg = str(err)
    assert "zipper" in msg
    assert "https://z.example.eu" in msg
    assert "EOSDK_ZIPPER_URL" in msg


def test_unsupported_api_version_message() -> None:
    err = UnsupportedApiVersion(
        service="zipper",
        advertised="v3",
        supported="v1-v2",
        remediation="upgrade eosdk or pin EOSDK_ZIPPER_URL",
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
    err = UnsupportedCapability(
        backend="zipper", capability="open", alternative="use via='exos'"
    )
    assert "zipper" in str(err)
    assert "open" in str(err)
    assert "exos" in str(err)


def test_product_not_found() -> None:
    err = ProductNotFound(product_id="S2B_X", backend="stac")
    assert "S2B_X" in str(err)
    assert err.backend == "stac"


def test_download_error_carries_cause() -> None:
    cause = OSError("boom")
    err = DownloadError("transfer failed", product_id="S2B_X", backend="zipper", cause=cause)
    assert err.cause is cause
    assert "S2B_X" in str(err)
    assert "boom" in str(err)


def test_quota_exceeded_retry_after() -> None:
    err = QuotaExceeded(service="zipper", retry_after=30.0)
    assert "429" in str(err)
    assert "30" in str(err)


def test_config_error_hint() -> None:
    err = ConfigError("zipper endpoint is not configured", hint="set EOSDK_ZIPPER_URL")
    assert "EOSDK_ZIPPER_URL" in str(err)
