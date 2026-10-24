"""Инструменты агента и исполнение вызовов одного ответа модели.

Порт сервера инструментов — `ToolServer`: карточки для модели и вызовы
`tools/call` в форме MCP; реализует его вход процесса над серверами MCP
сессии. Собственные инструменты агента (вопрос пользователю) живут рядом
с инструментами сервера и зовутся по имени (план, раздел 5.12) и отвечают
тем же результатом tools/call. Инструменты сервера отложены, кроме
названных администратором в списке «всегда загружать»; собственный
инструмент откладывается своим признаком. Отложенный модель видит одним
именем и загружает инструментом ToolSearch (docs/prompt_assembly/specs/
07_system_tools.md, раздел 2.3). ToolRunner ведёт пачку вызовов по разделу
5.13, шаг 8: каждый вызов проходит разрешения (раздел 5.6), вопросы
наружу идут по очереди; разрешённые вызовы уходят серверу одним
`submit`; результаты собираются в порядке вызовов; отмена посреди пачки
даёт результаты-отказы по таблице раздела 5.14. Вызов отложенного
инструмента, который модель не загрузила, — ошибка вызова с подсказкой.
Результат длиннее предела обрезается, пустой заменяется заглушкой.

Ошибки:
ToolsError — имя инструмента предложено дважды: собственным инструментом и
    сервером либо двумя серверами; с таким набором агент не стартует.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.agent.attachments import Announced, ConnectedServer, SnapshotTool
from boba.agent.control import (
    AllowAnswer,
    CanUseToolRequest,
    ControlQuestions,
    DenyAnswer,
    PermissionAnswer,
    QuestionWithdrawnError,
)
from boba.agent.events import (
    ControlCancelEvent,
    ControlRequestEvent,
    ToolStartedEvent,
)
from boba.agent.ids import Clock
from boba.agent.mcp import ToolCard, ToolHints, ToolReplies, ToolReply
from boba.agent.permissions import (
    Allowed,
    Asked,
    AutoClassifier,
    Classified,
    Denied,
    PermissionPolicy,
    PermissionState,
    StaticRules,
)
from boba.agent.profile import ModelProfile, TurnLimits
from boba.agent.records import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleOperation,
    PermissionRuleRecord,
    ToolDenialKind,
    ToolUseBlock,
)
from boba.agent.session import SessionState
from boba.agent.tags import AgentTag
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.agent.writer import RecordKeeper
from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.core.validation import ValidationText

logger = logging.getLogger(__name__)

__all__ = [
    "AgentTools",
    "Approved",
    "AskOption",
    "AskOutcome",
    "AskPick",
    "AskQuestion",
    "AskUserQuestionInput",
    "AskUserQuestionOutput",
    "AskUserQuestionTool",
    "BatchOutcome",
    "CallResult",
    "DeferredCatalog",
    "McpServerSet",
    "OwnTool",
    "PlannedCall",
    "Questioner",
    "ToolMatcher",
    "ToolRunner",
    "ToolSearchInput",
    "ToolSearchOutput",
    "ToolSearchTool",
    "ToolServer",
    "ToolTexts",
    "ToolsError",
]


class ToolsError(Exception):
    """Набор инструментов противоречив: имена совпали."""


class ToolServer(Protocol):
    """Порт сервера инструментов для агента.

    Агент знает исполнение только через него: берёт карточки инструментов,
    которые сервер отдаёт модели, и шлёт вызовы. Вызов — блок `tool_use`
    ответа модели с аргументами, прошедшими разрешения; итог — ответ
    tools/call как пришёл (ToolReply). Реализует вход процесса агента над
    серверами MCP сессии и стенд тестов.
    """

    @abstractmethod
    def tools(self) -> Sequence[ToolCard]:
        """Карточки инструментов, которые сервер отдаёт модели."""

    @abstractmethod
    async def submit(
        self, calls: Sequence[ToolUseBlock]
    ) -> Sequence[asyncio.Future[ToolReply]]:
        """Принять вызовы одного ответа модели; итог каждого — своё ожидание.

        Ожидания идут в порядке вызовов и кончаются независимо: быстрый
        вызов отвечает, не дожидаясь медленного соседа. Отмена ожидания
        останавливает его вызов. Сбой вызова — ответ с isError, а не
        исключение; наверх идёт только отмена.
        """


@dataclass(frozen=True)
class PlannedCall:
    """Вызов из записанного ответа: блок `tool_use` и uuid его записи."""

    block: ToolUseBlock
    assistant_uuid: str

    @property
    def name(self) -> str:
        return self.block.name


@dataclass(frozen=True)
class CallResult:
    """Результат вызова для записи `user`: текст модели, ответ tools/call
    (его JSON — `toolUseResult`) и причина отказа, если это отказ. Вызов,
    оборванный концом хода, ответа не получил: reply None, для модели это
    ошибка. references — инструменты, которые результат загрузил (ToolSearch):
    они ложатся в результат ссылками tool_reference."""

    call: PlannedCall
    text: str
    reply: ToolReply | None
    denial: ToolDenialKind | None = None
    references: Sequence[str] = ()

    @property
    def is_error(self) -> bool:
        if self.reply is None:
            return True

        return self.reply.result.is_error

    @property
    def stored(self) -> Any:
        """Значение `toolUseResult`: ответ как пришёл; None — ответа не было."""
        if self.reply is None:
            return None

        return self.reply.raw


@dataclass(frozen=True)
class BatchOutcome:
    """Итог пачки: результаты в порядке вызовов и причина отмены, если была."""

    results: Sequence[CallResult]
    aborted: StopReason | None


@dataclass(frozen=True)
class Approved:
    """Вызов прошёл разрешения: эти аргументы уходят серверу."""

    arguments: Mapping[str, Any]


class Questioner:
    """Вопрос наружу в рамках одного хода: регистрирует его в реестре,
    отдаёт событием, ждёт ответа; снятый отменой вопрос уходит событием
    снятия. Создаёт ToolRunner на пачку вызовов."""

    def __init__(
        self,
        questions: ControlQuestions,
        keeper: RecordKeeper,
        cancellation: RunCancellation,
    ) -> None:
        self._questions = questions
        self._keeper = keeper
        self._cancellation = cancellation

    async def ask(self, request: CanUseToolRequest) -> PermissionAnswer:
        """Ошибки:
        QuestionWithdrawnError — ход отменён до ответа.
        """
        request_id = self._questions.open(request)
        await self._keeper.emit(
            ControlRequestEvent(request_id=request_id, request=request)
        )
        self._keeper.session.enter(SessionState.WAITING)
        try:
            return await self._questions.wait(request_id, self._cancellation)
        except QuestionWithdrawnError:
            await self._keeper.emit(ControlCancelEvent(request_id=request_id))
            raise
        finally:
            self._keeper.session.enter(SessionState.TURN)


class OwnTool(Protocol):
    """Собственный инструмент агента: описан как инструмент сервера, но
    исполняется в процессе агента."""

    @property
    def name(self) -> str: ...

    @property
    def hints(self) -> ToolHints: ...

    @property
    def deferred(self) -> bool:
        """Отложен ли инструмент: модель видит одно имя и загружает
        определение инструментом ToolSearch."""
        ...

    def card(self) -> SnapshotTool: ...

    async def run(
        self, call: PlannedCall, arguments: Mapping[str, Any], questioner: Questioner
    ) -> CallResult:
        """Ошибки:
        QuestionWithdrawnError — ход отменён, пока инструмент ждал ответа.
        """
        ...


class McpServerSet(Protocol):
    """Порт серверов MCP сессии для хода.

    В начале хода ядро просит освежить то, что устарело, — описание
    сервера, списки инструментов, промптов и ресурсов по сроку свежести
    ответа сервера, — и спрашивает, какие серверы подключены и с какими
    инструкциями: из этого и из карточек ToolServer ход объявляет модели
    разницу инструментов и инструкций. Реализует вход процесса агента над
    серверами своей сессии; отказ сервера реализация не выпускает: сервер,
    который не ответил, перестаёт быть подключённым.
    """

    @abstractmethod
    async def refreshed(self) -> None:
        """Устаревшие ответы серверов перечитаны."""

    @abstractmethod
    def connected(self) -> Sequence[ConnectedServer]:
        """Подключённые серверы с их инструкциями."""


class AskOption(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    label: str = Field(
        description=(
            "Текст варианта, который пользователь видит и выбирает: 1–5 слов, "
            "ясно называющих выбор."
        )
    )
    description: str = Field(
        description=(
            "Что значит вариант и что будет, если его выбрать: компромиссы и "
            "последствия."
        )
    )
    preview: str = Field(
        default="",
        description=(
            "Предпросмотр варианта, который показывается, пока вариант в фокусе:\n"
            "   - макет интерфейса, фрагмент кода, вариант схемы или конфигурации\n"
            "       когда пользователю нужно сравнить варианты глазами\n"
            "   - пусто — простой выбор, где хватает метки и описания\n"
            "Только у вопроса с одиночным выбором; формат — в описании "
            "инструмента."
        ),
    )


class AskPick(BaseModel):
    """Выбор пользователя по одному вопросу: метки вариантов в порядке
    отметки и свой текст «Other». other None — «Other» не выбран, пустая
    строка — выбран без текста."""

    model_config = ConfigDict(frozen=True)

    labels: Sequence[str] = ()
    other: str | None = None

    def typed(self) -> bool:
        """Ответ несёт свой текст пользователя, а не только метки."""
        if self.other is None:
            return False

        return bool(self.other.strip())


class AskQuestion(BaseModel):
    """Вопрос пользователю: текст, метка вкладки и варианты ответа."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    OTHER: ClassVar[str] = "Other"
    """Вариант со своим текстом: его дорисовывает клиент, модель его не пишет."""
    JOIN: ClassVar[str] = ", "

    question: str = Field(
        description=(
            "Полный текст вопроса: ясный, конкретный, с вопросительным знаком в "
            "конце. Пример: «Какую библиотеку взять для форматирования дат?». "
            "Вопрос с несколькими ответами формулируется под них: «Какие "
            "возможности включить?»"
        )
    )
    header: str = Field(
        max_length=12,
        description=(
            "Короткая метка вопроса на его вкладке, не длиннее 12 знаков: "
            "«Вход», «Библиотека», «Подход»."
        ),
    )
    options: Sequence[AskOption] = Field(
        min_length=2,
        max_length=4,
        description=(
            "Варианты ответа, от 2 до 4: каждый — отдельный выбор, при одиночном "
            "выборе варианты исключают друг друга. Вариант «Other» со своим "
            "текстом интерфейс добавляет сам — в список его не включай."
        ),
    )
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description=(
            "Сколько вариантов может выбрать пользователь:\n"
            "   - false — один вариант\n"
            "   - true — несколько, когда варианты не исключают друг друга\n"
        ),
    )

    def labels(self) -> list[str]:
        labels: list[str] = []
        for option in self.options:
            labels.append(option.label)

        return labels

    def pick(self, reply: str) -> AskPick:
        """Выбор по ответу reply: метки через ", " в порядке отметки, свой
        текст последним; у одиночного выбора ответ — метка либо свой текст."""
        labels = self.labels()
        if not self.multi_select:
            if reply in labels:
                return AskPick(labels=(reply,))

            if reply == self.OTHER:
                return AskPick(other="")

            return AskPick(other=reply)

        known = [*labels, self.OTHER]
        picked: list[str] = []
        rest = reply
        while rest:
            label = self._leading(rest, known)
            if label is None:
                break

            picked.append(label)
            rest = rest[len(label) :]
            rest = rest.removeprefix(self.JOIN)

        chosen = [label for label in picked if label != self.OTHER]
        if rest:
            return AskPick(labels=chosen, other=rest)

        if self.OTHER in picked:
            return AskPick(labels=chosen, other="")

        return AskPick(labels=chosen)

    def _leading(self, rest: str, known: Sequence[str]) -> str | None:
        """Самая длинная метка, с которой начинается остаток ответа."""
        found: str | None = None
        for label in known:
            whole = rest == label
            leading = rest.startswith(label + self.JOIN)
            if not whole and not leading:
                continue

            if found is not None and len(found) >= len(label):
                continue

            found = label

        return found


