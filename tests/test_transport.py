import httpx
import pytest
import respx

from eosdk import __version__
from eosdk.exceptions import EndpointUnreachable, QuotaExceeded
from eosdk.transport import RetryPolicy, Transport, odata_key, route, user_agent

BASE = "https://svc.example.eu"


class TestRoute:
    def test_happy_path(self) -> None:
        url = route(BASE, "odata/v1/Products({id})/$value", id="abc-123")
        assert url == f"{BASE}/odata/v1/Products(abc-123)/$value"

    def test_encodes_params(self) -> None:
        url = route(BASE, "Nodes({name})", name="a b(c)'d/e")
        assert url == f"{BASE}/Nodes(a%20b%28c%29%27d%2Fe)"

    def test_missing_param(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            route(BASE, "Products({id})")

    def test_unknown_param(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            route(BASE, "Products", id="x")

    def test_no_double_slash(self) -> None:
        assert route(BASE + "/", "/stac/search") == f"{BASE}/stac/search"

    def test_version_segment_preserved(self) -> None:
        assert route(BASE, "v1/keys") == f"{BASE}/v1/keys"


class TestODataKey:
    @pytest.mark.parametrize(
        ("raw", "quoted"),
        [
            ("simple", "'simple'"),
            ("with space", "'with space'"),
            ("O'Brien", "'O''Brien'"),
            ("S2B_MSIL2A (1).SAFE", "'S2B_MSIL2A (1).SAFE'"),
        ],
    )
    def test_quoting(self, raw: str, quoted: str) -> None:
        assert odata_key(raw) == quoted


def make_transport() -> Transport:
    slept: list[float] = []
    t = Transport(retry=RetryPolicy(jitter=False), sleep=slept.append)
    t.slept = slept  # type: ignore[attr-defined]
    return t


@respx.mock
def test_retries_on_503_then_succeeds() -> None:
    mock = respx.get(f"{BASE}/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, text="ok")]
    )
    with make_transport() as t:
        response = t.request("GET", f"{BASE}/x", service="svc")
    assert response.status_code == 200
    assert mock.call_count == 3


@respx.mock
def test_post_not_retried_by_default() -> None:
    mock = respx.post(f"{BASE}/x").mock(return_value=httpx.Response(503))
    with make_transport() as t:
        response = t.request("POST", f"{BASE}/x", service="svc")
    assert response.status_code == 503
    assert mock.call_count == 1


@respx.mock
def test_429_honors_retry_after_then_raises_quota() -> None:
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    t = make_transport()
    with pytest.raises(QuotaExceeded) as exc_info:
        t.request("GET", f"{BASE}/x", service="zipper")
    assert exc_info.value.retry_after == 7.0
    assert all(s >= 7.0 for s in t.slept)  # type: ignore[attr-defined]
    assert len(t.slept) == t.retry.attempts - 1  # type: ignore[attr-defined]
    assert "zipper" in str(exc_info.value)


@respx.mock
def test_connect_error_maps_to_endpoint_unreachable() -> None:
    respx.get(f"{BASE}/x").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EndpointUnreachable) as exc_info:
        make_transport().request("GET", f"{BASE}/x", service="keycloak")
    msg = str(exc_info.value)
    assert "keycloak" in msg
    assert f"{BASE}/x" in msg
    assert "EOSDK_" in msg


@respx.mock
def test_user_agent_header() -> None:
    mock = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    with make_transport() as t:
        t.request("GET", f"{BASE}/x", service="svc")
    sent = mock.calls.last.request.headers["User-Agent"]
    assert sent == user_agent()
    assert f"eosdk/{__version__}" in sent


@respx.mock
def test_stream_yields_and_closes() -> None:
    respx.get(f"{BASE}/big").mock(return_value=httpx.Response(200, content=b"payload"))
    with make_transport() as t, t.stream("GET", f"{BASE}/big", service="svc") as response:
        body = response.read()
    assert body == b"payload"
    assert response.is_closed


def test_verify_false_warns() -> None:
    with pytest.warns(UserWarning, match="TLS verification is disabled"):
        Transport(verify=False).close()


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(backoff_base=1.0, backoff_max=4.0, jitter=False)
    assert [policy.backoff(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 4.0]
