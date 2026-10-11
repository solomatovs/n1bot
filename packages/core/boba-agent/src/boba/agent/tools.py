"""Инструменты агента и исполнение вызовов одного ответа модели.

Порт сервера инструментов — `ToolServer` из boba.toolkit; собственные
инструменты агента (вопрос пользователю) живут рядом с инструментами
сервера и зовутся по имени (план, раздел 5.12). ToolRunner ведёт пачку
вызовов по разделу 5.13, шаг 8: каждый вызов проходит разрешения (раздел
5.6), вопросы наружу идут по очереди, затем хук `PreToolUse` (boba.agent.hooks)
может заменить аргументы, снять вызов или потребовать вопрос наружу;
разрешённые вызовы уходят серверу одним `submit` с аргументами после хука,
а запись `tool_use` хранит аргументы модели; результаты собираются в
порядке вызовов; отмена посреди пачки даёт результаты-отказы по таблице
раздела 5.14. Результат длиннее предела обрезается, пустой заменяется
заглушкой.

Ошибки:
ToolsError — имя собственного инструмента совпало с именем инструмента
    сервера: с таким набором агент не стартует.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.attachments import SnapshotTool
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
from boba.agent.hooks import (
    HookAllowed,
    HookAsked,
    HookBlocked,
    HookDenied,
    HookedCall,
    HookVerdict,
    PreToolUseHooks,
)
from boba.agent.ids import Clock
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
from boba.toolkit.dag import DagNode, NodeOutcome, ToolHints, ToolServer

logger = logging.getLogger(__name__)

__all__ = [
    "AgentTools",
    "Approved",
    "AskUserQuestionTool",
    "BatchOutcome",
    "CallResult",
    "OwnTool",
    "PlannedCall",
    "Questioner",
    "ToolRunner",
    "ToolTexts",
    "ToolsError",
]


class ToolsError(Exception):
    """Набор инструментов противоречив: имена совпали."""


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
    """Результат вызова для записи `user`: текст, признак ошибки, структурный
    итог для `toolUseResult` и причина отказа, если это отказ."""

    call: PlannedCall
    text: str
    is_error: bool
    artifact: Any
    denial: ToolDenialKind | None = None


@dataclass(frozen=True)
class BatchOutcome:
    """Итог пачки: результаты в порядке вызовов и причина отмены, если была."""

    results: Sequence[CallResult]
    aborted: StopReason | None


@dataclass(frozen=True)
class Approved:
    """Вызов прошёл разрешения и хук: эти аргументы уходят серверу."""

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


class AskOption(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    label: str = Field(
        description=(
            "The display text for this option that the user will see and select. "
            "Should be concise (1-5 words) and clearly describe the choice."
        )
    )
    description: str = Field(
        description=(
            "Explanation of what this option means or what will happen if chosen. "
            "Useful for providing context about trade-offs or implications."
        )
    )


class AskQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    question: str = Field(
        description=(
            "The complete question to ask the user. Should be clear, specific, and "
            'end with a question mark. Example: "Which library should we use for '
            'date formatting?" If multiSelect is true, phrase it accordingly, e.g. '
            '"Which features do you want to enable?"'
        )
    )
    header: str = Field(
        max_length=12,
        description=(
            "Very short label displayed as a chip/tag (max 12 chars). Examples: "
            '"Auth method", "Library", "Approach".'
        ),
    )
    options: Sequence[AskOption] = Field(
        min_length=2,
        max_length=4,
        description=(
            "The available choices for this question. Must have 2-4 options. Each "
            "option should be a distinct, mutually exclusive choice (unless "
            "multiSelect is enabled). There should be no 'Other' option, that will "
            "be provided automatically."
        ),
    )
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description=(
            "Set to true to allow the user to select multiple options instead of "
            "just one. Use when choices are not mutually exclusive."
        ),
    )


class AskUserQuestionInput(BaseModel):
    """Ввод инструмента «вопрос пользователю»."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    questions: Sequence[AskQuestion] = Field(
        min_length=1,
        max_length=4,
        description="Questions to ask the user (1-4 questions)",
    )


