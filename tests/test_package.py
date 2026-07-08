import eosdk


def test_version() -> None:
    assert eosdk.__version__
