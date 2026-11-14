"""Pure validation of ACP SQL results, without transport or execution."""

from typing import Annotated, Any, ClassVar, Literal

from acp.experimental.v2 import schema
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
)


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


ContentBlock = Annotated[
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock,
    Field(discriminator="type"),
]


class MessageUpdate(DataModel):
    """Complete persisted content; replacing the same ID is idempotent."""

    session_update: Literal["user_message", "agent_message", "agent_thought"] = Field(
        alias="sessionUpdate"
    )
    message_id: str = Field(alias="messageId")
    content: tuple[ContentBlock, ...]


class ToolCallUpdate(DataModel):
    """The result patches the original call using the same toolCallId."""

    session_update: Literal["tool_call_update"] = Field(
        default="tool_call_update", alias="sessionUpdate"
    )
    tool_call_id: str = Field(alias="toolCallId")
    name: str | None = None
    title: str | None = None
    kind: Literal["other"] | None = None
    status: Literal["pending", "completed", "failed"]
    raw_input: dict[str, JsonValue] | None = Field(default=None, alias="rawInput")
    content: tuple[schema.ContentToolCallContent, ...] | None = None


class CompactionUpdate(DataModel):
    session_update: Literal["compaction_update"] = Field(
        default="compaction_update", alias="sessionUpdate"
    )
    compaction_id: str = Field(alias="compactionId")
    status: Literal["completed"] = "completed"
    summary: tuple[ContentBlock, ...]


class HistoryBounds(DataModel):
    session_id: str
    after_seq: NonNegativeInt
    through_seq: NonNegativeInt | None
    last_seq: PositiveInt | None


class StateBounds(DataModel):
    session_id: str
    through_seq: NonNegativeInt | None
    last_seq: PositiveInt | None


class Source(DataModel):
    seq: PositiveInt
    record_id: str
    kind: str


class PartSource(Source):
    position: tuple[PositiveInt, ...]
    route: str


class CopySource(Source):
    category: str
    copied_from_id: str
    source_seq: PositiveInt | None
    source_kind: str | None
    source_category: str | None


class CallSource(DataModel):
    seq: PositiveInt
    record_id: str
    call_id: str


class ValidationData(DataModel):
    unmapped_records: tuple[Source, ...]
    parts: tuple[PartSource, ...]
    copies: tuple[CopySource, ...]
    calls: tuple[CallSource, ...]
    results: tuple[CallSource, ...]


class NotificationParams(DataModel):
    session_id: str = Field(alias="sessionId")
    update: dict[str, JsonValue]


