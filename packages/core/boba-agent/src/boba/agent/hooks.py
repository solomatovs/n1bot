"""Хук `PreToolUse`: внешний обработчик решает судьбу вызова инструмента.

Перед вызовом инструмента агент берёт группы обработчиков из настроек,
чей матчер подходит к имени инструмента у модели (полное имя
`mcp__<сервер>__<имя>` для инструмента сервера MCP, собственное имя — для
своего инструмента; форму держит McpToolNames),
шлёт каждому `POST` с JSON события и применяет ответ: `allow` пропускает
вызов, `updatedInput` заменяет его аргументы целиком, `deny` снимает вызов
с причиной для модели, `ask` переводит вызов в вопрос наружу. Молчание,
сетевая ошибка, не-2xx и нечитаемое тело — отказ обработчика; его судьбу
решает `on_failure`: пропустить вызов с текущими аргументами или
заблокировать. Запись `tool_use` в истории хранит аргументы, которые
назвала модель; подмену видит только сервер инструментов.

Событие повторяет форму хуков настроек: `hook_event_name`, `session_id`,
`cwd`, `permission_mode`, `tool_name`, `tool_input`, `tool_use_id`.
Расширение — два поля сверх формы: `tool_schema` (входная JSON-схема
инструмента) и `mcp_server` (имя сервера MCP и объявленные им
возможности); по ним обработчик решает, какие аргументы и чем запечатать.

Транспорт — порт HookTransport: ядро знает только запрос и ответ, клиент
HTTP живёт у входа процесса.

Ошибки:
HookTransportError — выпускает реализация порта: обработчик недоступен,
    срок вышел, ответ не дочитан; исполнитель хука превращает её в отказ
    обработчика по `on_failure`.
ToolStopped — ход отменён, пока обработчик отвечал.
ValueError — матчер не компилируется как регулярное выражение или срок
    обработчика не положительный.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from boba.agent.records import PermissionMode
from boba.cancellation import RunCancellation, ToolStopped
from boba.toolkit.mcp_names import McpToolNames

logger = logging.getLogger(__name__)

__all__ = [
    "HookAllowed",
    "HookAsked",
    "HookBlocked",
    "HookDecision",
    "HookDenied",
    "HookEventName",
    "HookFailure",
    "HookGroup",
    "HookMatcher",
    "HookReading",
    "HookReplies",
    "HookReply",
    "HookReplyBody",
    "HookReplyError",
    "HookRequest",
    "HookSpecificOutput",
    "HookTransport",
    "HookTransportError",
    "HookVerdict",
    "HookedCall",
    "HttpHookSpec",
    "McpServerInfo",
    "PreToolUseEvent",
    "PreToolUseHooks",
]


class HookTransportError(Exception):
    """Обработчик не ответил: соединение, срок или обрыв тела."""


class HookReplyError(Exception):
    """Обработчик ответил, но ответ не годится: статус не 2xx, тело не
    JSON-объект или не проходит форму."""


class HookEventName(StrEnum):
    """События хуков, которые исполняет агент."""

    PRE_TOOL_USE = "PreToolUse"


class HookFailure(StrEnum):
    """Что делать, если обработчик не ответил: пропустить или заблокировать."""

    CONTINUE = "continue"
    BLOCK = "block"


class HookDecision(StrEnum):
    """Решение обработчика о вызове."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class MatchEverything(StrEnum):
    """Матчеры, которые подходят к любому инструменту."""

    STAR = "*"
    EMPTY = ""