class AskUserQuestionInput(BaseModel):
    """Ввод инструмента «вопрос пользователю»: тексты вопросов различаются,
    метки вариантов различаются внутри вопроса."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    questions: Sequence[AskQuestion] = Field(
        min_length=1,
        max_length=4,
        description="Вопросы пользователю, от 1 до 4.",
    )

    @model_validator(mode="after")
    def _unique(self) -> Self:
        texts: set[str] = set()
        for question in self.questions:
            labels = question.labels()
            if len(set(labels)) != len(labels):
                msg = (
                    f"option labels must be unique within each question, got "
                    f"{labels} for {question.question!r}"
                )
                raise ValueError(msg)

            if question.question in texts:
                msg = f"question texts must be unique, got {question.question!r} twice"
                raise ValueError(msg)

            texts.add(question.question)

        return self


class AskOutcome(StrEnum):
    """Исход вопроса пользователю: ответил, отказался, ответа нет."""

    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class AskUserQuestionOutput(BaseModel):
    """Итог вопроса пользователю для истории (`toolUseResult`) и ленты
    клиента: вопросы, ответы текстом модели, выбор по каждому вопросу и
    исход. Ключ ответов и выбора — текст вопроса."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    questions: Sequence[AskQuestion]
    answers: Mapping[str, str] = Field(default_factory=dict)
    picks: Mapping[str, AskPick] = Field(default_factory=dict)
    outcome: AskOutcome


