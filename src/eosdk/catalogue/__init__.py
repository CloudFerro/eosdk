"""Catalogue search over STAC and OData (SPEC §6.5); OData lands in Phase 2."""

from eosdk.catalogue.base import Catalogue
from eosdk.catalogue.stac import StacCatalogue

__all__ = ["Catalogue", "StacCatalogue"]
