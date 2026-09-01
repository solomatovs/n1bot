"""Проверка соединения по профилю: билет вызова, probe-хук типа, итог строкой.

Как проверяется конкретный тип, знает его пакет-владелец — хук приходит из
манифеста реестра типов.

Ошибки: своих не выпускает — исход любой проверки описывает ProbeResult.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from boba.connections.credentials import CredentialSource
from boba.connections.manifest import ConnectionTypes
from boba.connections.profile import ConnectionProfileBase, ProbeResult
from boba.identity.context import Credential
from boba.toolkit.timing import Elapsed

__all__ = ["ConnectionProbe", "ProbeResult"]

logger = logging.getLogger(__name__)


class ConnectionProbe:
    """Пробное соединение по профилю с билетом вызова вместо kerberos-секции."""

    TIMEOUT_SEC: ClassVar[float] = 15.0

    def __init__(self, source: CredentialSource, types: ConnectionTypes) -> None:
        self._source = source
        self._types = types

    async def probe(
        self, profile: ConnectionProfileBase, credential: Credential
    ) -> ProbeResult:
        elapsed = Elapsed()
        try:
            hook = self._types.manifest_of(profile.kind).probe
            armed = await self._source.for_connection(profile, credential)
            message = await asyncio.wait_for(hook(armed), self.TIMEOUT_SEC)
        except TimeoutError:
            return ProbeResult(
                ok=False,
                message=f"no answer in {self.TIMEOUT_SEC:.0f}s",
                elapsed_ms=elapsed.ms(),
            )
        except Exception as exc:
            # граница к пользователю: любой исход пробы — это ProbeResult
            logger.info("connection probe failed: %s", exc)
            return ProbeResult(ok=False, message=str(exc), elapsed_ms=elapsed.ms())

        return ProbeResult(ok=True, message=message, elapsed_ms=elapsed.ms())