class HookMatcher:
    """Матчер имени инструмента по правилам формы хуков.

    Пустая строка и `*` подходят ко всему; строка только из букв, цифр,
    `_`, `-`, пробелов, `,` и `|` — точное имя или список имён через `|`
    либо `,`; любая другая строка — регулярное выражение без якорей,
    которое ищется в полном имени инструмента. Создаётся при переводе
    настроек в группы хуков.
    """

    PLAIN: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\- ,|]*$")
    LIST_SEPARATORS: ClassVar[re.Pattern[str]] = re.compile(r"[|,]")

    def __init__(self, pattern: str) -> None:
        """Ошибки:
        ValueError — строка не компилируется как регулярное выражение.
        """
        self._pattern = pattern
        self._names: frozenset[str] = frozenset()
        self._regex: re.Pattern[str] | None = None
        if pattern in (MatchEverything.STAR, MatchEverything.EMPTY):
            return

        if self.PLAIN.match(pattern):
            self._names = frozenset(self._listed(pattern))
            return

        try:
            self._regex = re.compile(pattern)
        except re.error as exc:
            msg = f"hook matcher '{pattern}' is not a valid regular expression: {exc}"
            raise ValueError(msg) from exc

    @property
    def pattern(self) -> str:
        return self._pattern

    def matches(self, name: str) -> bool:
        if self._regex is not None:
            return self._regex.search(name) is not None

        if self._names:
            return name in self._names

        return True

    def _listed(self, pattern: str) -> Iterator[str]:
        for piece in self.LIST_SEPARATORS.split(pattern):
            cleaned = piece.strip()
            if not cleaned:
                continue

            yield cleaned


@dataclass(frozen=True)
class HttpHookSpec:
    """Один обработчик: адрес, заголовки как есть, срок и судьба отказа."""

    url: str
    headers: Mapping[str, str]
    timeout_sec: float
    on_failure: HookFailure

    def __post_init__(self) -> None:
        if self.timeout_sec <= 0:
            msg = f"hook {self.url}: timeout must be positive, got {self.timeout_sec}"
            raise ValueError(msg)


@dataclass(frozen=True)
class HookGroup:
    """Группа обработчиков под одним матчером."""

    matcher: HookMatcher
    hooks: Sequence[HttpHookSpec]


class HookWire(BaseModel):
    """Основа моделей события: ключи в snake_case, как в форме хуков."""

    model_config = ConfigDict(frozen=True)


class McpServerInfo(HookWire):
    """Расширение события: сервер MCP инструмента и его возможности."""

    name: str
    features: Mapping[str, Mapping[str, object]] = Field(default_factory=dict)


class PreToolUseEvent(HookWire):
    """Тело `POST` обработчику. Поля формы хуков: `hook_event_name`,
    `session_id`, `cwd`, `permission_mode`, `tool_name`, `tool_input`,
    `tool_use_id`. Расширение: `tool_schema` — входная JSON-схема
    инструмента; `mcp_server` — сервер MCP и его возможности, у
    собственного инструмента агента поля нет."""

    hook_event_name: Literal[HookEventName.PRE_TOOL_USE] = HookEventName.PRE_TOOL_USE
    session_id: str
    cwd: str
    permission_mode: PermissionMode
    tool_name: str
    tool_input: Mapping[str, Any]
    tool_use_id: str
    tool_schema: Mapping[str, Any]
    mcp_server: McpServerInfo | None = None


class HookReplyWire(BaseModel):
    """Основа моделей ответа: ключи в camelCase, чужие поля пропускаются."""

    model_config = ConfigDict(
        frozen=True, alias_generator=to_camel, populate_by_name=True, extra="ignore"
    )


class HookSpecificOutput(HookReplyWire):
    """`hookSpecificOutput` ответа: решение, причина, замена аргументов."""

    hook_event_name: str = ""
    permission_decision: HookDecision | None = None
    permission_decision_reason: str = ""
    updated_input: Mapping[str, Any] | None = None


class HookReplyBody(HookReplyWire):
    """JSON-объект ответа обработчика; без `hookSpecificOutput` — решения нет."""

    hook_specific_output: HookSpecificOutput | None = None


@dataclass(frozen=True)
class HookRequest:
    """Запрос обработчику: `POST` JSON тела на адрес с заголовками."""

    url: str
    headers: Mapping[str, str]
    body: Mapping[str, Any]
    timeout_sec: float


@dataclass(frozen=True)
class HookReply:
    """Ответ обработчика: статус и тело текстом."""

    status: int
    body: str

    OK_FROM: ClassVar[int] = 200
    OK_BELOW: ClassVar[int] = 300

    def ok(self) -> bool:
        return self.OK_FROM <= self.status < self.OK_BELOW