class AskUserQuestionTool(OwnTool):
    """Инструмент «вопрос пользователю» (тексты — docs/prompt_assembly).

    Вопрос уходит наружу тем же `can_use_tool`, ответы приходят в
    `updated_input` под ключом `answers`: текст вопроса → ответ; отказ —
    DenyAnswer, ответ без ответов — вопрос снят без пользователя. Итог
    вызова — AskUserQuestionOutput. Создаёт агент."""

    NAME: ClassVar[str] = "AskUserQuestion"
    ANSWERS_KEY: ClassVar[str] = "answers"
    HINTS: ClassVar[ToolHints] = ToolHints(read_only=True, destructive=False)

    def __init__(self, templates: AgentTemplate, texts: ToolTexts) -> None:
        self._templates = templates
        self._texts = texts
        self._errors = ValidationText()

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def hints(self) -> ToolHints:
        return self.HINTS

    @property
    def deferred(self) -> bool:
        return False

    def card(self) -> SnapshotTool:
        return SnapshotTool(
            name=self.NAME,
            description=self._templates.read(AgentTemplateFile.ASK_USER_QUESTION),
            schema=AskUserQuestionInput.model_json_schema(by_alias=True),
        )

    async def run(
        self, call: PlannedCall, arguments: Mapping[str, Any], questioner: Questioner
    ) -> CallResult:
        try:
            asked = AskUserQuestionInput.model_validate(arguments)
        except ValidationError as exc:
            text = (
                f"The {self.NAME} call was not shown to the user: its arguments do "
                f"not match the tool schema: {self._errors.of(exc)}"
            )
            return self._texts.failed(call, text)

        shown = asked.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        request = CanUseToolRequest(
            tool_name=self.NAME, input=shown, tool_use_id=call.block.id
        )
        answer = await questioner.ask(request)
        if isinstance(answer, DenyAnswer):
            output = AskUserQuestionOutput(
                questions=asked.questions, outcome=AskOutcome.DECLINE
            )
            return self._texts.rejected(call, answer.message, self._dumped(output))

        answers = self._answers(answer, asked)
        if not answers:
            text = self._templates.read(AgentTemplateFile.NO_ANSWERS)
            output = AskUserQuestionOutput(
                questions=asked.questions, outcome=AskOutcome.CANCEL
            )
            return self._texts.answered(call, text, self._dumped(output))

        listed: list[str] = []
        picks: dict[str, AskPick] = {}
        template = AgentTemplateFile.ANSWERS
        for question in asked.questions:
            reply = answers.get(question.question)
            if reply is None:
                continue

            listed.append(f'"{question.question}"="{reply}"')
            pick = question.pick(reply)
            picks[question.question] = pick
            if pick.typed():
                template = AgentTemplateFile.ANSWERS_TYPED

        text = self._templates.read(template).format(answers=", ".join(listed))
        output = AskUserQuestionOutput(
            questions=asked.questions,
            answers=answers,
            picks=picks,
            outcome=AskOutcome.ACCEPT,
        )

        return self._texts.answered(call, text, self._dumped(output))

    def _answers(
        self, answer: AllowAnswer, asked: AskUserQuestionInput
    ) -> dict[str, str]:
        """Непустые ответы на заданные вопросы по их текстам."""
        updated = answer.updated_input
        if updated is None:
            return {}

        raw = updated.get(self.ANSWERS_KEY)
        if not isinstance(raw, Mapping):
            return {}

        answers: dict[str, str] = {}
        for question in asked.questions:
            reply = raw.get(question.question)
            if reply is None:
                continue

            text = str(reply)
            if not text.strip():
                continue

            answers[question.question] = text

        return answers

    @staticmethod
    def _dumped(output: AskUserQuestionOutput) -> dict[str, Any]:
        return output.model_dump(mode="json", by_alias=True, exclude_defaults=True)


