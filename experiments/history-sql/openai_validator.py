"""Проверяет результат SQL по целевым operation_id, scope и результатам вызовов.

Ошибки:
RequestValidationError — кандидат запроса нарушает контракт истории или OpenAI.
pydantic.ValidationError — SQL вернул метаданные неверной формы.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Literal

from pydantic import JsonValue, NonNegativeInt, PositiveInt, ValidationError

from boba.agent.openai_validator import (
    AssemblyIssue,
    DataModel,
    ParameterSource,
    RecordSource,
    RequestValidationError,
    issue,
)


class Category(StrEnum):
    MESSAGE = "message"
    ASSEMBLY = "assembly"
    COMPACT = "compact"
    PROMPT = "prompt"
    CONTEXT = "context"
    SETTING = "setting"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_CATALOG = "tool_catalog"
    TOOL_SCHEMA = "tool_schema"

    @property
    def scoped(self) -> bool:
        return self in (
            self.MESSAGE,
            self.ASSEMBLY,
            self.PROMPT,
            self.CONTEXT,
            self.SETTING,
        )


class Selection(DataModel):
    session_id: str
    operation_id: str | None
    seq: int
    last_seq: NonNegativeInt
    operation_seq: PositiveInt | None
    compact_seq: NonNegativeInt
    assembly_seq: PositiveInt | None
    assembly_id: str | None
    mode: str
    from_seq: int | None
    through_seq: int | None


class InputType(DataModel):
    category: str
    scope: str | None
    seq: PositiveInt


class Removal(RecordSource):
    category: str
    key: str
    operation: str
    previous_operation: str | None


class Schema(RecordSource):
    key: str
    catalog_revision: str
    catalog_seq: PositiveInt | None
    catalog_category: str | None
    catalog_key: str | None
    catalog_operation: str | None
    removed_at: PositiveInt | None


class Call(DataModel):
    assistant_seq: PositiveInt | None
    assistant_id: str | None
    position: PositiveInt | None
    call_id: str
    call_seq: PositiveInt | None
    call_record_id: str | None
    call_assistant_id: str | None
    task_id: str | None
    result_seq: PositiveInt | None
    result_id: str | None
    result_call_record_id: str | None
    status: str | None
    source_record_id: str | None
    source_seq: PositiveInt | None
    source_category: str | None
    source_parent_id: str | None
    own_content_count: NonNegativeInt | None
    own_state_type: str | None


class Content(RecordSource):
    role: str
    ordering: tuple[int, ...]
    position: tuple[PositiveInt, ...]
    kind: str
    signature_format: str | None
    content_type: str | None


class Message(RecordSource):
    role: str
    ordering: tuple[int, ...]
    call_id: str | None


class ValidationData(DataModel):
    selection: Selection
    input_types: tuple[InputType, ...]
    removals: tuple[Removal, ...]
    schemas: tuple[Schema, ...]
    calls: tuple[Call, ...]
    content: tuple[Content, ...]
    messages: tuple[Message, ...]
    parameters: tuple[ParameterSource, ...]


class OpenAiRequestDraft(DataModel):
    provider: str | None
    request: dict[str, JsonValue]
    validation_data: ValidationData


class FunctionCall(DataModel):
    name: str
    arguments: str


class WireCall(DataModel):
    id: str
    type: str
    function: FunctionCall


class WireMessage(DataModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: JsonValue
    tool_call_id: str | None = None
    tool_calls: tuple[WireCall, ...] = ()
    reasoning_content: str | None = None


class OpenAiRequestValidator:
    """Чистая проверка источников и собранного тела; исходные данные не исправляет."""

    def __init__(self) -> None:
        self._parts = {
            "system": frozenset(("text",)),
            "user": frozenset(("text", "image", "resource")),
            "assistant": frozenset(
                ("text", "reasoning", "resource", "tool_call_reference")
            ),
            "tool": frozenset(("text", "resource", "tool_reference")),
        }
        self._statuses = frozenset(
            ("pending", "running", "completed", "failed", "cancelled", "interrupted")
        )

    def validate(self, draft: OpenAiRequestDraft) -> tuple[AssemblyIssue, ...]:
        initial = tuple(self._selection(draft.validation_data.selection))
        if initial:
            return initial

        return tuple(self._issues(draft))

    def require_valid(self, draft: OpenAiRequestDraft) -> dict[str, JsonValue]:
        errors = self.validate(draft)
        if errors:
            raise RequestValidationError(errors)

        return draft.request

    def _selection(self, value: Selection) -> Iterable[AssemblyIssue]:
        if not 0 <= value.seq <= value.last_seq:
            yield issue(
                "invalid_history_range", through_seq=value.seq, last_seq=value.last_seq
            )
            return

        if value.operation_id is not None:
            if value.operation_seq is None:
                yield issue("unknown_operation", operation_id=value.operation_id)

        if value.mode not in ("conversation", "isolated"):
            yield issue("unknown_assembly_mode", mode=value.mode)

        if value.mode == "isolated":
            bounded = (value.from_seq is not None, value.through_seq is not None)
            if any(bounded):
                yield issue("isolated_context_has_range")

            return

        lower = value.compact_seq
        if value.from_seq is not None:
            lower = value.from_seq

        upper = value.seq
        if value.through_seq is not None:
            upper = value.through_seq

        if not value.compact_seq <= lower <= upper <= value.seq:
            yield issue("invalid_context_range", from_seq=lower, through_seq=upper)

    def _issues(self, draft: OpenAiRequestDraft) -> Iterable[AssemblyIssue]:
        data = draft.validation_data
        for source in data.input_types:
            try:
                category = Category(source.category)
            except ValueError:
                yield AssemblyIssue(
                    code="unknown_category",
                    seq=source.seq,
                    details={"category": source.category},
                )
                continue

            if category.scoped:
                if source.scope not in ("session", "request"):
                    yield AssemblyIssue(
                        code="invalid_scope",
                        seq=source.seq,
                        details={"category": source.category},
                    )
                continue

            if source.scope is not None:
                yield AssemblyIssue(
                    code="invalid_scope",
                    seq=source.seq,
                    details={"category": source.category},
                )

        for source in data.removals:
            if source.previous_operation != "set":
                yield issue(
                    "inactive_remove", source, category=source.category, key=source.key
                )

        yield from self._schemas(data.schemas)
        yield from self._calls(data.calls)
        yield from self._content(data.content)
        yield from self._wire(draft)
        yield from self._parameters(draft)

    def _schemas(self, schemas: tuple[Schema, ...]) -> Iterable[AssemblyIssue]:
        for source in schemas:
            if source.catalog_seq is None:
                yield issue("invalid_dependency", source, key=source.key)
                continue

            valid_origin = (
                source.catalog_category == "tool_catalog",
                source.catalog_operation == "set",
                source.catalog_key == source.key,
                source.catalog_seq < source.seq,
            )
            if not all(valid_origin):
                yield issue("invalid_dependency", source, key=source.key)
                continue

            if source.removed_at is not None:
                if source.removed_at <= source.seq:
                    yield issue("invalid_dependency", source, key=source.key)

    def _calls(self, calls: tuple[Call, ...]) -> Iterable[AssemblyIssue]:
        seen: set[str] = set()
        tasks: set[str] = set()
        for source in calls:
            if source.call_id in seen:
                yield issue("duplicate_tool_call", call_id=source.call_id)

            seen.add(source.call_id)
            if source.assistant_seq is None:
                yield issue("orphan_tool_result", call_id=source.call_id)
                continue

            if source.call_seq is None:
                yield issue("missing_tool_call_record", call_id=source.call_id)
                continue

            if source.assistant_id != source.call_assistant_id:
                yield issue("invalid_tool_call_reference", call_id=source.call_id)

            if source.call_seq <= source.assistant_seq:
                yield issue("tool_call_before_message", call_id=source.call_id)

            if source.task_id is not None:
                if source.task_id in tasks:
                    yield issue("duplicate_task_binding", task_id=source.task_id)
                tasks.add(source.task_id)

            yield from self._result(source, source.call_seq)

    def _result(self, source: Call, call_seq: int) -> Iterable[AssemblyIssue]:
        if source.result_seq is None:
            yield issue("missing_tool_result", call_id=source.call_id)
            return

        if source.result_call_record_id != source.call_record_id:
            yield issue("invalid_tool_result_reference", call_id=source.call_id)

        if source.result_seq <= call_seq:
            yield issue("tool_result_before_call", call_id=source.call_id)

        if source.status not in self._statuses:
            yield issue("invalid_tool_status", call_id=source.call_id)

        if source.source_record_id is None:
            return

        if source.source_seq is None:
            yield issue("missing_result_source", record_id=source.source_record_id)
            return

        source_checks = (
            source.source_category == "tool_result",
            source.source_seq < source.result_seq,
            source.source_parent_id is None,
            source.own_content_count == 0,
            source.own_state_type == "null",
        )
        if not all(source_checks):
            yield issue("invalid_result_source", record_id=source.source_record_id)

    def _content(self, parts: tuple[Content, ...]) -> Iterable[AssemblyIssue]:
        call_positions: dict[tuple[int, ...], tuple[int, ...]] = {}
        for source in parts:
            if source.kind == "tool_call_reference":
                call_positions.setdefault(source.ordering, source.position)
                continue

            first_call = call_positions.get(source.ordering)
            if first_call is not None:
                if source.kind in ("text", "image", "resource", "reasoning"):
                    yield issue("unrepresentable_content_order", source)

        for source in parts:
            if source.kind not in self._parts.get(source.role, ()):
                yield issue(
                    "unsupported_content", source, kind=source.kind, role=source.role
                )

            if source.kind == "resource":
                if source.content_type != "array":
                    yield issue("unsupported_content", source, kind=source.kind)

            if source.signature_format is not None:
                yield issue(
                    "unsupported_reasoning_signature",
                    source,
                    format=source.signature_format,
                )

    def _wire(self, draft: OpenAiRequestDraft) -> Iterable[AssemblyIssue]:
        if not draft.provider:
            yield issue("missing_provider")

        raw = draft.request.get("messages")
        if not isinstance(raw, list):
            yield issue("invalid_messages")
            return

        if not raw:
            yield issue("empty_messages")

        sources = draft.validation_data.messages
        if len(raw) != len(sources):
            yield issue("message_count_mismatch")
            return

        yield from self._exchange(raw, sources)

    def _exchange(
        self, raw: Sequence[JsonValue], sources: Sequence[Message]
    ) -> Iterable[AssemblyIssue]:
        pending: set[str] = set()
        for value, source in zip(raw, sources, strict=True):
            try:
                message = WireMessage.model_validate(value)
            except ValidationError:
                yield issue("invalid_message", source)
                continue

            if message.role != source.role:
                yield issue("message_role_mismatch", source)

            if message.role == "tool":
                if message.tool_call_id not in pending:
                    yield issue("orphan_tool_result", source)
                    continue

                pending.remove(message.tool_call_id)
                continue

            for call_id in sorted(pending):
                yield issue("missing_tool_result", source, call_id=call_id)
            pending.clear()

            for call in message.tool_calls:
                pending.add(call.id)

        for call_id in sorted(pending):
            yield issue("missing_tool_result", call_id=call_id)

    def _parameters(self, draft: OpenAiRequestDraft) -> Iterable[AssemblyIssue]:
        fields: Counter[str] = Counter()
        for source in draft.validation_data.parameters:
            fields[source.name] += 1

        if "model" not in fields:
            yield issue("missing_model")

        for name, count in fields.items():
            if count > 1:
                yield issue("duplicate_parameter", field=name)

        yield from self._tool_choice(draft)

    def _tool_choice(self, draft: OpenAiRequestDraft) -> Iterable[AssemblyIssue]:
        choice = draft.request.get("tool_choice")
        if choice == "required":
            if not draft.request.get("tools"):
                yield issue("unavailable_tool_choice")
            return

        if not isinstance(choice, dict):
            return

        function = choice.get("function")
        if not isinstance(function, dict):
            return

        name = function.get("name")
        for source in draft.validation_data.schemas:
            if source.key != name:
                continue

            if source.removed_at is None:
                return

        yield issue("unavailable_tool_choice", name=name)