class AskUserQuestionTool(OwnTool):
    """Инструмент «вопрос пользователю»: схема и тексты — по инструменту
    AskUserQuestion Claude Code. Вопрос уходит наружу тем же
    `can_use_tool`, ответы приходят в `updated_input` под ключом `answers`:
    вопрос → ответ. Создаёт агент."""

    NAME: ClassVar[str] = "AskUserQuestion"
    ANSWERS_KEY: ClassVar[str] = "answers"
    HINTS: ClassVar[ToolHints] = ToolHints(read_only=True, destructive=False)

    def __init__(self, templates: AgentTemplate, texts: ToolTexts) -> None:
        self._templates = templates
        self._texts = texts

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
        request = CanUseToolRequest(
            tool_name=self.NAME, input=arguments, tool_use_id=call.block.id
        )
        answer = await questioner.ask(request)
        if isinstance(answer, DenyAnswer):
            return self._texts.rejected(call, answer.message)

        answers = self._answers(answer)
        if not answers:
            text = self._templates.read(AgentTemplateFile.NO_ANSWERS)
            return CallResult(call=call, text=text, is_error=False, artifact=text)

        listed: list[str] = []
        for question, reply in answers.items():
            listed.append(f'"{question}"="{reply}"')

        text = self._templates.read(AgentTemplateFile.ANSWERS).format(
            answers=", ".join(listed)
        )
        artifact = {
            "questions": list(arguments.get("questions", [])),
            "answers": answers,
        }

        return CallResult(call=call, text=text, is_error=False, artifact=artifact)

    def _answers(self, answer: AllowAnswer) -> dict[str, str]:
        updated = answer.updated_input
        if updated is None:
            return {}

        raw = updated.get(self.ANSWERS_KEY)
        if not isinstance(raw, Mapping):
            return {}

        answers: dict[str, str] = {}
        for question, reply in raw.items():
            answers[str(question)] = str(reply)

        return answers


class AgentTools:
    """Инструменты, которые видит модель: сервера и собственные, по имени.

    Создаёт агент при открытии сессии. Карточки для снимка — отсюда;
    пометки — у живого сервера или собственного инструмента; собственный
    инструмент, одноимённый с инструментом сервера, — ошибка на старте.
    """

    def __init__(self, server: ToolServer | None, own: Sequence[OwnTool]) -> None:
        self._server = server
        self._own: dict[str, OwnTool] = {}
        for tool in own:
            self._own[tool.name] = tool

        self._server_hints: dict[str, ToolHints] = {}
        self._server_cards: dict[str, SnapshotTool] = {}
        if server is not None:
            for card in server.tools():
                if card.name in self._own:
                    msg = (
                        f"The tool server offers a tool named '{card.name}', which is "
                        "also the name of the agent's own tool. Rename or drop the "
                        "server tool: the agent cannot route calls between the two."
                    )
                    raise ToolsError(msg)

                self._server_hints[card.name] = card.hints
                self._server_cards[card.name] = SnapshotTool(
                    name=card.name,
                    description=card.description,
                    schema=dict(card.parameters),
                )

    @property
    def server(self) -> ToolServer | None:
        return self._server

    def snapshot(self) -> Sequence[SnapshotTool]:
        """Карточки для снимка промпта: сервера, затем собственные."""
        cards = list(self._server_cards.values())
        for tool in self._own.values():
            cards.append(tool.card())

        return cards

    def hints_of(self, name: str) -> ToolHints | None:
        """Пометки инструмента; None — такого инструмента нет."""
        own = self._own.get(name)
        if own is not None:
            return own.hints

        return self._server_hints.get(name)

    def schema_of(self, name: str) -> Mapping[str, Any]:
        """Входная JSON-схема инструмента; у неизвестного — пустая."""
        own = self._own.get(name)
        if own is not None:
            return own.card().schema_

        card = self._server_cards.get(name)
        if card is None:
            return {}

        return card.schema_

    def own(self, name: str) -> OwnTool | None:
        return self._own.get(name)