class DeferredCatalog(Protocol):
    """Порт набора отложенных инструментов для ToolSearch: определения,
    среди которых ищет поиск. Реализует AgentTools."""

    @abstractmethod
    def deferred(self) -> Sequence[SnapshotTool]:
        """Определения отложенных инструментов сейчас."""


class AgentTools(DeferredCatalog):
    """Инструменты, которые видит модель: сервера и собственные, по имени.

    Создаёт агент при открытии сессии. Карточки инструментов сервера
    берутся у живого сервера при каждом обращении: сервер MCP может сменить
    список посреди сессии, и ход объявит разницу модели. Перед этим ход
    освежает серверы MCP сессии (McpServerSet) и берёт у них инструкции.
    Инструменты сервера отложены, кроме перечисленных в always_load;
    собственный инструмент отложен своим признаком, если его нет в
    always_load. Пока отложенные есть, в наборе с полным определением стоит
    ToolSearch (ToolSearchTool), который ищет среди них. Собственный
    инструмент, одноимённый с инструментом сервера, — ошибка на старте и в
    любом обращении после смены списка.
    """

    def __init__(  # noqa: PLR0913 — набор собирается из портов и текстов поиска
        self,
        server: ToolServer | None,
        own: Sequence[OwnTool],
        servers: McpServerSet | None,
        always_load: frozenset[str],
        templates: AgentTemplate,
        texts: ToolTexts,
    ) -> None:
        self._server = server
        self._servers = servers
        self._always = always_load
        self._search = ToolSearchTool(self, templates, texts)
        self._own: dict[str, OwnTool] = {}
        for tool in own:
            self._own[tool.name] = tool

        self._server_cards()

    @property
    def server(self) -> ToolServer | None:
        return self._server

    async def refreshed(self) -> None:
        """Устаревшее у серверов MCP сессии перечитано; без них — ничего."""
        if self._servers is None:
            return

        await self._servers.refreshed()

    def connected(self) -> Sequence[ConnectedServer]:
        """Подключённые серверы MCP с инструкциями; без них — пусто."""
        if self._servers is None:
            return ()

        return self._servers.connected()

    def inline(self) -> Sequence[SnapshotTool]:
        """Карточки с полным определением для снимка промпта: сервера, затем
        собственные, затем ToolSearch, если есть отложенные.

        Ошибки:
        ToolsError — сервер предложил инструмент с именем собственного.
        """
        cards: list[SnapshotTool] = []
        for card in self._server_cards().values():
            if card.name not in self._always:
                continue

            cards.append(self._snapshot_card(card))

        for tool in self._own.values():
            if self._own_deferred(tool):
                continue

            cards.append(tool.card())

        if self._searching():
            cards.append(self._search.card())

        return cards

    def deferred(self) -> Sequence[SnapshotTool]:
        """Карточки отложенных инструментов: сервера, затем собственные.

        Ошибки:
        ToolsError — сервер предложил инструмент с именем собственного.
        """
        cards: list[SnapshotTool] = []
        for card in self._server_cards().values():
            if card.name in self._always:
                continue

            cards.append(self._snapshot_card(card))

        for tool in self._own.values():
            if not self._own_deferred(tool):
                continue

            cards.append(tool.card())

        return cards

    def names(self) -> Sequence[str]:
        """Имена всех инструментов: с полным определением, затем отложенные."""
        names: list[str] = []
        for card in [*self.inline(), *self.deferred()]:
            names.append(card.name)

        return names

    def is_deferred(self, name: str) -> bool:
        """Инструмент отложен: модель должна загрузить его ToolSearch."""
        if name in self._always:
            return False

        own = self._own.get(name)
        if own is not None:
            return own.deferred

        return name in self._server_cards()

    def hints_of(self, name: str) -> ToolHints | None:
        """Пометки инструмента; None — такого инструмента нет."""
        own = self.own(name)
        if own is not None:
            return own.hints

        card = self._server_cards().get(name)
        if card is None:
            return None

        return card.hints

    def own(self, name: str) -> OwnTool | None:
        """Собственный инструмент по имени. ToolSearch находится всегда: его
        пометки нужны и воспроизведению истории, где отложенных уже нет, а
        вызов без отложенных честно отвечает, что искать нечего."""
        if name == self._search.name:
            return self._search

        return self._own.get(name)

    def _searching(self) -> bool:
        return bool(self.deferred())

    def _own_deferred(self, tool: OwnTool) -> bool:
        if tool.name in self._always:
            return False

        return tool.deferred

    @staticmethod
    def _snapshot_card(card: ToolCard) -> SnapshotTool:
        return SnapshotTool(
            name=card.name, description=card.description, schema=dict(card.parameters)
        )

    def _server_cards(self) -> dict[str, ToolCard]:
        """Карточки сервера сейчас по именам.

        Ошибки:
        ToolsError — инструмент сервера назван как собственный.
        """
        if self._server is None:
            return {}

        cards: dict[str, ToolCard] = {}
        for card in self._server.tools():
            own = card.name in self._own
            if card.name == self._search.name:
                own = True

            if own:
                msg = (
                    f"The tool server offers a tool named '{card.name}', which is "
                    "also the name of the agent's own tool. Rename or drop the "
                    "server tool: the agent cannot route calls between the two."
                )
                raise ToolsError(msg)

            if card.name in cards:
                msg = (
                    f"The tool server offers a tool named '{card.name}' twice. "
                    "Rename or drop one of them: the agent cannot route calls "
                    "between the two."
                )
                raise ToolsError(msg)

            cards[card.name] = card

        return cards


