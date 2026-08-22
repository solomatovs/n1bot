"""Свежий kerberos-тикет к моменту, когда конфиг уезжает в песочницу.

Тело инструмента работает готовым тикетом: keytab остаётся у приложения, и
выпустить новый TGT внутри песочницы нечем. Значит момент обновления один —
перед вызовом, на хосте, по тем же конфигам соединений, что уедут внутрь.

Ошибки: KerberosError — тикет не выпущен, вызов начинать нечем.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody
from boba.krb import KeytabConfig, KeytabCredentials

__all__ = ["KerberosTickets"]

logger = logging.getLogger(__name__)


class KerberosTickets:
    """Обвязка секции: тикеты её соединений обновляются перед каждым вызовом."""

    class _Hooks(CallHooks[None]):
        def __init__(self, credentials: Sequence[KeytabCredentials]) -> None:
            self._credentials = tuple(credentials)

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> None:
            for creds in self._credentials:
                creds.ensure()

    @classmethod
    def bind_all(cls, tools: Sequence[BaseTool], configs: Sequence[object]) -> None:
        """Ставит обновление на инструменты, чьи конфиги несут keytab-креды."""
        keytabs = list(cls.keytabs_of(configs))
        if not keytabs:
            return

        credentials: list[KeytabCredentials] = []
        for config in keytabs:
            credentials.append(KeytabCredentials(config))

        names: list[str] = []
        for config in keytabs:
            names.append(config.principal)

        logger.info("kerberos tickets refreshed before each call: %s", ", ".join(names))

        hooks = cls._Hooks(credentials)
        ToolBody.hook_all(tools, hooks)

    @classmethod
    def keytabs_of(cls, values: Sequence[object]) -> Iterator[KeytabConfig]:
        """Keytab-креды из конфигов инструмента, включая вложенные профили."""
        seen: set[str] = set()
        for value in values:
            for config in cls._walk(value):
                if config.ccache in seen:
                    continue

                seen.add(config.ccache)
                yield config

    @classmethod
    def _walk(cls, value: object) -> Iterator[KeytabConfig]:
        if isinstance(value, KeytabConfig):
            yield value
            return

        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                yield from cls._walk(getattr(value, name))
            return

        if isinstance(value, dict):
            for nested in value.values():
                yield from cls._walk(nested)
            return

        if isinstance(value, (list, tuple)):
            for nested in value:
                yield from cls._walk(nested)
