"""Turn a CLI product reference into a :class:`~eosdk.models.Product` stub.

Commands that take a product on the command line (``download``, ``list``,
``cat``) accept the same two reference shapes, mirroring what each backend can
address (SPEC §6.6): a bare product id for the HTTP route, or an S3 path
(``s3://…`` / ``/eodata/…``, e.g. from ``eo search --format s3``) for the S3
route. Keeping the parsing here means all three commands behave identically.
"""

from __future__ import annotations

import typer

from eosdk.models import Product


def product_from_ref(ref: str, via: str) -> Product:
    """Build a minimal :class:`Product` from a CLI reference.

    A bare id yields an id-only stub (enough for the HTTP ``$value``/``$nodes``
    routes); an S3 path carries the address the S3 backend needs and is only
    valid with ``--via s3``.
    """
    if ref.startswith(("s3://", "/")):
        if via != "s3":
            raise typer.BadParameter(
                f"{ref!r} is an S3 path and only works with --via s3; "
                "the http route needs product ids (eo search --format id)"
            )
        name = ref.rstrip("/").rsplit("/", 1)[-1]
        return Product(id=name, name=name, s3_path=ref)
    return Product(id=ref, name=ref)
