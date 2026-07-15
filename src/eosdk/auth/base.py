"""Credential provider protocol.

Domain modules never handle raw credentials (SPEC §4.2); they attach the
``httpx.Auth`` produced here to individual requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import httpx


@runtime_checkable
class CredentialsProvider(Protocol):
    def httpx_auth(self) -> httpx.Auth:
        """Return the auth hook that signs each outgoing request."""
        ...
