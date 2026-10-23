"""Инструменты агента и исполнение вызовов одного ответа модели.

Порт сервера инструментов — `ToolServer`: карточки для модели и вызовы
`tools/call` в форме MCP; реализует его вход процесса над серверами MCP
сессии. Собственные инструменты агента (вопрос пользователю) живут рядом
с инструментами сервера и зовутся по имени (план, раздел 5.12) и отвечают
тем же результатом tools/call. ToolRunner ведёт пачку вызовов по разделу
5.13, шаг 8: каждый вызов проходит разрешения (раздел 5.6), вопросы
наружу идут по очереди; разрешённые вызовы уходят серверу одним
`submit`; результаты собираются в порядке вызовов; отмена посреди пачки
даёт результаты-отказы по таблице раздела 5.14. Результат длиннее предела
обрезается, пустой заменяется заглушкой.

Ошибки:
ToolsError — имя инструмента предложено дважды: собственным инструментом и
    сервером либо двумя серверами; с таким набором агент не стартует.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.agent.attachments import ConnectedServer, SnapshotTool
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
    "McpServerSet",
    "OwnTool",
    "PlannedCall",
    "Questioner",
    "ToolRunner",
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
    (его JSON — `toolUseResult`) и причина отказа, если это отказ."""

    call: PlannedCall
    text: str
    reply: ToolReply
    denial: ToolDenialKind | None = None

    @property
    def is_error(self) -> bool:
        return self.reply.result.is_error


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


class AgentTools:
    """Инструменты, которые видит модель: сервера и собственные, по имени.

    Создаёт агент при открытии сессии. Карточки инструментов сервера
    берутся у живого сервера при каждом снимке: сервер MCP может сменить
    список посреди сессии, и ход объявит разницу модели. Перед этим ход
    освежает серверы MCP сессии (McpServerSet) и берёт у них инструкции.
    Собственный инструмент, одноимённый с инструментом сервера, — ошибка на
    старте и в любом снимке после смены списка.
    """

    def __init__(
        self,
        server: ToolServer | None,
        own: Sequence[OwnTool],
        servers: McpServerSet | None,
    ) -> None:
        self._server = server
        self._servers = servers
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

    def snapshot(self) -> Sequence[SnapshotTool]:
        """Карточки для снимка промпта: сервера, затем собственные.

        Ошибки:
        ToolsError — сервер предложил инструмент с именем собственного.
        """
        cards: list[SnapshotTool] = []
        for card in self._server_cards().values():
            cards.append(
                SnapshotTool(
                    name=card.name,
                    description=card.description,
                    schema=dict(card.parameters),
                )
            )

        for tool in self._own.values():
            cards.append(tool.card())

        return cards

    def hints_of(self, name: str) -> ToolHints | None:
        """Пометки инструмента; None — такого инструмента нет."""
        own = self._own.get(name)
        if own is not None:
            return own.hints

        card = self._server_cards().get(name)
        if card is None:
            return None

        return card.hints

    def own(self, name: str) -> OwnTool | None:
        return self._own.get(name)

    def _server_cards(self) -> dict[str, ToolCard]:
        """Карточки сервера сейчас по именам.

        Ошибки:
        ToolsError — инструмент сервера назван как собственный.
        """
        if self._server is None:
            return {}

        cards: dict[str, ToolCard] = {}
        for card in self._server.tools():
            if card.name in self._own:
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


class ToolTexts:
    """Тексты и ответы результатов: обрезка, пустой результат, отказы.

    Каждый результат несёт ответ tools/call: у вызова сервера — как пришёл,
    у отказа и собственного инструмента — собранный здесь (ToolReplies).
    """

    def __init__(self, templates: AgentTemplate, limit: int, agent_name: str) -> None:
        self._templates = templates
        self._limit = limit
        self._agent = agent_name
        self._replies = ToolReplies()

    def bounded(self, text: str, tool: str) -> str:
        """Пустой результат — заглушка; длиннее предела — обрезка с пометкой."""
        if not text.strip():
            return self._templates.read(AgentTemplateFile.NO_OUTPUT).format(tool=tool)

        if self._limit <= 0 or len(text) <= self._limit:
            return text

        note = self._templates.read(AgentTemplateFile.TRUNCATED).format(
            shown=self._limit, total=len(text)
        )

        return text[: self._limit] + AgentTag.TRUNCATED_OUTPUT.wrap(note)

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
        """Ход закончен ради элемента `now`: вызов не доработал."""
        text = self._templates.read(AgentTemplateFile.TURN_ABORTED)

        return CallResult(
            call=call,
            text=text,
            reply=self._replies.refused(text),
            denial=ToolDenialKind.INTERRUPTED,
        )

    def failed(self, call: PlannedCall, error: str) -> CallResult:
        return CallResult(call=call, text=error, reply=self._replies.refused(error))

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

        return self._texts.rejected(call, "")

    def _reason(self, cancellation: RunCancellation) -> StopReason:
        reason = cancellation.reason
        if reason is None:
            return StopReason.USER_STOP

        return reason
