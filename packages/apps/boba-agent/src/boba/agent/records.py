"""Создание и копирование записей истории для runtime и координатора задач.

Ошибки:
ValidationError — данные производителя не соответствуют модели истории.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from boba.agent import models as m
from boba.agent.llm import Generation

R = TypeVar("R", bound=m.RecordBase)


@dataclass(frozen=True)
class AttemptContext:
    """Идентификаторы и время одной HTTP-попытки; ответ получает отдельную операцию."""

    number: int
    operation_id: str
    answer_operation_id: str
    output_id: str
    parent_operation_id: str | None
    service: bool
    started_at: datetime
    started: float


class RecordFactory:
    """Создаёт записи для runtime и координатора; seq назначает HistoryStore."""

    def __init__(self, version: str) -> None:
        self.version = version

    def make(self, model: type[R], *, payload: Any = None, **fields: Any) -> R:
        values: dict[str, Any] = {
            "record_id": uuid4().hex,
            "operation_id": uuid4().hex,
            "operation_position": 0,
            "parent_operation_id": None,
            "created_at": datetime.now(UTC),
            "version": self.version,
            "payload": {},
        }
        for field in ("role", "key"):
            if model.model_fields[field].is_required():
                values[field] = None

        if payload is not None:
            values["payload"] = payload

        values.update(fields)
        return model.model_validate(values)

    def group(
        self,
        records: Sequence[m.HistoryRecord],
        operation_id: str,
        parent_operation_id: str | None = None,
    ) -> tuple[m.HistoryRecord, ...]:
        values = []
        for position, item in enumerate(records):
            data = item.model_dump()
            data.update(
                operation_id=operation_id,
                operation_position=position,
                parent_operation_id=parent_operation_id,
            )
            values.append(m.HISTORY_RECORD_ADAPTER.validate_python(data))

        return tuple(values)

    def attempt(
        self, context: AttemptContext, output: Generation, failure: BaseException | None
    ) -> m.LlmRequestAttemptRecord | None:
        if output.address is None:
            return None

        status = "completed"
        level = "info"
        error = None
        text = "Inference attempt completed."
        if failure is not None:
            status = "failed"
            level = "warning"
            error = f"{type(failure).__name__}: {failure}"
            text = f"Inference attempt failed: {error}"

        if isinstance(failure, asyncio.CancelledError):
            status = "cancelled"

        return self.make(
            m.LlmRequestAttemptRecord,
            operation_id=context.operation_id,
            parent_operation_id=context.parent_operation_id,
            payload={
                "attempt": context.number,
                "status": status,
                "started_at": context.started_at,
                "method": "POST",
                "url": output.address,
                "status_code": output.status,
                "duration_ms": int((time.monotonic() - context.started) * 1000),
                "error": error,
                "level": level,
                "content": [m.TextPart(text=text)],
            },
        )

    def response(
        self, context: AttemptContext, output: Generation, failure: BaseException | None
    ) -> tuple[m.HistoryRecord, ...]:
        completion = "complete"
        if output.finish_reason == "length":
            completion = "output_limit"
        if failure is not None:
            completion = "cancelled"

        content = output.content(partial=completion != "complete")
        answers: list[m.HistoryRecord] = []
        if content:
            model: type[m.AssistantMessageRecord] | type[m.RequestOutputRecord] = (
                m.AssistantMessageRecord
            )
            if context.service:
                model = m.RequestOutputRecord

            answers.append(
                self.make(
                    model,
                    record_id=context.output_id,
                    parent_operation_id=context.operation_id,
                    payload={"content": content, "completion": completion},
                )
            )
            if completion == "complete":
                answers.extend(self.invocations(context.output_id, output))

        if output.usage is not None:
            answers.append(
                self.make(
                    m.TokenUsageRecord,
                    parent_operation_id=context.operation_id,
                    payload={"usage": output.usage},
                )
            )

        return self.group(answers, context.answer_operation_id, context.operation_id)

    def invocations(
        self, output_id: str, output: Generation
    ) -> tuple[m.HistoryRecord, ...]:
        records: list[m.HistoryRecord] = []
        for call in output.calls():
            task_id = uuid4().hex
            if call.name == "task_status":
                task_id = None

            invocation = self.make(
                m.ToolCallRecord,
                key=call.id,
                payload={
                    "assistant_record_id": output_id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                    "task_id": task_id,
                },
            )
            records.append(invocation)
            records.append(
                self.make(
                    m.ToolResultRecord,
                    key=call.id,
                    payload={
                        "call_record_id": invocation.record_id,
                        "status": "pending",
                        "content": [
                            m.TextPart(text=f"Call {call.id}: pending execution.")
                        ],
                    },
                )
            )

        return tuple(records)

    def copy(self, records: Sequence[m.HistoryRecord]) -> tuple[m.HistoryRecord, ...]:
        identifiers = {}
        operations = {}
        positions: dict[str, int] = {}
        for item in records:
            identifiers[item.record_id] = uuid4().hex
            if item.operation_id not in operations:
                operations[item.operation_id] = uuid4().hex
                positions[item.operation_id] = 0

        copied = []
        for item in records:
            value = item.model_dump()
            value.update(
                session_id=None,
                seq=None,
                record_id=identifiers[item.record_id],
                copied_from_id=item.record_id,
                operation_id=operations[item.operation_id],
                operation_position=positions[item.operation_id],
                parent_operation_id=operations.get(
                    item.parent_operation_id, item.parent_operation_id
                ),
            )
            positions[item.operation_id] += 1
            payload = value["payload"]
            for field in (
                "assistant_record_id",
                "call_record_id",
                "source_record_id",
                "catalog_revision",
                "related_message_id",
            ):
                reference = payload.get(field)
                if reference in identifiers:
                    payload[field] = identifiers[reference]

            copied.append(m.HISTORY_RECORD_ADAPTER.validate_python(value))

        return tuple(copied)