class ToolTexts:
    """Тексты результатов: обрезка, пустой результат, отказы."""

    def __init__(self, templates: AgentTemplate, limit: int, agent_name: str) -> None:
        self._templates = templates
        self._limit = limit
        self._agent = agent_name

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

        return CallResult(call=call, text=text, is_error=True, artifact=text)

    def denied(self, call: PlannedCall, text: str) -> CallResult:
        return CallResult(call=call, text=text, is_error=True, artifact=text)

    def rejected(self, call: PlannedCall, message: str) -> CallResult:
        """Отказ пользователя: с его пояснением или без."""
        if message.strip():
            text = self._templates.read(AgentTemplateFile.USER_REJECTED_SAID).format(
                message=message
            )
        else:
            text = self._templates.read(AgentTemplateFile.USER_REJECTED)

        return CallResult(
            call=call,
            text=text,
            is_error=True,
            artifact=self._templates.read(AgentTemplateFile.USER_REJECTED_RESULT),
            denial=ToolDenialKind.USER_REJECTED,
        )

    def hook_denied(self, call: PlannedCall, reason: str) -> CallResult:
        """Обработчик хука снял вызов: с причиной или без неё."""
        if reason.strip():
            text = self._templates.read(AgentTemplateFile.HOOK_DENIED).format(
                tool=call.name, reason=reason
            )
        else:
            text = self._templates.read(AgentTemplateFile.HOOK_DENIED_SILENT).format(
                tool=call.name
            )

        return CallResult(call=call, text=text, is_error=True, artifact=text)

    def hook_blocked(self, call: PlannedCall, url: str, error: str) -> CallResult:
        """Обработчик хука не ответил, а его отказ блокирует вызов."""
        text = self._templates.read(AgentTemplateFile.HOOK_BLOCKED).format(
            tool=call.name, url=url, error=error
        )

        return CallResult(call=call, text=text, is_error=True, artifact=text)

    def interrupted(self, call: PlannedCall) -> CallResult:
        """Ход закончен ради элемента `now`: вызов не доработал."""
        text = self._templates.read(AgentTemplateFile.TURN_ABORTED)

        return CallResult(
            call=call,
            text=text,
            is_error=True,
            artifact=text,
            denial=ToolDenialKind.INTERRUPTED,
        )

    def failed(self, call: PlannedCall, error: str) -> CallResult:
        return CallResult(call=call, text=error, is_error=True, artifact=error)

    def of_outcome(self, call: PlannedCall, outcome: NodeOutcome) -> CallResult:
        text = self.bounded(outcome.content, call.name)
        # журнал не хранит null, поэтому и в памяти итог лежит без них
        artifact = outcome.use_result().model_dump(mode="json", exclude_none=True)

        return CallResult(
            call=call, text=text, is_error=not outcome.ok(), artifact=artifact
        )


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
        hooks: PreToolUseHooks | None,
    ) -> None:
        self._static = static
        self._hooks = hooks
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
            decided = await self._decide(call, questioner, cancellation)
            if isinstance(decided, CallResult):
                running.results[index] = decided
                continue

            running.submitted.append((index, call, decided.arguments))

    async def _decide(
        self,
        call: PlannedCall,
        questioner: Questioner,
        cancellation: RunCancellation,
    ) -> CallResult | Approved:
        """Разрешения, вопрос наружу, хук, собственный инструмент: результат
        сразу либо аргументы для сервера."""
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

        hooked = await self._hooked(call, arguments, cancellation)
        settled = await self._after_hook(call, hooked, questioner)
        if isinstance(settled, CallResult):
            return settled

        own = self._tools.own(call.name)
        if own is not None:
            await self._started(call)
            return await own.run(call, settled.arguments, questioner)

        return settled

    async def _hooked(
        self,
        call: PlannedCall,
        arguments: Mapping[str, Any],
        cancellation: RunCancellation,
    ) -> HookVerdict:
        if self._hooks is None:
            return HookAllowed(arguments=arguments)

        session = self._keeper.session
        hooked = HookedCall(
            session_id=session.id.value,
            permission_mode=PermissionState(session.service()).mode,
            tool_name=call.name,
            tool_use_id=call.block.id,
            arguments=arguments,
            schema=self._tools.schema_of(call.name),
        )

        return await self._hooks.before_call(hooked, cancellation)

    async def _after_hook(
        self, call: PlannedCall, hooked: HookVerdict, questioner: Questioner
    ) -> CallResult | Approved:
        """Вердикт хука → отказ сразу, вопрос наружу или аргументы вызова."""
        if isinstance(hooked, HookDenied):
            return self._texts.hook_denied(call, hooked.reason)

        if isinstance(hooked, HookBlocked):
            return self._texts.hook_blocked(call, hooked.url, hooked.error)

        if isinstance(hooked, HookAsked):
            answer = await questioner.ask(self._request(call, hooked.arguments))
            if isinstance(answer, DenyAnswer):
                return self._texts.rejected(call, answer.message)

            return Approved(await self._granted(answer, hooked.arguments))

        return Approved(hooked.arguments)

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

        nodes: list[DagNode] = []
        for _, call, arguments in running.submitted:
            nodes.append(
                DagNode(key=call.block.id, tool=call.name, args=dict(arguments))
            )
            await self._started(call)

        running.at_server = True
        try:
            futures = await server.submit(nodes)
        except Exception as exc:
            logger.warning("tools: the tool server refused the batch: %s", exc)
            for index, call, _ in running.submitted:
                error = f"The tool server refused the call of {call.name}: {exc}"
                running.results[index] = self._texts.failed(call, error)

            return

        with cancellation.abort_with(self._canceller(futures)):
            settled = await asyncio.gather(*futures, return_exceptions=True)

        for (index, call, _), outcome in zip(running.submitted, settled, strict=True):
            if isinstance(outcome, NodeOutcome):
                running.results[index] = self._texts.of_outcome(call, outcome)
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
        self, futures: Sequence[asyncio.Future[NodeOutcome]]
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