class ToolSearchInput(BaseModel):
    """Ввод ToolSearch (docs/prompt_assembly/texts/system, ToolSearch)."""

    model_config = ConfigDict(frozen=True)

    query: str = Field(
        description=(
            'Query to find deferred tools. Use "select:<tool_name>" for direct '
            "selection, or keywords to search."
        )
    )
    max_results: int = Field(
        default=5,
        ge=1,
        description="Maximum number of results to return (default: 5)",
    )


class ToolSearchOutput(BaseModel):
    """Итог ToolSearch для истории (`toolUseResult`) и клиента: найденные
    имена, запрос и сколько отложенных было в поиске."""

    model_config = ConfigDict(frozen=True)

    matches: Sequence[str]
    query: str
    total_deferred_tools: int


class ToolQuery(StrEnum):
    """Разметка запроса ToolSearch: выбор по именам и обязательное слово."""

    SELECT = "select:"
    NAMES = ","
    REQUIRED = "+"


class Bm25(float, Enum):
    """Параметры BM25: насыщение частоты слова и вес длины описания."""

    K1 = 1.5
    B = 0.75


class SchemaKey(StrEnum):
    """Ключи JSON Schema, по которым поиск собирает параметры инструмента."""

    PROPERTIES = "properties"
    DESCRIPTION = "description"
    ITEMS = "items"


class NamePattern(StrEnum):
    """Разбор текста на слова: разделители и слова camelCase."""

    SEPARATORS = r"[^A-Za-z0-9]+"
    WORDS = r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+"


@dataclass(frozen=True)
class ToolDocument:
    """Инструмент в индексе поиска: имя и частоты слов его имени, описания,
    имён и описаний параметров."""

    name: str
    counts: Mapping[str, int]
    length: int


@dataclass(frozen=True)
class Scored:
    """Инструмент и вес его совпадения с запросом."""

    name: str
    score: float


class ToolIndex:
    """BM25-индекс набора отложенных инструментов: документ — инструмент,
    вес слова запроса — idf, умноженная на насыщенную частоту с поправкой
    на длину документа. Строит ToolMatcher."""

    def __init__(self, documents: Sequence[ToolDocument]) -> None:
        self.documents = documents
        self._frequency: dict[str, int] = {}
        total = 0
        for document in documents:
            total += document.length
            for word in document.counts:
                self._frequency[word] = self._frequency.get(word, 0) + 1

        self._average = 1.0
        if documents and total:
            self._average = total / len(documents)

    def score(self, document: ToolDocument, words: Sequence[str]) -> float:
        total = 0.0
        for word in words:
            count = document.counts.get(word, 0)
            if not count:
                continue

            total += self._idf(word) * self._saturated(count, document.length)

        return total

    def _idf(self, word: str) -> float:
        size = len(self.documents)
        frequency = self._frequency.get(word, 0)

        return math.log((size - frequency + 0.5) / (frequency + 0.5) + 1.0)

    def _saturated(self, count: int, length: int) -> float:
        k1 = Bm25.K1.value
        norm = 1.0 - Bm25.B.value + Bm25.B.value * length / self._average

        return count * (k1 + 1.0) / (count + k1 * norm)