class HookTransport(Protocol):
    """Порт доставки запроса обработчику."""

    @abstractmethod
    async def post(self, request: HookRequest) -> HookReply:
        """Ошибки:
        HookTransportError — обработчик недоступен, срок вышел, ответ не дочитан.
        """
        ...


@dataclass(frozen=True)
class HookReading:
    """Прочитанный ответ: решение (None — обработчик решения не дал),
    причина и замена аргументов, если она есть."""

    decision: HookDecision | None
    reason: str
    updated_input: Mapping[str, Any] | None


class HookReplies:
    """Чтение ответа обработчика по форме хуков: пустое тело 2xx — молчаливое
    согласие, JSON-объект — решение из `hookSpecificOutput`."""

    BODY_PREVIEW: ClassVar[int] = 200

    def read(self, reply: HookReply) -> HookReading:
        """Ошибки:
        HookReplyError — статус не 2xx, тело не JSON-объект, форма нарушена.
        """
        if not reply.ok():
            msg = (
                f"expected a 2xx status with a JSON body, got {reply.status}: "
                f"{reply.body[: self.BODY_PREVIEW]!r}"
            )
            raise HookReplyError(msg)

        if not reply.body.strip():
            return HookReading(decision=None, reason="", updated_input=None)

        try:
            body = HookReplyBody.model_validate_json(reply.body)
        except ValidationError as exc:
            msg = (
                "expected a JSON object in the form of hookSpecificOutput, got "
                f"{reply.body[: self.BODY_PREVIEW]!r}: {exc}"
            )
            raise HookReplyError(msg) from exc

        output = body.hook_specific_output
        if output is None:
            return HookReading(decision=None, reason="", updated_input=None)

        return HookReading(
            decision=output.permission_decision,
            reason=output.permission_decision_reason,
            updated_input=output.updated_input,
        )


@dataclass(frozen=True)
class HookedCall:
    """Вызов глазами хука: сессия, режим, имя у модели (у инструмента
    сервера MCP — полное), идентификатор, текущие аргументы и входная схема
    инструмента."""

    session_id: str
    permission_mode: PermissionMode
    tool_name: str
    tool_use_id: str
    arguments: Mapping[str, Any]
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class HookAllowed:
    """Вызов идёт с этими аргументами."""

    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class HookAsked:
    """Обработчик просит спросить снаружи; аргументы уже с его заменами."""

    arguments: Mapping[str, Any]
    reason: str


@dataclass(frozen=True)
class HookDenied:
    """Обработчик снял вызов."""

    reason: str


@dataclass(frozen=True)
class HookBlocked:
    """Обработчик не ответил, а его `on_failure` блокирует вызов."""

    url: str
    error: str


HookVerdict: TypeAlias = HookAllowed | HookAsked | HookDenied | HookBlocked


