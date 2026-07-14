from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eosdk import Client
from eosdk.eodata.capabilities import Capability, Strategy
from eosdk.exceptions import UnsupportedCapability
from eosdk.plugins import PluginSpec, load_plugins
from tests.conftest import write_profile_config


class DummyDownloader:
    def __init__(self) -> None:
        self.fetched: list[Any] = []

    def fetch(self, products: Any, target: Any, **kwargs: Any) -> list[Any]:
        self.fetched.append(products)
        return []


DUMMY = DummyDownloader()

PLUGIN = PluginSpec(
    name="dummy",
    kind="downloader",
    factory=lambda client: DUMMY,
    strategies=(Strategy("native", frozenset({Capability.DOWNLOAD})),),
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    config = write_profile_config(tmp_path / "config.toml")
    with Client(profile="test", cwd=tmp_path, user_config=config) as c:
        yield c


def fake_entry_point(name: str, value: Any, *, broken: bool = False) -> Any:
    def load() -> Any:
        if broken:
            raise ImportError("boom")
        return value

    return SimpleNamespace(name=name, value=str(value), load=load)


class TestLoadPlugins:
    def test_loads_valid_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "eosdk.plugins.entry_points",
            lambda group: [fake_entry_point("dummy", PLUGIN)],
        )
        plugins = load_plugins()
        assert plugins["dummy"] is PLUGIN

    def test_broken_plugin_skipped_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            "eosdk.plugins.entry_points",
            lambda group: [
                fake_entry_point("bad", None, broken=True),
                fake_entry_point("dummy", PLUGIN),
            ],
        )
        with caplog.at_level("WARNING"):
            plugins = load_plugins()
        assert "dummy" in plugins
        assert "bad" not in plugins
        assert any("skipping broken" in r.message for r in caplog.records)

    def test_non_spec_object_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "eosdk.plugins.entry_points",
            lambda group: [fake_entry_point("weird", object())],
        )
        assert load_plugins() == {}


class TestClientDispatch:
    def test_via_dispatches_to_plugin(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "eosdk.plugins.entry_points", lambda group: [fake_entry_point("dummy", PLUGIN)]
        )
        from eosdk.models import Product

        client.download(Product(id="x", name="X"), target=".", via="dummy")
        assert DUMMY.fetched  # the plugin backend received the call

    def test_plugin_capability_gate(self, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "eosdk.plugins.entry_points", lambda group: [fake_entry_point("dummy", PLUGIN)]
        )
        from eosdk.models import Product

        with pytest.raises(UnsupportedCapability, match="open"):
            client.open(Product(id="x", name="X"), path="a/b", via="dummy")

    def test_unknown_via_still_rejected(self, client: Client) -> None:
        from eosdk.exceptions import ConfigError
        from eosdk.models import Product

        with pytest.raises(ConfigError, match="unknown download backend"):
            client.download(Product(id="x", name="X"), target=".", via="nope")