class ToolMatcher:
    """Поиск ToolSearch среди отложенных инструментов: `select:a,b` — эти
    имена как есть; иначе BM25 по словам имени, описания, имён и описаний
    параметров; `+слово` обязано входить в имя и тоже участвует в весе;
    лучшие max_results по весу, при равном весе — по имени. Индекс строится
    при первом поиске и заново, когда набор инструментов сменился. Создаёт
    ToolSearchTool."""

    def __init__(self) -> None:
        self._separators = re.compile(NamePattern.SEPARATORS.value)
        self._words = re.compile(NamePattern.WORDS.value)
        self._index = ToolIndex(())
        self._fingerprint: tuple[str, ...] = ()

    def matches(
        self, query: ToolSearchInput, pool: Sequence[SnapshotTool]
    ) -> Sequence[str]:
        text = query.query.strip()
        if text.lower().startswith(ToolQuery.SELECT.value):
            return self._selected(text[len(ToolQuery.SELECT.value) :], pool)

        index = self._indexed(pool)
        scored = sorted(self._scored(text, index), key=self._order)
        names: list[str] = []
        for item in scored[: query.max_results]:
            names.append(item.name)

        return names

    def words(self, text: str) -> list[str]:
        """Слова текста в нижнем регистре: разделители и границы camelCase."""
        found: list[str] = []
        for part in self._separators.split(text):
            for word in self._words.findall(part):
                found.append(word.lower())

        return found

    def _selected(self, listed: str, pool: Sequence[SnapshotTool]) -> Sequence[str]:
        known: set[str] = set()
        for tool in pool:
            known.add(tool.name)

        names: list[str] = []
        for raw in listed.split(ToolQuery.NAMES.value):
            name = raw.strip()
            if name not in known:
                continue

            if name in names:
                continue

            names.append(name)

        return names

    def _indexed(self, pool: Sequence[SnapshotTool]) -> ToolIndex:
        fingerprint = self._fingerprint_of(pool)
        if fingerprint == self._fingerprint:
            return self._index

        documents: list[ToolDocument] = []
        for tool in pool:
            documents.append(self._document(tool))

        self._index = ToolIndex(documents)
        self._fingerprint = fingerprint

        return self._index

    @staticmethod
    def _fingerprint_of(pool: Sequence[SnapshotTool]) -> tuple[str, ...]:
        parts: list[str] = []
        for tool in pool:
            parts.append(tool.model_dump_json())

        return tuple(parts)

    def _document(self, tool: SnapshotTool) -> ToolDocument:
        counts: dict[str, int] = {}
        texts = [tool.name, tool.description, *self._schema_texts(tool.schema_)]
        length = 0
        for text in texts:
            for word in self.words(text):
                counts[word] = counts.get(word, 0) + 1
                length += 1

        return ToolDocument(name=tool.name, counts=counts, length=length)

    def _schema_texts(self, schema: Mapping[str, Any]) -> Iterator[str]:
        """Имена и описания параметров схемы, вложенных объектов и элементов
        массивов."""
        properties = schema.get(SchemaKey.PROPERTIES.value)
        if isinstance(properties, Mapping):
            for key, value in properties.items():
                yield str(key)
                if isinstance(value, Mapping):
                    yield from self._parameter_texts(value)

        items = schema.get(SchemaKey.ITEMS.value)
        if isinstance(items, Mapping):
            yield from self._schema_texts(items)

    def _parameter_texts(self, parameter: Mapping[str, Any]) -> Iterator[str]:
        description = parameter.get(SchemaKey.DESCRIPTION.value)
        if isinstance(description, str):
            yield description

        yield from self._schema_texts(parameter)

    def _scored(self, text: str, index: ToolIndex) -> Iterator[Scored]:
        required: list[str] = []
        words: list[str] = []
        for term in text.lower().split():
            bare = term.removeprefix(ToolQuery.REQUIRED.value)
            if term.startswith(ToolQuery.REQUIRED.value) and bare:
                required.append(bare)

            words.extend(self.words(bare))

        for document in index.documents:
            if not self._named(document.name, required):
                continue

            score = index.score(document, words)
            if score > 0:
                yield Scored(name=document.name, score=score)

    @staticmethod
    def _named(name: str, required: Sequence[str]) -> bool:
        lowered = name.lower()
        found = map(lowered.__contains__, required)

        return all(found)

    @staticmethod
    def _order(item: Scored) -> tuple[float, str]:
        return (-item.score, item.name)


class ToolSearchTool(OwnTool):
    """Инструмент ToolSearch: загружает отложенные инструменты по запросу
    (docs/prompt_assembly/specs/07_system_tools.md, раздел 2.3).

    Ищет среди отложенных инструментов DeferredCatalog (ToolMatcher) и
    отвечает ссылками на найденные: они ложатся в историю блоками
    tool_reference результата, и со следующего запроса определения
    найденных уходят модели вместе с остальными инструментами. Сам не
    откладывается. Создаёт AgentTools.
    """

    NAME: ClassVar[str] = "ToolSearch"
    HINTS: ClassVar[ToolHints] = ToolHints(read_only=True, destructive=False)

    def __init__(
        self, catalog: DeferredCatalog, templates: AgentTemplate, texts: ToolTexts
    ) -> None:
        self._catalog = catalog
        self._templates = templates
        self._texts = texts
        self._matcher = ToolMatcher()
        self._errors = ValidationText()

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def hints(self) -> ToolHints:
        return self.HINTS

    @property
    def deferred(self) -> bool:
        return False

    def card(self) -> SnapshotTool:
        return SnapshotTool(
            name=self.NAME,
            description=self._templates.read(AgentTemplateFile.TOOL_SEARCH),
            schema=ToolSearchInput.model_json_schema(),
        )

    async def run(
        self, call: PlannedCall, arguments: Mapping[str, Any], questioner: Questioner
    ) -> CallResult:
        try:
            asked = ToolSearchInput.model_validate(arguments)
        except ValidationError as exc:
            text = (
                f"The {self.NAME} call found nothing: its arguments do not match "
                f"the tool schema: {self._errors.of(exc)}"
            )
            return self._texts.failed(call, text)

        pool = self._catalog.deferred()
        matches = self._matcher.matches(asked, pool)
        output = ToolSearchOutput(
            matches=matches, query=asked.query, total_deferred_tools=len(pool)
        )
        structured = output.model_dump(mode="json")
        if not matches:
            text = self._templates.read(AgentTemplateFile.NO_MATCHING_TOOLS)
            return self._texts.answered(call, text, structured)

        return self._texts.loaded(call, matches, structured)


class ResultBound:
    """Предел длины результата инструмента по пределам хода и профилю
    модели (TurnLimits.tool_result_chars). Создаёт агент и отдаёт всем
    ToolTexts сессии; смена модели меняет профиль и с ним предел."""

    def __init__(self, limits: TurnLimits, profile: ModelProfile) -> None:
        self._limits = limits
        self._chars = limits.tool_result_chars(profile)

    def set_profile(self, profile: ModelProfile) -> None:
        self._chars = self._limits.tool_result_chars(profile)

    @property
    def chars(self) -> int:
        return self._chars


