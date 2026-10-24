"""Сборщик запроса: разговор и снимок промпта → `ChatRequest` порта модели.

Алгоритм — docs/prompt_assembly/specs/06_messages_algorithm.md, доведённый до
формы `ChatTurn` (план, раздел 5.12). Что вносит в запрос каждая запись и
где она встаёт, говорит сама запись (ChainRecord.to_request и placement);
здесь — общий алгоритм: перестановка вложений перед текстом пользователя,
склейка ответа по `message.id`, слияние
соседних сообщений пользователя, два режима подачи системных вложений,
починка пар вызов — результат, чистка и защита от подделки служебного
блока (TagShield у текстов пользователя и результатов).
Инструменты — из снимка, по имени, затем загруженные отложенные из истории
в порядке загрузки с пометкой deferred (docs/prompt_assembly/specs/
07_system_tools.md, раздел 2.3): провайдер без родных отложенных
инструментов дописывает их в конец списка, начало списка не меняется.
Ссылки tool_reference результата уходят строками текста и списком
tool_references сообщения роли tool.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from boba.agent.attachments import Announced
from boba.agent.profile import ModelProfile, ReasoningReturn
from boba.agent.records import (
    AssistantBlock,
    ChainRecord,
    Placement,
    PromptSnapshot,
    RequestFeed,
    SnapshotTool,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from boba.agent.tags import AgentTag, TagShield
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.llm.chat import ChatRequest, ChatRole, ChatTurn, ToolCall, ToolSpec

__all__ = ["RequestBuilder", "RequestTexts"]


@dataclass
class UserTurn:
    """Сообщение пользователя до перевода: результаты, вложения, тексты."""

    results: list[ToolResultBlock] = field(default_factory=list)
    reminders: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    @property
    def human(self) -> bool:
        return bool(self.texts)

    @property
    def results_only(self) -> bool:
        return bool(self.results) and not self.texts and not self.reminders


@dataclass
class AssistantTurn:
    """Ответ модели до перевода: блоки одного `message.id`."""

    message_id: str
    thinking: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    calls: list[ToolUseBlock] = field(default_factory=list)
    reasoning_returned: bool = False


@dataclass
class SystemTurn:
    """Системное сообщение из вложений в режиме `system_turns`."""

    texts: list[str] = field(default_factory=list)


Turn = UserTurn | AssistantTurn | SystemTurn


class RequestTexts:
    """Тексты записи, какими их видит модель: слова пользователя с
    обезвреженными служебными тегами и результат инструмента — строки о
    загруженных отложенных инструментах, обезвреженный текст, у ошибки —
    обёртка tool_use_error. Им пользуются сборщик запроса и оценка окна,
    чтобы окно считалось по тому же тексту, что уходит модели."""

    TEXT_JOIN: ClassVar[str] = "\n"

    def __init__(self, templates: AgentTemplate) -> None:
        self._templates = templates
        self._shield = TagShield([AgentTag.SYSTEM_REMINDER, AgentTag.TOOL_USE_ERROR])

    def user(self, text: str) -> str:
        return self._shield.shield(text)

    def result(self, result: ToolResultBlock) -> str:
        lines: list[str] = []
        for name in result.references():
            lines.append(
                self._templates.read(AgentTemplateFile.TOOL_LOADED).format(name=name)
            )

        text = self._shield.shield(result.text())
        if text:
            lines.append(text)

        joined = self.TEXT_JOIN.join(lines)
        if result.is_error:
            return AgentTag.TOOL_USE_ERROR.wrap(joined)

        return joined


class Feed(RequestFeed):
    """Реализация RequestFeed сборщика: накопитель сообщений в порядке
    подачи (шаги 4–5 разбора 06). Записи отдают сюда свои части, накопитель
    склеивает их: блоки одного ответа — одним сообщением, тексты
    пользователя — с обезвреженными служебными тегами, системные вложения —
    напоминаниями в сообщении пользователя либо отдельным системным
    сообщением в режиме system_turns. Создаёт сборщик на каждый запрос."""

    def __init__(self, texts: RequestTexts, system_turns: bool) -> None:
        self._texts = texts
        self._system_turns = system_turns
        self.turns: list[Turn] = []
        self.pending_system: list[str] = []

    def user_text(self, text: str) -> None:
        self.last_user().texts.append(self._texts.user(text))

    def context_text(self, text: str) -> None:
        self.last_user().texts.append(text)

    def reminder(self, text: str) -> None:
        if self._system_turns:
            self.pending_system.append(AgentTag.SYSTEM_REMINDER.unwrap_block(text))
            return

        self.last_user().reminders.append(text)

    def tool_result(self, block: ToolResultBlock) -> None:
        self.last_user().results.append(block)

    def tool_definitions(self, tools: Sequence[SnapshotTool]) -> None:
        """Определения загруженных отложенных уходят списком tools: его
        сборщик берёт из свёртки Announced."""
        return

    def reply_block(self, message_id: str, block: AssistantBlock) -> None:
        turn = self.assistant(message_id)
        if isinstance(block, ThinkingBlock):
            turn.thinking.append(block.thinking)
            return

        if isinstance(block, TextBlock):
            turn.texts.append(block.text)
            return

        turn.calls.append(block)

    def last_user(self) -> UserTurn:
        if self.turns and isinstance(self.turns[-1], UserTurn):
            return self.turns[-1]

        turn = UserTurn()
        self.turns.append(turn)

        return turn

    def assistant(self, message_id: str) -> AssistantTurn:
        """Ответ с этим `message.id`, если его ещё можно продолжить, иначе новый."""
        for turn in reversed(self.turns):
            if isinstance(turn, AssistantTurn):
                if turn.message_id == message_id:
                    return turn

                break

            if isinstance(turn, UserTurn) and turn.results_only:
                continue

            if isinstance(turn, SystemTurn):
                continue

            break

        self.flush_system()
        turn = AssistantTurn(message_id=message_id)
        self.turns.append(turn)

        return turn

    def flush_system(self) -> None:
        """Накопленные системные тексты — одним сообщением после пользователя."""
        if not self.pending_system:
            return

        texts = self.pending_system
        self.pending_system = []
        if self.turns and isinstance(self.turns[-1], SystemTurn):
            self.turns[-1].texts.extend(texts)
            return

        if self.turns and isinstance(self.turns[-1], UserTurn):
            self.turns.append(SystemTurn(texts=texts))
            return

        self.last_user().reminders.extend(texts)


class RequestBuilder:
    """Строит `ChatRequest` из разговора и действующего снимка."""

    SECTION_JOIN: ClassVar[str] = "\n\n"
    TEXT_JOIN: ClassVar[str] = "\n"

    def __init__(self, profile: ModelProfile, templates: AgentTemplate) -> None:
        self._profile = profile
        self._templates = templates
        self._texts = RequestTexts(templates)

    def build(
        self, conversation: Sequence[ChainRecord], snapshot: PromptSnapshot
    ) -> ChatRequest:
        feed = Feed(self._texts, self._profile.system_turns)
        for record in self._reordered(conversation):
            record.to_request(feed)

        feed.flush_system()
        turns = self._repaired(self._cleaned(feed.turns))
        self._mark_reasoning(turns)

        messages: list[ChatTurn] = [
            ChatTurn(
                role=ChatRole.SYSTEM,
                content=self.SECTION_JOIN.join(snapshot.system_prompt),
            )
        ]
        for turn in turns:
            messages.extend(self._translate(turn))

        return ChatRequest(
            messages=messages,
            tools=list(self._tools(conversation, snapshot)),
            sampling=dict(self._profile.chat.sampling),
            stream=True,
        )

    def summary(
        self,
        sent: Sequence[ChainRecord],
        conversation: Sequence[ChainRecord],
        snapshot: PromptSnapshot,
    ) -> ChatRequest:
        """Запрос пересказа: сообщения — отрезок с инструкцией, инструменты —
        те же, что у основного запроса по всему разговору: загруженные
        отложенные записаны и в отброшенных лестницей группах, и в хвосте."""
        request = self.build(sent, snapshot)
        tools = list(self._tools(conversation, snapshot))

        return request.model_copy(update={"tools": tools})

    def _reordered(self, records: Sequence[ChainRecord]) -> list[ChainRecord]:
        """Вложения встают сразу после ближайшего выше ответа или результата
        инструмента и перед текстом пользователя, за которым записаны."""
        reversed_out: list[ChainRecord] = []
        held: list[ChainRecord] = []
        for record in reversed(records):
            placement = record.placement()
            if placement is Placement.FLOATS:
                held.insert(0, record)
                continue

            if placement is Placement.STOPS and held:
                reversed_out.extend(reversed(held))
                held = []

            reversed_out.append(record)

        reversed_out.extend(reversed(held))
        reversed_out.reverse()

        return reversed_out

    def _cleaned(self, turns: Sequence[Turn]) -> list[Turn]:
        """Пустые ответы и осиротевшие рассуждения — по таблице разбора 06, §6."""
        kept: list[Turn] = []
        last_assistant = self._last_assistant(turns)
        for turn in turns:
            if not isinstance(turn, AssistantTurn):
                kept.append(turn)
                continue

            has_text = bool(self.SECTION_JOIN.join(turn.texts).strip())
            if has_text or turn.calls:
                kept.append(turn)
                continue

            if turn is last_assistant and turn.thinking:
                text = self._templates.read(AgentTemplateFile.NO_MESSAGE_CONTENT)
                turn.texts = [text]
                kept.append(turn)

        return kept

    def _last_assistant(self, turns: Sequence[Turn]) -> AssistantTurn | None:
        for turn in reversed(turns):
            if isinstance(turn, AssistantTurn):
                return turn

        return None

    def _repaired(self, turns: Sequence[Turn]) -> list[Turn]:
        """Пары вызов — результат: вызову без результата — заглушка, результат
        без вызова и повторный идентификатор выбрасываются."""
        repaired: list[Turn] = []
        pending: dict[str, ToolUseBlock] = {}
        for turn in turns:
            if isinstance(turn, AssistantTurn):
                self._close_pending(repaired, pending)
                pending = self._unique_calls(turn)
                repaired.append(turn)
                continue

            if isinstance(turn, UserTurn):
                turn.results = self._known_results(turn.results, pending)
                if turn.results or turn.texts or turn.reminders:
                    repaired.append(turn)
                continue

            repaired.append(turn)

        self._close_pending(repaired, pending)

        return repaired

    def _unique_calls(self, turn: AssistantTurn) -> dict[str, ToolUseBlock]:
        unique: dict[str, ToolUseBlock] = {}
        for call in turn.calls:
            if call.id in unique:
                continue

            unique[call.id] = call

        turn.calls = list(unique.values())

        return unique

    def _known_results(
        self, results: Sequence[ToolResultBlock], pending: dict[str, ToolUseBlock]
    ) -> list[ToolResultBlock]:
        known: list[ToolResultBlock] = []
        for result in results:
            if result.tool_use_id not in pending:
                continue

            del pending[result.tool_use_id]
            known.append(result)

        return known

    def _close_pending(
        self, repaired: list[Turn], pending: dict[str, ToolUseBlock]
    ) -> None:
        """Вызовы без результата закрываются заглушкой первой среди результатов."""
        if not pending:
            return

        stubs: list[ToolResultBlock] = []
        text = self._templates.read(AgentTemplateFile.TOOL_RESULT_MISSING)
        for call_id in pending:
            stub = ToolResultBlock(tool_use_id=call_id, content=text, is_error=True)
            stubs.append(stub)

        pending.clear()
        if repaired and isinstance(repaired[-1], UserTurn):
            repaired[-1].results = [*stubs, *repaired[-1].results]
            return

        position = len(repaired)
        for index in range(len(repaired) - 1, -1, -1):
            if isinstance(repaired[index], AssistantTurn):
                position = index + 1
                break

        repaired.insert(position, UserTurn(results=stubs))

    def _mark_reasoning(self, turns: Sequence[Turn]) -> None:
        """Какие ответы уходят с рассуждениями — по `reasoning_return` профиля."""
        policy = self._profile.reasoning_return
        if policy is ReasoningReturn.NEVER:
            return

        after_human = False
        if policy is ReasoningReturn.ALWAYS:
            after_human = True

        last_human = -1
        for index, turn in enumerate(turns):
            if isinstance(turn, UserTurn) and turn.human:
                last_human = index

        for index, turn in enumerate(turns):
            if not isinstance(turn, AssistantTurn):
                continue

            turn.reasoning_returned = after_human or index > last_human

    def _translate(self, turn: Turn) -> Iterator[ChatTurn]:
        if isinstance(turn, SystemTurn):
            yield ChatTurn(
                role=ChatRole.SYSTEM, content=self.SECTION_JOIN.join(turn.texts)
            )
            return

        if isinstance(turn, AssistantTurn):
            yield self._assistant(turn)
            return

        yield from self._user(turn)

    def _assistant(self, turn: AssistantTurn) -> ChatTurn:
        calls: list[ToolCall] = []
        for call in turn.calls:
            arguments = dict(call.input)
            calls.append(ToolCall(id=call.id, name=call.name, arguments=arguments))

        reasoning: str | None = None
        if turn.reasoning_returned:
            reasoning = self.TEXT_JOIN.join(turn.thinking)

        return ChatTurn(
            role=ChatRole.ASSISTANT,
            content=self.SECTION_JOIN.join(turn.texts),
            reasoning=reasoning,
            tool_calls=calls,
        )

    def _user(self, turn: UserTurn) -> Iterator[ChatTurn]:
        tools: list[ChatTurn] = []
        for result in turn.results:
            tools.append(
                ChatTurn(
                    role=ChatRole.TOOL,
                    content=self._texts.result(result),
                    tool_call_id=result.tool_use_id,
                    tool_references=result.references(),
                )
            )

        texts = [*turn.reminders, *turn.texts]
        if tools and not turn.texts and turn.reminders:
            # в режиме без system_turns напоминания после одних результатов
            # дописываются в последний результат
            tail = tools[-1]
            joined = self.SECTION_JOIN.join([tail.content, *turn.reminders])
            tools[-1] = tail.model_copy(update={"content": joined})
            texts = []

        yield from tools
        if texts:
            # слитые сообщения пользователя и вложения — отдельными частями
            # одного сообщения, как блоки в docs/prompt_assembly
            yield ChatTurn(role=ChatRole.USER, parts=texts)

    def _tools(
        self, conversation: Sequence[ChainRecord], snapshot: PromptSnapshot
    ) -> Iterator[ToolSpec]:
        for tool in sorted(snapshot.tools, key=lambda item: item.name):
            yield ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.schema_),
            )

        for tool in Announced(conversation).deferred_loaded():
            yield ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.schema_),
                deferred=True,
            )
