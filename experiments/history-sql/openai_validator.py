"""Проверка целевого SQL со статусами задач; общие проверки запроса берутся из агента.

Ошибки:
RequestValidationError — кандидат нарушает контракт сборки.
"""

from collections import defaultdict
from collections.abc import Iterable

from pydantic import PositiveInt

from boba.agent.openai_validator import (
    AdmissionSource,
    AssemblyIssue,
    ContentSource,
    DataModel,
    MessageSource,
    OpenAiRequestDraft as BaseDraft,
    OpenAiRequestValidator as BaseValidator,
    RecordSource,
    ValidationData as BaseValidationData,
    issue,
)


class UnknownCategorySource(RecordSource):
    """Запись активного диапазона, для категории которой SQL не знает правил сборки."""

    category: str


class ToolSource(DataModel):
    call_id: str
    assistant_seq: PositiveInt | None
    assistant_id: str | None
    call_seq: PositiveInt | None
    call_record_id: str | None
    call_assistant_id: str | None
    position: tuple[PositiveInt, ...] | None
    task_id: str | None
    result_seq: PositiveInt | None
    result_id: str | None
    result_call_record_id: str | None
    result_is_error: bool | None
    status_record_id: str | None
    pinned_seq: PositiveInt | None
    pinned_task_id: str | None
    pinned_status: str | None
    task_seq: PositiveInt | None
    source_seq: PositiveInt | None
    source_id: str | None


class ValidationData(BaseValidationData):
    unknown_categories: tuple[UnknownCategorySource, ...]
    tool_sources: tuple[ToolSource, ...]
    admission_sources: tuple[AdmissionSource, ...] = ()


class OpenAiRequestDraft(BaseDraft):
    validation_data: ValidationData


class OpenAiRequestValidator(BaseValidator):
    """Проверяет реальные источники и порядок проекции; ничего не исправляет в запросе."""

    def validate(self, draft: BaseDraft) -> tuple[AssemblyIssue, ...]:
        if not isinstance(draft, OpenAiRequestDraft):
            raise TypeError(
                "Task assembly requires the draft with tool_sources metadata"
            )

        unknown = draft.validation_data.unknown_categories
        if unknown:
            return tuple(self._unknown_categories(unknown))

        errors = list(super().validate(draft))
        errors.extend(self._sources(draft.validation_data.tool_sources))
        return tuple(errors)

    def _unknown_categories(
        self, sources: tuple[UnknownCategorySource, ...]
    ) -> Iterable[AssemblyIssue]:
        for source in sources:
            yield issue("unknown_category", source, category=source.category)

    @staticmethod
    def _wire(draft: BaseDraft) -> Iterable[AssemblyIssue]:
        if not draft.provider:
            yield issue("missing_provider")

        messages = draft.request.get("messages")
        if not isinstance(messages, list):
            yield issue("invalid_messages")
            return

        sources = draft.validation_data.message_sources
        if len(messages) != len(sources):
            yield issue("message_count_mismatch")
            return

        for index, (message, source) in enumerate(zip(messages, sources, strict=True)):
            if not isinstance(message, dict):
                yield issue("invalid_message", index=index)
                continue

            if message.get("role") != source.role:
                yield issue("message_role_mismatch", source, index=index)

            if "content" not in message:
                yield issue("missing_message_content", index=index)

            if message.get("role") != "tool":
                continue

            if not message.get("tool_call_id"):
                yield issue("missing_tool_call_id", index=index)

    def _sources(self, sources: tuple[ToolSource, ...]) -> Iterable[AssemblyIssue]:
        tasks: set[str] = set()
        for source in sources:
            if source.assistant_seq is None:
                if source.call_record_id is not None:
                    yield issue("orphan_tool_call", call_id=source.call_id)
                    continue

                yield issue("orphan_tool_result", call_id=source.call_id)
                continue

            if source.assistant_id is None:
                yield issue("missing_assistant", call_id=source.call_id)
                continue

            origin = RecordSource(
                seq=source.assistant_seq, record_id=source.assistant_id
            )
            if source.call_seq is None:
                yield issue("missing_tool_call_record", origin, call_id=source.call_id)
                continue

            if source.call_assistant_id != source.assistant_id:
                yield issue(
                    "invalid_tool_call_reference", origin, call_id=source.call_id
                )

            if source.call_seq <= source.assistant_seq:
                yield issue("tool_call_before_message", origin, call_id=source.call_id)

            if source.task_id is not None:
                if source.task_id in tasks:
                    yield issue(
                        "duplicate_task_binding", origin, task_id=source.task_id
                    )

                tasks.add(source.task_id)

            if source.result_seq is None:
                continue

            if source.result_call_record_id != source.call_record_id:
                yield issue(
                    "invalid_tool_result_reference", origin, call_id=source.call_id
                )

            if source.result_seq <= source.call_seq:
                yield issue("tool_result_before_call", origin, call_id=source.call_id)

            if source.status_record_id is None:
                if source.task_id is not None:
                    yield issue("missing_final_status", origin, call_id=source.call_id)
                continue

            if source.pinned_seq is None:
                yield issue(
                    "missing_task_status", origin, record_id=source.status_record_id
                )
                continue

            if source.task_id is None:
                continue

            if source.pinned_status in ("pending", "running"):
                yield issue(
                    "unfinished_tool_result", origin, call_id=source.call_id
                )

            failed = source.pinned_status in ("failed", "cancelled", "interrupted")
            if source.result_is_error != failed:
                yield issue(
                    "tool_result_outcome_mismatch", origin, call_id=source.call_id
                )

            if source.pinned_task_id != source.task_id:
                yield issue("task_status_mismatch", origin, call_id=source.call_id)

    def _tool_pairs(
        self, data: BaseValidationData, *, through_seq: int
    ) -> Iterable[AssemblyIssue]:
        calls: dict[str, list[ContentSource]] = defaultdict(list)
        results: dict[str, list[MessageSource]] = defaultdict(list)
        positions: dict[int, int] = {}
        for index, message in enumerate(data.message_sources):
            if message.role == "tool":
                if message.call_id is not None:
                    results[message.call_id].append(message)
                continue

            positions[message.seq] = index

        for part in data.content_sources:
            if part.kind != "tool_call":
                continue

            if part.call_id is None:
                yield issue("missing_tool_call_id", part)
                continue

            calls[part.call_id].append(part)
            index = positions[part.seq]
            following = self._following_results(data.message_sources, index)
            if part.call_id not in following:
                yield issue("missing_tool_result", part, call_id=part.call_id)

        for call_id, parts in calls.items():
            if len(parts) > 1:
                yield issue("duplicate_tool_call", parts[0], call_id=call_id)

        for call_id, messages in results.items():
            if len(messages) > 1:
                yield issue("duplicate_tool_result", messages[0], call_id=call_id)

            if call_id not in calls:
                yield issue("orphan_tool_result", messages[0], call_id=call_id)

    def _following_results(
        self, messages: tuple[MessageSource, ...], index: int
    ) -> set[str]:
        result: set[str] = set()
        for message in messages[index + 1 :]:
            if message.role != "tool":
                break

            if message.call_id is not None:
                result.add(message.call_id)

        return result