class ToolTexts:
    """Тексты и ответы результатов: обрезка, пустой результат, отказы.

    Каждый результат несёт ответ tools/call: у вызова сервера — как пришёл,
    у отказа и собственного инструмента — собранный здесь (ToolReplies).
    """

    def __init__(
        self, templates: AgentTemplate, bound: ResultBound, agent_name: str
    ) -> None:
        self._templates = templates
        self._bound = bound
        self._agent = agent_name
        self._replies = ToolReplies()

    def bounded(self, text: str, tool: str) -> str:
        """Пустой результат — заглушка; длиннее предела — обрезка с пометкой."""
        if not text.strip():
            return self._templates.read(AgentTemplateFile.NO_OUTPUT).format(tool=tool)

        limit = self._bound.chars
        if len(text) <= limit:
            return text

        note = self._templates.read(AgentTemplateFile.TRUNCATED).format(
            shown=limit, total=len(text)
        )

        return text[:limit] + AgentTag.TRUNCATED_OUTPUT.wrap(note)

    def unknown(self, call: PlannedCall) -> CallResult:
        text = self._templates.read(AgentTemplateFile.NO_SUCH_TOOL).format(
            tool=call.name
        )

        return self.failed(call, text)

    def denied(self, call: PlannedCall, text: str) -> CallResult:
        return self.failed(call, text)

    def rejected(
        self,
        call: PlannedCall,
        message: str,
        structured: Mapping[str, Any] | None = None,
    ) -> CallResult:
        """Отказ пользователя: с его пояснением или без; structured — итог
        собственного инструмента, который отказ сопровождает."""
        if message.strip():
            text = self._templates.read(AgentTemplateFile.USER_REJECTED_SAID).format(
                message=message
            )
        else:
            text = self._templates.read(AgentTemplateFile.USER_REJECTED)

        shown = self._templates.read(AgentTemplateFile.USER_REJECTED_RESULT)

        return CallResult(
            call=call,
            text=text,
            reply=self._replies.made(shown, structured, True),
            denial=ToolDenialKind.USER_REJECTED,
        )

    def interrupted(self, call: PlannedCall) -> CallResult:
        """Ход закончен ради элемента `now`: вызов не доработал, ответа нет."""
        text = self._templates.read(AgentTemplateFile.TURN_ABORTED)

        return CallResult(
            call=call, text=text, reply=None, denial=ToolDenialKind.INTERRUPTED
        )

    def stopped(self, call: PlannedCall) -> CallResult:
        """Ход остановлен раньше ответа на вызов: модели — отказ
        пользователя, ответа нет."""
        text = self._templates.read(AgentTemplateFile.USER_REJECTED)

        return CallResult(
            call=call, text=text, reply=None, denial=ToolDenialKind.USER_REJECTED
        )

    def failed(self, call: PlannedCall, error: str) -> CallResult:
        return CallResult(call=call, text=error, reply=self._replies.refused(error))

    def not_loaded(self, call: PlannedCall) -> CallResult:
        """Вызов отложенного инструмента, которого модель не загрузила."""
        text = self._templates.read(AgentTemplateFile.NOT_LOADED).format(tool=call.name)

        return self.failed(call, text)

    def loaded(
        self, call: PlannedCall, names: Sequence[str], structured: Mapping[str, Any]
    ) -> CallResult:
        """Итог поиска инструментов: ссылки на загруженные; клиенту — их
        строки текстом."""
        template = self._templates.read(AgentTemplateFile.TOOL_LOADED)
        lines: list[str] = []
        for name in names:
            lines.append(template.format(name=name))

        reply = self._replies.made("\n".join(lines), structured, False)

        return CallResult(call=call, text="", reply=reply, references=names)

    def answered(
        self, call: PlannedCall, text: str, structured: Mapping[str, Any] | None
    ) -> CallResult:
        """Удачный итог собственного инструмента: текст модели и его
        structuredContent."""
        return CallResult(
            call=call, text=text, reply=self._replies.made(text, structured, False)
        )

    def of_reply(self, call: PlannedCall, reply: ToolReply) -> CallResult:
        """Итог вызова сервера: текст модели по блокам ответа."""
        text = self._replies.text(reply, call.name, call.block.id)

        return CallResult(call=call, text=self.bounded(text, call.name), reply=reply)


@dataclass
class Running:
    """Состояние одной пачки: что решено, что ушло серверу, чем кончилось."""

    results: dict[int, CallResult]
    submitted: list[tuple[int, PlannedCall, Mapping[str, Any]]]
    aborted: StopReason | None = None
    at_server: bool = False


