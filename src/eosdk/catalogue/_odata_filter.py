"""Query -> OData ``$filter`` translation (SPEC §6.5). Pure — no I/O.

Targets the Copernicus-style OData CSC catalogue dialect: ``Collection/Name``,
``ContentDate/Start``, ``OData.CSC.Intersects`` and typed ``Attributes/any``
lambdas. Constructs the backend cannot express raise
:class:`UnsupportedQueryFeature` before any HTTP request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eosdk.catalogue.query import FilterClause, parse_filters
from eosdk.exceptions import UnsupportedQueryFeature

if TYPE_CHECKING:
    from eosdk.models import Query

BACKEND = "odata"

_OP_TOKENS = {"eq": "eq", "neq": "ne", "lt": "lt", "lte": "le", "gt": "gt", "gte": "ge"}

# User-facing names -> OData attribute names (kept aligned with
# catalogue/query.PROPERTY_ALIASES so both backends read `filters` identically).
ATTRIBUTE_ALIASES: dict[str, str] = {
    "cloudCover": "cloudCover",
    "cloud_cover": "cloudCover",
    "eo:cloud_cover": "cloudCover",
    "productType": "productType",
    "product:type": "productType",
}


def _escape(literal: str) -> str:
    return literal.replace("'", "''")


def _datetime_clauses(interval: str) -> list[str]:
    """STAC-style interval "start/end" (open ends via '..') -> ContentDate clauses."""
    if "/" in interval:
        start, _, end = interval.partition("/")
    else:
        start, end = interval, interval
    clauses = []
    if start and start != "..":
        clauses.append(f"ContentDate/Start ge {_normalize_ts(start)}")
    if end and end != "..":
        clauses.append(f"ContentDate/Start le {_normalize_ts(end)}")
    if not clauses:
        raise UnsupportedQueryFeature(backend=BACKEND, feature=f"open interval {interval!r}")
    return clauses


def _normalize_ts(value: str) -> str:
    ts = value.strip()
    if "T" not in ts:
        ts = f"{ts}T00:00:00.000Z"
    elif not ts.endswith("Z"):
        ts = f"{ts}Z"
    return ts


def _bbox_clause(bbox: tuple[float, float, float, float]) -> str:
    minx, miny, maxx, maxy = bbox
    ring = f"{minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}"
    return "OData.CSC.Intersects(area=geography'SRID=4326;POLYGON((" + ring + "))')"


def _attribute_clause(clause: FilterClause) -> str:
    name = ATTRIBUTE_ALIASES.get(clause.prop, clause.prop)
    op = _OP_TOKENS[clause.op]
    if isinstance(clause.value, float):
        odata_type = "OData.CSC.DoubleAttribute"
        literal = f"{clause.value:g}"
    else:
        odata_type = "OData.CSC.StringAttribute"
        literal = f"'{_escape(clause.value)}'"
    return (
        f"Attributes/{odata_type}/any(att:att/Name eq '{_escape(name)}' "
        f"and att/{odata_type}/Value {op} {literal})"
    )


def build_filter(query: Query) -> str:
    """Translate the shared Query into a ``$filter`` expression."""
    clauses: list[str] = []
    if query.collection is not None:
        clauses.append(f"Collection/Name eq '{_escape(query.collection)}'")
    if query.datetime is not None:
        clauses.extend(_datetime_clauses(query.datetime))
    if query.bbox is not None:
        clauses.append(_bbox_clause(query.bbox))
    for clause in parse_filters(query.filters, backend=BACKEND):
        clauses.append(_attribute_clause(clause))
    if not clauses:
        raise UnsupportedQueryFeature(backend=BACKEND, feature="an empty (unbounded) query")
    return " and ".join(clauses)


def build_query_params(query: Query) -> dict[str, str]:
    """Full OData system query options for a search request."""
    params = {"$filter": build_filter(query), "$expand": "Attributes", "$count": "true"}
    if query.limit is not None:
        params["$top"] = str(query.limit)
    if query.sort is not None:
        direction = "desc" if query.sort.startswith("-") else "asc"
        fieldname = query.sort.lstrip("+-")
        odata_field = {
            "datetime": "ContentDate/Start",
            "name": "Name",
            "size": "ContentLength",
        }.get(fieldname)
        if odata_field is None:
            raise UnsupportedQueryFeature(
                backend=BACKEND, feature=f"sorting by {fieldname!r} (datetime/name/size only)"
            )
        params["$orderby"] = f"{odata_field} {direction}"
    return params
