"""Pure validation of openai.sql output; no SQL, I/O or saved state.

OpenAiRequestDraft contains the provider request and compact source metadata.
validate() returns structured issues; require_valid() raises before the caller
can send an invalid candidate to inference. Neither method changes the draft.
"""

import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
)


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordSource(DataModel):
    seq: PositiveInt
    record_id: str


class RequestBoundary(DataModel):
    seq: PositiveInt
    request_id: str


class StateSource(RecordSource):
    """Operations before LAST; request_local/strict_remove are category policies."""

    category: str
    key: str | None
    operation: Literal["set", "remove"]
    request_id: str | None
    request_local: bool
    strict_remove: bool


class SchemaSource(RecordSource):
    key: str
    catalog_revision: str
    catalog_seq: PositiveInt | None
    catalog_key: str | None
    catalog_operation: Literal["set", "remove"] | None
    removed_at: PositiveInt | None


class ContentSource(RecordSource):
    """No text, image bytes, arguments or schemas are repeated here."""

    position: tuple[
        PositiveInt, ...
    ]  # Array ordinals within payload.content, not stored columns.
    kind: str
    role: str
    call_id: str | None
    content_type: (
        str | None
    )  # JSON type: "array", "null", or None for no content field.


class MessageSource(RecordSource):
    section: int
    role: str
    call_id: str | None


class ParameterSource(DataModel):
    name: str
    seq: PositiveInt | None  # Generated fields messages/tools have no single source.
    record_id: str | None


class ValidationData(DataModel):
    """Required arrays: omitting metadata must never silently skip a check."""

    request_boundary: RequestBoundary | None
    compact_seq: NonNegativeInt
    request_boundaries: tuple[RequestBoundary, ...]
    state_sources: tuple[StateSource, ...]
    schema_sources: tuple[SchemaSource, ...]
    content_sources: tuple[ContentSource, ...]
    message_sources: tuple[MessageSource, ...]
    parameter_sources: tuple[ParameterSource, ...]


class OpenAiRequestDraft(DataModel):
    request: dict[str, JsonValue]
    validation_data: ValidationData


class AssemblyIssue(DataModel):
    code: str
    seq: PositiveInt | None = None
    record_id: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class RequestValidationError(ValueError):
    def __init__(self, issues: tuple[AssemblyIssue, ...]):
        self.issues = issues
        super().__init__(
            json.dumps([i.model_dump(mode="json") for i in issues], ensure_ascii=False)
        )


def issue(
    code: str,
    source: RecordSource | ParameterSource | None = None,
    **details: JsonValue,
) -> AssemblyIssue:
    return AssemblyIssue(
        code=code,
        seq=source.seq if source else None,
        record_id=source.record_id if source else None,
        details=details,
    )