class ToolRunner:
    """Исполняет вызовы одного ответа модели по разделу 5.13, шаг 8."""

    def __init__(  # noqa: PLR0913 — исполнитель собирается всеми частями ядра
        self,
        tools: AgentTools,
        texts: ToolTexts,
        templates: AgentTemplate,
        questions: ControlQuestions,
        classifier: AutoClassifier | None,
        keeper: RecordKeeper,
        clock: Clock,
        agent_name: str,
        static: StaticRules,
    ) -> None:
        self._static = static
        self._tools = tools
        self._texts = texts
        self._templates = templates
        self._questions = questions
        self._classifier = classifier
        self._keeper = keeper
        self._clock = clock
        self._agent = agent_name

    async def run(
        self, calls: Sequence[PlannedCall], cancellation: RunCancellation
    ) -> BatchOutcome:
        running = Running(results={}, submitted=[])
        questioner = Questioner(self._questions, self._keeper, cancellation)
        try:
            await self._decide_all(calls, running, questioner, cancellation)
            if running.submitted:
                await self._call_server(running, cancellation)
        except (ToolStopped, QuestionWithdrawnError):
            running.aborted = self._reason(cancellation)

        if cancellation.cancelled and running.aborted is None:
            running.aborted = self._reason(cancellation)

        ordered: list[CallResult] = []
        for index, call in enumerate(calls):
            result = running.results.get(index)
            if result is None:
                result = self._unfinished(call, running)

            ordered.append(result)

        return BatchOutcome(results=ordered, aborted=running.aborted)

    async def _decide_all(
        self,
        calls: Sequence[PlannedCall],
        running: Running,
        questioner: Questioner,
        cancellation: RunCancellation,
    ) -> None:
        for index, call in enumerate(calls):
            cancellation.raise_if_cancelled()
            decided = await self._decide(call, questioner)
            if isinstance(decided, CallResult):
                running.results[index] = decided
                continue

            running.submitted.append((index, call, decided.arguments))

    async def _decide(
        self, call: PlannedCall, questioner: Questioner
    ) -> CallResult | Approved:
        """Разрешения, вопрос наружу, собственный инструмент: результат сразу
        либо аргументы для сервера."""
        hints = self._tools.hints_of(call.name)
        if hints is None:
            return self._texts.unknown(call)

        if not self._loaded(call.name):
            return self._texts.not_loaded(call)

        decision = self._policy().decide(call.name, hints)
        if isinstance(decision, Classified):
            decision = await self._classified(call, hints)

        if isinstance(decision, Denied):
            return self._texts.denied(call, decision.text)

        arguments: Mapping[str, Any] = call.block.input
        if isinstance(decision, Asked):
            answer = await questioner.ask(self._request(call, arguments))
            if isinstance(answer, DenyAnswer):
                return self._texts.rejected(call, answer.message)

            arguments = await self._granted(answer, arguments)

        own = self._tools.own(call.name)
        if own is not None:
            await self._started(call)
            return await own.run(call, arguments, questioner)

        return Approved(arguments)

    def _loaded(self, name: str) -> bool:
        """Инструмент можно звать: он не отложен либо модель его загрузила."""
        if not self._tools.is_deferred(name):
            return True

        announced = Announced(self._keeper.session.conversation())

        return name in announced.loaded_names()

    async def _classified(
        self, call: PlannedCall, hints: ToolHints
    ) -> Allowed | Denied:
        if self._classifier is None:
            text = self._templates.read(AgentTemplateFile.CLASSIFIER_FAILED).format(
                tool=call.name, error="the agent has no classifier configured"
            )
            return Denied(text)

        return await self._classifier.decide(call.name, hints, call.block.input)

    def _policy(self) -> PermissionPolicy:
        state = PermissionState(self._keeper.session.service())

        return PermissionPolicy(state, self._templates, self._agent, self._static)

    def _request(
        self, call: PlannedCall, arguments: Mapping[str, Any]
    ) -> CanUseToolRequest:
        suggestion = PermissionRule(tool=call.name, behavior=PermissionBehavior.ALLOW)

        return CanUseToolRequest(
            tool_name=call.name,
            input=arguments,
            tool_use_id=call.block.id,
            permission_suggestions=[suggestion],
        )

    async def _granted(
        self, answer: AllowAnswer, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Правила «до конца сессии» — записями; аргументы — из ответа, если
        запускающая программа их заменила, иначе те, о которых спрашивали."""
        for rule in answer.updated_permissions:
            await self._keeper.add(
                PermissionRuleRecord(
                    timestamp=self._clock.now(),
                    session_id=self._keeper.session.id.value,
                    operation=PermissionRuleOperation.ADD,
                    rule=rule,
                )
            )

        if answer.updated_input is None:
            return arguments

        return answer.updated_input

    async def _call_server(
        self, running: Running, cancellation: RunCancellation
    ) -> None:
        server = self._tools.server
        if server is None:
            for index, call, _ in running.submitted:
                running.results[index] = self._texts.unknown(call)

            return

        approved: list[ToolUseBlock] = []
        for _, call, arguments in running.submitted:
            approved.append(call.block.model_copy(update={"input": dict(arguments)}))
            await self._started(call)

        running.at_server = True
        try:
            futures = await server.submit(approved)
        except Exception as exc:
            logger.warning("tools: the tool server refused the batch: %s", exc)
            for index, call, _ in running.submitted:
                error = f"The tool server refused the call of {call.name}: {exc}"
                running.results[index] = self._texts.failed(call, error)

            return

        with cancellation.abort_with(self._canceller(futures)):
            settled = await asyncio.gather(*futures, return_exceptions=True)

        for (index, call, _), outcome in zip(running.submitted, settled, strict=True):
            if isinstance(outcome, ToolReply):
                running.results[index] = self._texts.of_reply(call, outcome)
                continue

            if isinstance(outcome, asyncio.CancelledError):
                continue

            if isinstance(outcome, BaseException):
                error = f"The call of {call.name} crashed in the tool server: {outcome}"
                running.results[index] = self._texts.failed(call, error)

    async def _started(self, call: PlannedCall) -> None:
        """Вызов прошёл разрешения и уходит на исполнение."""
        await self._keeper.emit(ToolStartedEvent(tool_use_id=call.block.id))

    def _canceller(
        self, futures: Sequence[asyncio.Future[ToolReply]]
    ) -> Callable[[], None]:
        def cancel_all() -> None:
            for future in futures:
                future.cancel()

        return cancel_all

    def _unfinished(self, call: PlannedCall, running: Running) -> CallResult:
        """Вызов без результата после отмены: по стадии и причине (раздел 5.14)."""
        if running.at_server and running.aborted is StopReason.SUPERSEDED:
            return self._texts.interrupted(call)

        return self._texts.stopped(call)

    def _reason(self, cancellation: RunCancellation) -> StopReason:
        reason = cancellation.reason
        if reason is None:
            return StopReason.USER_STOP

        return reason
