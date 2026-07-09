"""Configuration: profiles, endpoint overrides, discovery.

What this shows
---------------
* the endpoint resolution order (most specific wins):
    1. explicit kwargs to ``Client(...)``
    2. environment variables (``EOSDK_ZIPPER_URL``, ``EOSDK_PROFILE``, ...)
    3. project-local ``./eosdk.toml``
    4. user config ``~/.config/eosdk/config.toml`` (selected profile)
    5. the platform's remote discovery document
    6. built-in defaults (Copernicus Data Space Ecosystem)
* inspecting the resolved configuration and where each value came from
* single-root bootstrap: give one platform URL, discover the rest
* the service discovery document

Prerequisites: none for the defaults; the discovery section needs a platform
root (see ``docker/discovery/`` in this repo for a local one).
"""

from __future__ import annotations

from eosdk import Client


def main() -> None:
    # -- defaults: no arguments -> built-in CDSE endpoints -------------------------
    with Client() as client:
        print(f"profile: {client.config.profile or '(none)'}")
        # Every endpoint with its value and the layer that provided it.
        # Discovery-only endpoints show '<pending>' until first use.
        for name, value in client.config.resolved().items():
            print(f"  {name:24} {value.display:60} [{value.source}]")

    # -- a named profile from ~/.config/eosdk/config.toml ---------------------------
    # Create profiles with `eo config init`; select per-run here or via
    # EOSDK_PROFILE. (This raises ConfigError if the profile does not exist.)
    #
    # client = Client(profile="staging")

    # -- single-root bootstrap: everything else comes from discovery -----------------
    # The platform serves /.well-known/eo-services.json; endpoints resolve
    # lazily on first use of each service, then are cached (with a TTL).
    #
    # client = Client(platform="https://platform.example.eu")
    # print(client.discovery.endpoints())     # force-fetch and inspect
    # client.discovery.refresh()              # bust the TTL cache

    # -- surgical overrides: pin one endpoint, keep the rest --------------------------
    # Handy for testing one service locally against otherwise-real infra.
    with Client(endpoints={"zipper": "http://localhost:8082"}) as client:
        resolved = client.config.resolved()
        print(f"\nzipper pinned -> {resolved['zipper'].display} [{resolved['zipper'].source}]")

    # -- other useful constructor knobs ------------------------------------------------
    # Client(
    #     verify=False,                        # TLS verification off (dev only!)
    #     timeout=60.0,                        # per-request timeout, seconds
    #     user_config=Path("./ci-config.toml"),# read profiles from a custom file
    #     token_cache_dir=Path("/run/secrets"),# where session tokens are cached
    # )

    # Environment variables work with zero code changes, e.g.:
    #   EOSDK_PROFILE=staging
    #   EOSDK_PLATFORM=https://platform.example.eu
    #   EOSDK_CATALOGUE_STAC_URL=http://localhost:8081/stac
    #   EOSDK_TLS_VERIFY=0


if __name__ == "__main__":
    main()
