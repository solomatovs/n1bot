"""Хук `PreToolUse` у входа процесса: транспорт на httpx и перевод секции
`hooks` действующих настроек в модель ядра (boba.agent.hooks).

Заголовки обработчика уходят из файла настроек как есть: подстановок из
окружения нет, секреты в заголовках живут только в файле. Клиент httpx
создаётся один на процесс и не читает окружение (`trust_env=False`).

Ошибки:
HookTransportError — обработчик недоступен, срок вышел, ответ не дочитан
    (контракт порта HookTransport ядра).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import httpx

from boba.agent.hooks import (
    HookGroup,
    HookMatcher,
    HookReply,
    HookRequest,
    HookTransport,
    HookTransportError,
    HttpHookSpec,
    PreToolUseHooks,
    ToolOrigins,
)
from boba.cli.settings import EffectiveSettings, HookEvent, HookMatcherGroup

__all__ = ["HookWiring", "HttpHookTransport"]


class HttpHookTransport(HookTransport):
    """Реализация HookTransport на httpx: `POST` JSON тела с заголовками
    обработчика и сроком из его настроек. Создаёт вход процесса."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def post(self, request: HookRequest) -> HookReply:
        try:
            response = await self._client.post(
                request.url,
                json=dict(request.body),
                headers=dict(request.headers),
                timeout=request.timeout_sec,
            )
        except httpx.TimeoutException as exc:
            msg = (
                f"POST {request.url}: no answer within {request.timeout_sec} s: "
                f"{type(exc).__name__}: {exc}"
            )
            raise HookTransportError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"POST {request.url}: request failed: {type(exc).__name__}: {exc}"
            raise HookTransportError(msg) from exc

        return HookReply(status=response.status_code, body=response.text)

    async def aclose(self) -> None:
        await self._client.aclose()


class HookWiring:
    """Собирает исполнитель хука `PreToolUse` на сессию из действующих
    настроек: группы матчеров и обработчиков — в модель ядра, транспорт
    общий на процесс, происхождение инструментов и рабочий каталог — у
    сессии. Создаёт вход процесса, зовёт сборка сессии."""

    def __init__(self, transport: HookTransport) -> None:
        self._transport = transport

    def hooks(
        self, settings: EffectiveSettings, origins: ToolOrigins, cwd: Path
    ) -> PreToolUseHooks | None:
        """Исполнитель хука для сессии с рабочим каталогом cwd; None — в
        настройках нет групп `PreToolUse`."""
        groups = settings.hooks.get(HookEvent.PRE_TOOL_USE)
        if not groups:
            return None

        return PreToolUseHooks(
            list(self._groups(groups)), self._transport, origins, str(cwd)
        )

    def _groups(self, groups: Sequence[HookMatcherGroup]) -> Iterator[HookGroup]:
        for group in groups:
            specs: list[HttpHookSpec] = []
            for hook in group.hooks:
                specs.append(
                    HttpHookSpec(
                        url=hook.url,
                        headers=dict(hook.headers),
                        timeout_sec=hook.timeout,
                        on_failure=hook.on_failure,
                    )
                )

            yield HookGroup(matcher=HookMatcher(group.matcher), hooks=specs)
