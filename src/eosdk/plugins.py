"""Third-party extension loading via the ``eosdk.plugins`` entry-point group (SPEC §11).

A plugin package declares::

    [project.entry-points."eosdk.plugins"]
    my-backend = "my_pkg.eosdk_plugin:PLUGIN"

where ``PLUGIN`` is a :class:`PluginSpec`. Broken plugins are skipped with a
warning — a plugin must never crash ``Client``. Built-ins win name collisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from eosdk.eodata.capabilities import Strategy

logger = logging.getLogger(__name__)

GROUP = "eosdk.plugins"


@dataclass(frozen=True)
class PluginSpec:
    name: str  # the `via=` / `protocol=` name it registers
    kind: Literal["catalogue", "downloader"]
    factory: Callable[..., Any]  # called with (client) -> backend instance
    strategies: tuple[Strategy, ...] = field(default=())


def load_plugins() -> dict[str, PluginSpec]:
    """Discover installed plugins; broken ones are skipped, never fatal."""
    plugins: dict[str, PluginSpec] = {}
    try:
        discovered = entry_points(group=GROUP)
    except Exception:  # pragma: no cover - importlib.metadata edge cases
        return plugins
    for entry_point in discovered:
        try:
            spec = entry_point.load()
            if not isinstance(spec, PluginSpec):
                raise TypeError(f"{entry_point.value} is not a PluginSpec")
            plugins[spec.name] = spec
        except Exception as exc:
            logger.warning("skipping broken eosdk plugin %r: %s", entry_point.name, exc)
    return plugins
