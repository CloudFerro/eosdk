"""The ``Client`` facade — the one import most users need (SPEC §4.1).

Construction is offline: local config layers are resolved and URL-validated,
but nothing is fetched. Discovery-sourced endpoints stay ``<pending>`` until
first use of the owning service (SPEC §6.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eosdk.config import load
from eosdk.transport import Transport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from eosdk.config import ResolvedConfig


class Client:
    def __init__(
        self,
        *,
        profile: str | None = None,
        platform: str | None = None,
        endpoints: Mapping[str, str] | None = None,
        verify: bool | None = None,
        timeout: float = 30.0,
        cwd: Path | None = None,
        user_config: Path | None = None,
    ) -> None:
        self.config: ResolvedConfig = load(
            kwargs_endpoints=endpoints,
            platform=platform,
            profile=profile,
            cwd=cwd,
            user_config=user_config,
        )
        self._transport = Transport(
            timeout=timeout,
            verify=self.config.verify_tls if verify is None else verify,
        )

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
