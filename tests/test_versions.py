import pytest

from eosdk.exceptions import UnsupportedApiVersion
from eosdk.versions import select_version


def test_absent_selects_default() -> None:
    assert select_version("data_access/http/odata", None) == "v1"


def test_supported_selected() -> None:
    assert select_version("data_access/s3/credentials", "v1") == "v1"


def test_unsupported_raises_with_remediation() -> None:
    with pytest.raises(UnsupportedApiVersion) as exc_info:
        select_version("data_access/http/odata", "v3")
    message = str(exc_info.value)
    assert "v3" in message
    assert "upgrade eosdk" in message
    assert "EOSDK_EODATA_HTTP_URL" in message
