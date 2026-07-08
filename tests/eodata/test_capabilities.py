import datetime as dt

import pytest

from eosdk.eodata.capabilities import (
    BUILTIN_MATRIX,
    Capability,
    Strategy,
    effective_capabilities,
    select_strategy,
)
from eosdk.exceptions import UnsupportedCapability

TODAY = dt.date(2026, 7, 8)


class TestMatrix:
    def test_matches_spec_table(self) -> None:
        zipper = {s.name: s.capabilities for s in BUILTIN_MATRIX["zipper"]}
        assert zipper["odata"] == {Capability.DOWNLOAD, Capability.LIST}
        assert zipper["resto"] == {Capability.DOWNLOAD}
        (exos,) = BUILTIN_MATRIX["exos"]
        assert exos.capabilities == {Capability.DOWNLOAD, Capability.LIST, Capability.OPEN}

    def test_zipper_prefers_odata(self) -> None:
        chosen = select_strategy(
            "zipper", Capability.DOWNLOAD, BUILTIN_MATRIX["zipper"], today=TODAY
        )
        assert chosen.name == "odata"


class TestSelection:
    def test_falls_back_to_resto_for_download_only(self) -> None:
        strategies = (
            Strategy("odata", frozenset({Capability.DOWNLOAD, Capability.LIST}), available=False),
            Strategy("resto", frozenset({Capability.DOWNLOAD})),
        )
        chosen = select_strategy("zipper", Capability.DOWNLOAD, strategies, today=TODAY)
        assert chosen.name == "resto"

    def test_resto_never_serves_list(self) -> None:
        strategies = (
            Strategy("odata", frozenset({Capability.DOWNLOAD, Capability.LIST}), available=False),
            Strategy("resto", frozenset({Capability.DOWNLOAD})),
        )
        with pytest.raises(UnsupportedCapability) as exc_info:
            select_strategy("zipper", Capability.LIST, strategies, today=TODAY)
        assert "odata" in str(exc_info.value)  # names the strategy that would provide it

    def test_open_never_available_on_zipper(self) -> None:
        with pytest.raises(UnsupportedCapability, match="open"):
            select_strategy("zipper", Capability.OPEN, BUILTIN_MATRIX["zipper"], today=TODAY)

    def test_past_sunset_excluded(self) -> None:
        strategies = (
            Strategy(
                "resto",
                frozenset({Capability.DOWNLOAD}),
                deprecated=True,
                sunset=dt.date(2026, 1, 1),
            ),
        )
        with pytest.raises(UnsupportedCapability):
            select_strategy("zipper", Capability.DOWNLOAD, strategies, today=TODAY)

    def test_deprecated_survivor_warns_with_sunset_and_replacement(self) -> None:
        strategies = (
            Strategy(
                "resto",
                frozenset({Capability.DOWNLOAD}),
                deprecated=True,
                sunset=dt.date(2027, 1, 1),
                replacement="odata",
            ),
        )
        with pytest.warns(DeprecationWarning, match="2027-01-01") as record:
            chosen = select_strategy("zipper", Capability.DOWNLOAD, strategies, today=TODAY)
        assert chosen.name == "resto"
        assert "odata" in str(record[0].message)

    def test_non_deprecated_choice_is_silent(self) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            select_strategy("zipper", Capability.DOWNLOAD, BUILTIN_MATRIX["zipper"], today=TODAY)


class TestIntersectionGate:
    def test_advertised_restricts(self) -> None:
        builtin = frozenset({Capability.DOWNLOAD, Capability.LIST, Capability.OPEN})
        assert effective_capabilities(builtin, ["download"]) == {Capability.DOWNLOAD}

    def test_advertised_cannot_extend(self) -> None:
        builtin = frozenset({Capability.DOWNLOAD})
        assert effective_capabilities(builtin, ["download", "open", "list"]) == {
            Capability.DOWNLOAD
        }

    def test_absent_means_builtin(self) -> None:
        builtin = frozenset({Capability.DOWNLOAD, Capability.LIST})
        assert effective_capabilities(builtin, None) == builtin

    def test_unknown_capability_names_ignored(self) -> None:
        builtin = frozenset({Capability.DOWNLOAD})
        assert effective_capabilities(builtin, ["download", "teleport"]) == {Capability.DOWNLOAD}
