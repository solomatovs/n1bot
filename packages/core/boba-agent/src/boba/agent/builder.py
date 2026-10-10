"""Сборщик запроса: разговор и снимок промпта → `ChatRequest` порта модели.

Алгоритм — docs/prompt_assembly/specs/06_messages_algorithm.md, доведённый до
формы `ChatTurn` (план, раздел 5.12): отбор записей, склейка ответа по
`message.id`, перестановка вложений перед текстом пользователя, слияние
соседних сообщений пользователя, два режима подачи системных вложений,
починка пар вызов — результат, чистка и защита от подделки служебного
блока (TagShield у текстов пользователя и результатов).
Инструменты — из снимка, по имени.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from boba.agent.attachments import AttachmentKind, PromptSnapshot
from boba.agent.profile import ModelProfile, ReasoningReturn
from boba.agent.records import (
    AssistantRecord,
    AttachmentRecord,
    AttachmentRole,
    ChainRecord,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.agent.tags import AgentTag, TagShield
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.llm.chat import ChatRequest, ChatRole, ChatTurn, ToolCall, ToolSpec

__all__ = ["RequestBuilder"]


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


class Feed:
    """Накопитель сообщений в порядке подачи (шаги 4–5 разбора 06)."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.pending_system: list[str] = []

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
        self._shield = TagShield([AgentTag.SYSTEM_REMINDER, AgentTag.TOOL_USE_ERROR])

    def build(
        self, conversation: Sequence[ChainRecord], snapshot: PromptSnapshot
    ) -> ChatRequest:
        feed = Feed()
        for record in self._reordered(list(self._sendable(conversation))):
            self._take(record, feed)

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
            tools=list(self._tools(snapshot)),
            sampling=dict(self._profile.chat.sampling),
            stream=True,
        )

    def _sendable(self, conversation: Sequence[ChainRecord]) -> Iterator[ChainRecord]:
        for record in conversation:
            if isinstance(record, UserRecord):
                yield record
                continue

            if isinstance(record, AssistantRecord):
                if record.is_api_error_message:
                    continue

                yield record
                continue

            if isinstance(record, AttachmentRecord):
                if record.rendered is None:
                    continue

                if record.attachment.type == AttachmentKind.PROMPT_SNAPSHOT.value:
                    continue

                yield record

    def _reordered(self, records: Sequence[ChainRecord]) -> list[ChainRecord]:
        """Вложения встают сразу после ближайшего выше ответа или результата
        инструмента и перед текстом пользователя, за которым записаны."""
        reversed_out: list[ChainRecord] = []
        held: list[ChainRecord] = []
        for record in reversed(records):
            if isinstance(record, AttachmentRecord):
                held.insert(0, record)
                continue

            if self._stop_point(record) and held:
                reversed_out.extend(reversed(held))
                held = []

            reversed_out.append(record)

        reversed_out.extend(reversed(held))
        reversed_out.reverse()

        return reversed_out

    def _stop_point(self, record: ChainRecord) -> bool:
        if isinstance(record, AssistantRecord):
            return True

        if isinstance(record, UserRecord):
            return bool(record.message.tool_results())

        return False

    def _take(self, record: ChainRecord, feed: Feed) -> None:
        if isinstance(record, UserRecord):
            self._take_user(record, feed)
            return

        if isinstance(record, AttachmentRecord):
            self._take_attachment(record, feed)
            return

        if isinstance(record, AssistantRecord):
            self._take_assistant(record, feed)

    def _take_user(self, record: UserRecord, feed: Feed) -> None:
        content = record.message.content
        if isinstance(content, str):
            if content == "":
                return

            feed.last_user().texts.append(self._shield.shield(content))
            return

        if not content:
            return

        turn = feed.last_user()
        for block in content:
            if isinstance(block, ToolResultBlock):
                turn.results.append(block)
                continue

            turn.texts.append(self._shield.shield(block.text))

    def _take_attachment(self, record: AttachmentRecord, feed: Feed) -> None:
        rendered = record.rendered
        if rendered is None:
            return

        texts: list[str] = []
        for message in rendered:
            texts.append(message.content)

        if record.rendered_role is AttachmentRole.USER:
            # вложение с ролью user — слова пользователя, не напоминание
            feed.last_user().texts.extend(texts)
            return

        if self._profile.system_turns:
            for text in texts:
                feed.pending_system.append(AgentTag.SYSTEM_REMINDER.unwrap_block(text))

            return

        feed.last_user().reminders.extend(texts)

    def _take_assistant(self, record: AssistantRecord, feed: Feed) -> None:
        turn = feed.assistant(record.message.id)
        for block in record.message.content:
            if isinstance(block, ThinkingBlock):
                turn.thinking.append(block.thinking)
                continue

            if isinstance(block, TextBlock):
                turn.texts.append(block.text)
                continue

            if isinstance(block, ToolUseBlock):
                turn.calls.append(block)

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
                    content=self._result_text(result),
                    tool_call_id=result.tool_use_id,
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
            yield ChatTurn(role=ChatRole.USER, content=self.TEXT_JOIN.join(texts))

    def _result_text(self, result: ToolResultBlock) -> str:
        text = self._shield.shield(result.text())
        if result.is_error:
            return AgentTag.TOOL_USE_ERROR.wrap(text)

        return text

    def _tools(self, snapshot: PromptSnapshot) -> Iterator[ToolSpec]:
        for tool in sorted(snapshot.tools, key=lambda item: item.name):
            yield ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.schema_),
            )