class SessionNotification(DataModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["session/update"] = "session/update"
    params: NotificationParams


class NotificationRow(DataModel):
    seq: PositiveInt
    record_id: str
    position: tuple[NonNegativeInt, ...]
    notification: SessionNotification


class AcpHistoryDraft(DataModel):
    bounds: HistoryBounds
    notifications: tuple[NotificationRow, ...]
    validation_data: ValidationData


class SavedSetting(DataModel):
    key: str
    record_id: str = Field(alias="recordId")
    value: dict[str, JsonValue]
    provider: str | None


class SavedPermissionRule(DataModel):
    tool: str
    decision: Literal["allow", "deny", "ask"]


class SavedPermissions(DataModel):
    key: str | None
    mode: Literal[
        "default", "accept_edits", "plan", "dont_ask", "bypass_permissions", "auto"
    ]
    record_id: str = Field(alias="recordId")
    rules: tuple[SavedPermissionRule, ...]


class SavedSessionState(DataModel):
    through_seq: NonNegativeInt = Field(alias="throughSeq")
    settings: tuple[SavedSetting, ...]
    permissions: tuple[SavedPermissions, ...]
    usage: dict[str, NonNegativeInt]


class AcpStateDraft(DataModel):
    bounds: StateBounds
    replay_complete: dict[str, JsonValue]
    update: dict[str, JsonValue]


class ReplayCompleteUpdate(DataModel):
    session_update: Literal["replay_complete"] = Field(
        default="replay_complete", alias="sessionUpdate"
    )


class SubagentUpdate(DataModel):
    session_update: Literal["subagent_update"] = Field(
        default="subagent_update", alias="sessionUpdate"
    )
    session_id: str = Field(alias="sessionId")


class AcpElicitationDraft(DataModel):
    session_id: str
    record_id: str
    seq: PositiveInt | None
    response_count: NonNegativeInt
    completion_count: NonNegativeInt
    method: Literal["elicitation/create"]
    required_mode: Literal["form", "url"] | None
    params: dict[str, JsonValue] | None


class AcpAssemblyError(ValueError):
    def __init__(self, code: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


class AcpHistoryValidator:
    """Validate every row before sending any notification; never mutate the draft."""

    UPDATES: ClassVar = {
        "user_message": MessageUpdate,
        "agent_message": MessageUpdate,
        "agent_thought": MessageUpdate,
        "tool_call_update": ToolCallUpdate,
        "compaction_update": CompactionUpdate,
        "subagent_update": SubagentUpdate,
    }
    PARTS: ClassVar = {
        "user": {"text", "image", "resource"},
        "assistant": {"text", "reasoning", "tool_call"},
        "result": {"text", "image", "resource", "tool_reference"},
        "compact": {"text"},
        "command_result": {"text", "image", "resource"},
        "notice": {"text"},
    }

    @staticmethod
    def bounds(bounds: HistoryBounds | StateBounds) -> None:
        if bounds.through_seq is None or bounds.through_seq > (bounds.last_seq or 0):
            raise AcpAssemblyError("invalid_history_range")
        if isinstance(bounds, HistoryBounds) and bounds.after_seq > bounds.through_seq:
            raise AcpAssemblyError("invalid_history_range")

    def _require_sources(self, data: ValidationData) -> None:
        if data.unmapped_records:
            raise AcpAssemblyError("unmapped_record", records=data.unmapped_records)
        for part in data.parts:
            if part.kind not in self.PARTS.get(part.route, set()):
                raise AcpAssemblyError("unsupported_content", part=part)
        for copy in data.copies:
            if (
                copy.source_seq is None
                or copy.source_seq >= copy.seq
                or copy.source_kind != copy.kind
                or copy.source_category != copy.category
            ):
                raise AcpAssemblyError("invalid_copy_reference", record=copy)

    @staticmethod
    def _require_calls(data: ValidationData) -> None:
        calls = {}
        for call in data.calls:
            if call.call_id in calls:
                raise AcpAssemblyError("duplicate_tool_call", call_id=call.call_id)
            calls[call.call_id] = call
        results = set()
        for result in data.results:
            call = calls.get(result.call_id)
            if call is None or call.seq >= result.seq:
                raise AcpAssemblyError("orphan_tool_result", record=result)
            if result.call_id in results:
                raise AcpAssemblyError("duplicate_tool_result", call_id=result.call_id)
            results.add(result.call_id)

    def require_valid(self, draft: AcpHistoryDraft) -> tuple[NotificationRow, ...]:
        self.bounds(draft.bounds)
        self._require_sources(draft.validation_data)
        self._require_calls(draft.validation_data)
        previous = None
        for row in draft.notifications:
            key = (row.seq, row.position)
            if previous is not None and key <= previous:
                raise AcpAssemblyError("invalid_notification_order")
            previous = key
            if not draft.bounds.after_seq < row.seq <= draft.bounds.through_seq:
                raise AcpAssemblyError("notification_outside_range")
            if row.notification.params.session_id != draft.bounds.session_id:
                raise AcpAssemblyError("session_mismatch")
            update = row.notification.params.update
            model = self.UPDATES.get(update.get("sessionUpdate"))
            if model is None:
                raise AcpAssemblyError("unsupported_update")
            model.model_validate(update)
        return draft.notifications

    def require_state(self, draft: AcpStateDraft) -> SavedSessionState:
        self.bounds(draft.bounds)
        ReplayCompleteUpdate.model_validate(draft.replay_complete)
        schema.AgentMessageUpdate.model_validate(draft.update)
        result = SavedSessionState.model_validate_json(
            draft.update["content"][0]["resource"]["text"]
        )
        if result.through_seq != draft.bounds.through_seq:
            raise AcpAssemblyError("snapshot_mismatch")
        return result

    def require_elicitation(self, draft: AcpElicitationDraft) -> BaseModel:
        if draft.seq is None or draft.params is None:
            raise AcpAssemblyError("elicitation_not_found")
        if draft.response_count or draft.completion_count:
            raise AcpAssemblyError("elicitation_already_answered")
        if draft.params.get("sessionId") != draft.session_id:
            raise AcpAssemblyError("session_mismatch")
        models = {
            "form": schema.CreateFormSessionElicitationRequest,
            "url": schema.CreateUrlSessionElicitationRequest,
        }
        model = models.get(draft.required_mode)
        if model is None:
            raise AcpAssemblyError("unsupported_elicitation_mode")
        return model.model_validate(draft.params)


class AcpNoticeDraft(DataModel):
    session_id: str
    record_id: str
    seq: PositiveInt | None
    update: dict[str, JsonValue]

    def require_valid(self) -> schema.SessionNotice:
        if self.seq is None:
            raise AcpAssemblyError("notice_not_found")
        return schema.SessionNotice.model_validate(self.update)
