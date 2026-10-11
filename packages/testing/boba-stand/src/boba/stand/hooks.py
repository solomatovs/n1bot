"""Стенд обработчика хука `PreToolUse`: HTTP-приложение, которое отвечает
агенту по форме хуков и запоминает, что получило.

Нужен тестам исполнителя хука в агенте и служит образцом для обработчика
чата: событие читается моделью PreToolUseEvent ядра, ответ строится
моделью HookReplyBody. Поведение задают правила HookStandRules по полному
имени инструмента: запечатать аргументы-соединения по `contentMediaType`
из `tool_schema`, заменить названные аргументы, отказать с причиной,
попросить вопрос наружу, промолчать дольше срока, ответить ошибкой или
не-JSON. Принятые события и заголовки лежат в `received`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from boba.agent.hooks import (
    HookDecision,
    HookEventName,
    HookReplyBody,
    HookSpecificOutput,
    PreToolUseEvent,
)
from boba.stand.agent import StandMedia
from boba.stand.ui.stand import free_port

__all__ = [
    "HookStandApp",
    "HookStandRoute",
    "HookStandRules",
    "HookStandServer",
    "ReceivedHook",
]


class HookStandRoute(StrEnum):
    """Маршруты стенда."""

    PRE_TOOL_USE = "/hooks/pre-tool-use"
    HEALTH = "/health"


class SchemaKey(StrEnum):
    """Ключи JSON-схемы, по которым стенд находит аргументы-соединения."""

    PROPERTIES = "properties"
    CONTENT_MEDIA_TYPE = "contentMediaType"


@dataclass(frozen=True)
class HookStandRules:
    """Поведение стенда по полному имени инструмента (`mcp__<сервер>__<имя>`)."""

    seal_prefix: str = "sealed:"
    """Чем помечается запечатанное значение аргумента-соединения."""

    replacements: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Инструмент → аргументы, которые стенд подменяет в `updatedInput`."""

    denied: Mapping[str, str] = field(default_factory=dict)
    """Инструмент → причина отказа."""

    asked: Mapping[str, str] = field(default_factory=dict)
    """Инструмент → причина, с которой стенд просит спросить снаружи."""

    silent: frozenset[str] = frozenset()
    """Инструменты, на которые стенд отвечает только через `hang_sec`."""

    failing: frozenset[str] = frozenset()
    """Инструменты, на которые стенд отвечает статусом 500."""

    garbled: frozenset[str] = frozenset()
    """Инструменты, на которые стенд отвечает 200 с текстом вместо JSON."""

    hang_sec: float = 5.0


@dataclass(frozen=True)
class ReceivedHook:
    """Что стенд получил: событие и заголовки запроса в нижнем регистре."""

    event: PreToolUseEvent
    headers: Mapping[str, str]


class HookStandApp:
    """ASGI-приложение обработчика: одно событие — один ответ по правилам."""

    FAILURE_STATUS: ClassVar[int] = 500
    FAILURE_TEXT: ClassVar[str] = "the stand handler failed"
    GARBLED_TEXT: ClassVar[str] = "this is not json"
    TEXT_MEDIA: ClassVar[str] = "text/plain"

    def __init__(self, rules: HookStandRules) -> None:
        self._rules = rules
        self.received: list[ReceivedHook] = []

    def asgi(self) -> FastAPI:
        app = FastAPI()

        @app.get(HookStandRoute.HEALTH.value)
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post(HookStandRoute.PRE_TOOL_USE.value)
        async def pre_tool_use(request: Request) -> Response:
            event = PreToolUseEvent.model_validate(await request.json())
            self.received.append(
                ReceivedHook(event=event, headers=dict(request.headers))
            )

            return await self._answer(event)

        return app

    async def _answer(self, event: PreToolUseEvent) -> Response:
        rules = self._rules
        name = event.tool_name
        if name in rules.silent:
            await asyncio.sleep(rules.hang_sec)
            return Response(status_code=200)

        if name in rules.failing:
            return Response(
                self.FAILURE_TEXT,
                status_code=self.FAILURE_STATUS,
                media_type=self.TEXT_MEDIA,
            )

        if name in rules.garbled:
            return Response(self.GARBLED_TEXT, media_type=self.TEXT_MEDIA)

        if name in rules.denied:
            return self._json(HookDecision.DENY, rules.denied[name], None)

        updated = self._updated(event)
        if name in rules.asked:
            return self._json(HookDecision.ASK, rules.asked[name], updated)

        return self._json(HookDecision.ALLOW, "", updated)

    def _json(
        self,
        decision: HookDecision,
        reason: str,
        updated: Mapping[str, Any] | None,
    ) -> Response:
        body = HookReplyBody(
            hook_specific_output=HookSpecificOutput(
                hook_event_name=HookEventName.PRE_TOOL_USE.value,
                permission_decision=decision,
                permission_decision_reason=reason,
                updated_input=updated,
            )
        )

        return JSONResponse(
            body.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    def _updated(self, event: PreToolUseEvent) -> Mapping[str, Any] | None:
        """Аргументы после запечатывания и подмен; None — менять нечего."""
        updated = dict(event.tool_input)
        for key in self._connection_keys(event.tool_schema):
            value = updated.get(key)
            if not isinstance(value, str):
                continue

            updated[key] = f"{self._rules.seal_prefix}{value}"

        replacement = self._rules.replacements.get(event.tool_name)
        if replacement is not None:
            updated.update(replacement)

        if updated == dict(event.tool_input):
            return None

        return updated

    @staticmethod
    def _connection_keys(schema: Mapping[str, Any]) -> list[str]:
        """Аргументы, чей `contentMediaType` называет соединение."""
        keys: list[str] = []
        properties = schema.get(SchemaKey.PROPERTIES.value)
        if not isinstance(properties, Mapping):
            return keys

        for key, spec in properties.items():
            if not isinstance(spec, Mapping):
                continue

            media = spec.get(SchemaKey.CONTENT_MEDIA_TYPE.value)
            if not isinstance(media, str):
                continue

            if media.startswith(StandMedia.CONNECTION_PREFIX.value):
                keys.append(str(key))

        return keys


class HookStandServer:
    """HookStandApp под uvicorn в текущем цикле событий на свободном порту."""

    HOST: ClassVar[str] = "127.0.0.1"

    def __init__(self, rules: HookStandRules) -> None:
        self.app = HookStandApp(rules)
        self.port = free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app.asgi(),
                host=self.HOST,
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.HOST}:{self.port}{HookStandRoute.PRE_TOOL_USE.value}"

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task