class Posting:
    """Один запрос обработчику, который отмена хода может оборвать."""

    def __init__(self, transport: HookTransport, request: HookRequest) -> None:
        self._transport = transport
        self._request = request
        self._task: asyncio.Future[HookReply] | None = None

    def abort(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def reply(self) -> HookReply:
        """Ошибки:
        HookTransportError — обработчик недоступен или не ответил в срок.
        """
        self._task = asyncio.ensure_future(self._transport.post(self._request))
        try:
            return await self._task
        except HookTransportError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            msg = f"POST {self._request.url}: unexpected transport failure: {exc}"
            raise HookTransportError(msg) from exc


class PreToolUseHooks:
    """Исполнитель хука `PreToolUse` на вызов инструмента.

    Создаёт вход процесса из групп настроек, транспорта и серверов MCP
    сессии по именам; зовёт ToolRunner после решения о разрешении и до
    отправки вызова серверу. Сервер инструмента для события находится
    разбором полного имени (McpToolNames). Обработчики подходящих групп идут
    по порядку: каждый видит аргументы с заменами предыдущих; `deny` и
    блокирующий отказ обрывают цепочку.
    """

    def __init__(
        self,
        groups: Sequence[HookGroup],
        transport: HookTransport,
        servers: Mapping[str, McpServerInfo],
        cwd: str,
    ) -> None:
        self._groups = list(groups)
        self._transport = transport
        self._servers = dict(servers)
        self._cwd = cwd
        self._replies = HookReplies()
        self._names = McpToolNames()

    def matching(self, full_name: str) -> Iterator[HttpHookSpec]:
        """Обработчики групп, чей матчер подходит к полному имени."""
        for group in self._groups:
            if not group.matcher.matches(full_name):
                continue

            yield from group.hooks

    async def before_call(
        self, call: HookedCall, cancellation: RunCancellation
    ) -> HookVerdict:
        """Ошибки:
        ToolStopped — ход отменён, пока обработчик отвечал.
        """
        full_name = call.tool_name
        arguments = call.arguments
        asked = False
        reason = ""
        for hook in self.matching(full_name):
            event = self._event(call, full_name, arguments)
            try:
                reading = await self._reading(hook, event, cancellation)
            except (HookTransportError, HookReplyError) as exc:
                if hook.on_failure is HookFailure.BLOCK:
                    logger.warning(
                        "hooks: PreToolUse %s failed for %s, the call is blocked: %s",
                        hook.url,
                        full_name,
                        exc,
                    )
                    return HookBlocked(url=hook.url, error=str(exc))

                logger.warning(
                    "hooks: PreToolUse %s failed for %s, the call goes on: %s",
                    hook.url,
                    full_name,
                    exc,
                )
                continue

            if reading.decision is HookDecision.DENY:
                logger.info("hooks: PreToolUse %s denied %s", hook.url, full_name)
                return HookDenied(reason=reading.reason)

            if reading.updated_input is not None:
                changed = self._changed_keys(arguments, reading.updated_input)
                logger.info(
                    "hooks: PreToolUse %s replaced %s of %s",
                    hook.url,
                    changed,
                    full_name,
                )
                arguments = reading.updated_input

            if reading.decision is HookDecision.ASK:
                asked = True
                reason = reading.reason

        if asked:
            return HookAsked(arguments=arguments, reason=reason)

        return HookAllowed(arguments=arguments)

    def _event(
        self, call: HookedCall, full_name: str, arguments: Mapping[str, Any]
    ) -> PreToolUseEvent:
        server: McpServerInfo | None = None
        if parsed := self._names.parse(full_name):
            server = self._servers.get(parsed.server)

        return PreToolUseEvent(
            session_id=call.session_id,
            cwd=self._cwd,
            permission_mode=call.permission_mode,
            tool_name=full_name,
            tool_input=arguments,
            tool_use_id=call.tool_use_id,
            tool_schema=call.schema,
            mcp_server=server,
        )

    async def _reading(
        self,
        hook: HttpHookSpec,
        event: PreToolUseEvent,
        cancellation: RunCancellation,
    ) -> HookReading:
        """Ошибки:
        HookTransportError — обработчик недоступен или не ответил в срок.
        HookReplyError — ответ не годится.
        ToolStopped — ход отменён во время запроса.
        """
        request = HookRequest(
            url=hook.url,
            headers=hook.headers,
            body=event.model_dump(mode="json", exclude_none=True),
            timeout_sec=hook.timeout_sec,
        )
        posting = Posting(self._transport, request)
        try:
            with cancellation.abort_with(posting.abort):
                reply = await posting.reply()
        except asyncio.CancelledError as exc:
            if not cancellation.cancelled:
                raise

            msg = (
                f"the run was stopped while the PreToolUse hook {hook.url} was "
                f"answering for {event.tool_name}"
            )
            raise ToolStopped(msg) from exc

        return self._replies.read(reply)

    @staticmethod
    def _changed_keys(
        before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> Sequence[str]:
        """Имена аргументов, которые обработчик заменил; значения в лог не идут."""
        changed: list[str] = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                changed.append(key)

        return changed
