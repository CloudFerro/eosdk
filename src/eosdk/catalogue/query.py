"""Backend-neutral query translation utilities (SPEC §6.5).

The :class:`~eosdk.models.Query` model lives in ``models.py``; this module owns
the pieces shared by backend translators: operator parsing of ``filters``
values and the property-alias table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from eosdk.exceptions import UnsupportedQueryFeature

if TYPE_CHECKING:
    from collections.abc import Mapping

Op = Literal["eq", "neq", "lt", "lte", "gt", "gte"]

_OPERATOR_PREFIXES: tuple[tuple[str, Op], ...] = (
    ("<=", "lte"),
    (">=", "gte"),
    ("!=", "neq"),
    ("<", "lt"),
    (">", "gt"),
    ("=", "eq"),
)

# User-facing property names -> STAC property names. The full vocabulary policy
# must be settled before the OData backend lands (Phase 2), so both backends
# interpret `filters` identically (plan risk R6).
PROPERTY_ALIASES: dict[str, str] = {
    "cloudCover": "eo:cloud_cover",
    "cloud_cover": "eo:cloud_cover",
    "productType": "product:type",
}


@dataclass(frozen=True)
class FilterClause:
    prop: str
    op: Op
    value: str | float


def _coerce(value: str) -> str | float:
    try:
        return float(value)
    except ValueError:
        return value


def parse_filters(filters: Mapping[str, str], *, backend: str) -> list[FilterClause]:
    """Parse ``{"cloudCover": "<20", "productType": "GRD"}`` into clauses.

    A bare value means equality; a recognized operator prefix (``<`` ``<=``
    ``>`` ``>=`` ``!=`` ``=``) selects the comparison. Anything else is an
    unsupported feature, reported against ``backend``.
    """
    clauses: list[FilterClause] = []
    for prop, raw in filters.items():
        raw = raw.strip()
        if not raw:
            raise UnsupportedQueryFeature(backend=backend, feature=f"empty filter for {prop!r}")
        for prefix, op in _OPERATOR_PREFIXES:
            if raw.startswith(prefix):
                literal = raw[len(prefix) :].strip()
                if not literal:
                    raise UnsupportedQueryFeature(
                        backend=backend, feature=f"filter {prop}={raw!r} has no value"
                    )
                clauses.append(FilterClause(prop, op, _coerce(literal)))
                break
        else:
            if raw[0] in "~^*":
                raise UnsupportedQueryFeature(
                    backend=backend, feature=f"filter operator {raw[0]!r} in {prop}={raw!r}"
                )
            clauses.append(FilterClause(prop, "eq", _coerce(raw)))
    return clauses
