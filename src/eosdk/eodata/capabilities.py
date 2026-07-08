"""Capability matrix and strategy selection (SPEC §6.3, §6.6).

The built-in matrix is the source of truth for what each backend/strategy
*can* do. A discovery document (Phase 3) may only *restrict* it — the
intersection gate — never extend it. Selection is capability-first, then by
the module's preference order; deprecated survivors emit a warning naming the
sunset date and replacement.
"""

from __future__ import annotations

import datetime as dt
import enum
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eosdk.exceptions import UnsupportedCapability

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class Capability(str, enum.Enum):
    DOWNLOAD = "download"
    LIST = "list"
    OPEN = "open"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Strategy:
    name: str
    capabilities: frozenset[Capability]
    deprecated: bool = False
    sunset: dt.date | None = None
    replacement: str | None = None
    url: str | None = None  # discovered per-strategy URL (Phase 3); None -> base + template
    available: bool = True

    def past_sunset(self, today: dt.date) -> bool:
        return self.sunset is not None and today >= self.sunset


# Ordered preference per backend (SPEC §6.6 capability matrix).
BUILTIN_MATRIX: dict[str, tuple[Strategy, ...]] = {
    "zipper": (
        Strategy("odata", frozenset({Capability.DOWNLOAD, Capability.LIST})),
        Strategy("resto", frozenset({Capability.DOWNLOAD})),
    ),
    "exos": (Strategy("s3", frozenset({Capability.DOWNLOAD, Capability.LIST, Capability.OPEN})),),
}


def effective_capabilities(
    builtin: frozenset[Capability], advertised: Iterable[str] | None
) -> frozenset[Capability]:
    """SPEC §6.2 intersection gate: discovery can disable, never add."""
    if advertised is None:
        return builtin
    advertised_set = {c for c in Capability if c.value in set(advertised)}
    return builtin & frozenset(advertised_set)


def select_strategy(
    backend: str,
    capability: Capability,
    strategies: Sequence[Strategy],
    *,
    today: dt.date | None = None,
) -> Strategy:
    """Pick the highest-preference available strategy supporting ``capability``.

    Raises :class:`UnsupportedCapability` naming the strategy that would
    provide the capability, if any exists in the full (unfiltered) set.
    """
    today = today or dt.date.today()
    survivors = [
        s
        for s in strategies
        if s.available and not s.past_sunset(today) and capability in s.capabilities
    ]
    if not survivors:
        would_provide = next((s.name for s in strategies if capability in s.capabilities), None)
        alternative = (
            f"strategy {would_provide!r} would provide it but is unavailable or past sunset"
            if would_provide
            else None
        )
        raise UnsupportedCapability(
            backend=backend, capability=capability.value, alternative=alternative
        )
    chosen = survivors[0]
    if chosen.deprecated:
        message = f"{backend} strategy {chosen.name!r} is deprecated"
        if chosen.sunset is not None:
            message += f" (sunset {chosen.sunset.isoformat()})"
        if chosen.replacement is not None:
            message += f"; replacement: {chosen.replacement!r}"
        warnings.warn(message, DeprecationWarning, stacklevel=3)
    return chosen
