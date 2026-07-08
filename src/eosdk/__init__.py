"""eosdk — Python SDK for Earth Observation services."""

__version__ = "0.1.0.dev0"


def __getattr__(name: str) -> object:
    # Lazy import: keeps `import eosdk` cheap and avoids a circular import,
    # since transport reads eosdk.__version__ for the User-Agent.
    if name == "Client":
        from eosdk.client import Client

        return Client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Client", "__version__"]
