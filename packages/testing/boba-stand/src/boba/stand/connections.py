"""Параметры connections стенда используют серверный resolver каталога.

Ошибки:
RefusalError — профиль не выдан субъекту, имя неоднозначно или нет credentials.
ConnectionStoreError — хранилище соединений недоступно.
ToolConfigError — некорректное объявление параметра или профиль в таблице.
KerberosError — не удалось подготовить билет вызова.
"""

from __future__ import annotations

from collections.abc import Sequence

from boba.connection_broker.params import ConnectionParams
from boba.connection_broker.tickets import CredentialsRef
from boba.connection_broker.user_connections import ArmedConnections, StoreRef, TypesRef
from boba.identity.context import CallContexts
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import StaticConfig

__all__ = ["StandUserConnections"]


class StandUserConnections:
    """Собирает ConnectionParams из реального хранилища и credentials стенда."""

    def __init__(
        self,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        types_ref: TypesRef,
        contexts: CallContexts,
    ) -> None:
        resolver = ArmedConnections(store_ref, credentials_ref, contexts)
        self._params = ConnectionParams(types_ref, contexts, StaticConfig(), resolver)

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._params.bind_all(tools)