class OpenAiRequestValidator:
    """Provider content support and history structure; parameter values stay opaque."""

    SUPPORTED_PARTS = frozenset(
        {
            ("text", "system"),
            ("text", "user"),
            ("text", "assistant"),
            ("text", "tool"),
            ("image", "user"),
            ("tool_call", "assistant"),
            ("resource", "user"),
        }
    )

    def validate(self, draft: OpenAiRequestDraft) -> tuple[AssemblyIssue, ...]:
        data = draft.validation_data
        if data.request_boundary is None:
            return (issue("missing_request"),)

        errors = [
            *self._scopes(data),
            *self._removals(data),
            *self._schemas(data),
            *self._content(data),
            *self._tool_pairs(data),
            *self._parameters(data),
        ]
        if not data.message_sources:
            errors.append(issue("empty_messages"))
        return tuple(
            sorted(
                errors,
                key=lambda e: (
                    e.seq or 0,
                    e.code,
                    json.dumps(e.details, sort_keys=True, ensure_ascii=False),
                ),
            )
        )

    def require_valid(self, draft: OpenAiRequestDraft) -> dict[str, JsonValue]:
        errors = self.validate(draft)
        if errors:
            raise RequestValidationError(errors)
        return draft.request

    @staticmethod
    def _scopes(data: ValidationData) -> Iterable[AssemblyIssue]:
        boundaries = {r.request_id: r.seq for r in data.request_boundaries}
        for source in data.state_sources:
            if not source.request_local:
                continue
            boundary_seq = boundaries.get(source.request_id)
            if boundary_seq is None or boundary_seq <= source.seq:
                yield issue(
                    "invalid_request_scope",
                    source,
                    request_id=source.request_id,
                    boundary_seq=boundary_seq,
                )

    @staticmethod
    def _removals(data: ValidationData) -> Iterable[AssemblyIssue]:
        # Global state is applied first; the selected request's overlay follows it.
        previous = {}
        for source in sorted(
            data.state_sources, key=lambda s: (s.request_local, s.seq)
        ):
            if (
                source.request_local
                and source.request_id != data.request_boundary.request_id
            ):
                continue
            slot = source.category, source.key
            if (
                source.strict_remove
                and source.operation == "remove"
                and previous.get(slot) != "set"
            ):
                yield issue(
                    "inactive_remove", source, category=source.category, key=source.key
                )
            previous[slot] = source.operation

    @staticmethod
    def _schemas(data: ValidationData) -> Iterable[AssemblyIssue]:
        for source in data.schema_sources:
            if (
                source.catalog_seq is None
                or source.catalog_seq >= source.seq
                or source.catalog_key != source.key
                or source.catalog_operation != "set"
                or (source.removed_at is not None and source.removed_at <= source.seq)
            ):
                yield issue(
                    "invalid_dependency",
                    source,
                    key=source.key,
                    catalog_revision=source.catalog_revision,
                    catalog_seq=source.catalog_seq,
                    removed_at=source.removed_at,
                )

    def _content(self, data: ValidationData) -> Iterable[AssemblyIssue]:
        calls = defaultdict(list)
        for source in data.content_sources:
            if source.kind == "tool_call":
                calls[source.seq].append(source)
        for source in data.content_sources:
            if (source.kind, source.role) not in self.SUPPORTED_PARTS or (
                source.kind == "resource" and source.content_type == "null"
            ):
                yield issue(
                    "unsupported_content",
                    source,
                    kind=source.kind,
                    role=source.role,
                    position=list(source.position),
                )
            if source.kind == "text":
                for call in calls[source.seq]:
                    if call.position < source.position:
                        yield issue(
                            "unrepresentable_content_order",
                            source,
                            position=list(source.position),
                            call_position=list(call.position),
                        )

    def _tool_pairs(self, data: ValidationData) -> Iterable[AssemblyIssue]:
        calls, results = defaultdict(list), defaultdict(list)
        for source in data.content_sources:
            if source.kind == "tool_call":
                calls[source.call_id].append(source)
        for source in data.message_sources:
            if source.role == "tool":
                results[source.call_id].append(source)

        yield from self._tool_id_issues(calls, results)
        steps = sorted(
            s.seq for s in data.message_sources if s.section == 1 and s.role != "tool"
        )
        next_step = dict(
            zip(steps, [*steps[1:], data.request_boundary.seq + 1], strict=False)
        )
        for call_id, sources in calls.items():
            for source in sources:
                following = next_step.get(source.seq)
                if following is not None and not any(
                    source.seq < result.seq < following for result in results[call_id]
                ):
                    yield issue("missing_tool_result", source, call_id=call_id)

    @staticmethod
    def _tool_id_issues(calls, results) -> Iterable[AssemblyIssue]:
        for code, groups in (
            ("duplicate_tool_call", calls),
            ("duplicate_tool_result", results),
        ):
            for call_id, sources in groups.items():
                if len(sources) > 1:
                    yield issue(
                        code,
                        min(sources, key=lambda s: s.seq),
                        call_id=call_id,
                        source_seqs=[s.seq for s in sources],
                    )
        for call_id, sources in results.items():
            for source in sources:
                if not any(call.seq < source.seq for call in calls[call_id]):
                    yield issue("orphan_tool_result", source, call_id=call_id)

    @staticmethod
    def _parameters(data: ValidationData) -> Iterable[AssemblyIssue]:
        fields = defaultdict(list)
        for source in data.parameter_sources:
            fields[source.name].append(source)
        for name, sources in fields.items():
            if len(sources) > 1:
                # SQL min(seq) ignores NULL origins of generated fields.
                source = min(
                    (s for s in sources if s.seq is not None),
                    key=lambda s: s.seq,
                    default=None,
                )
                yield issue(
                    "duplicate_parameter",
                    source,
                    field=name,
                    sources=[s.model_dump(mode="json") for s in sources],
                )
