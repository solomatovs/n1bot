"""Типизированные строки проприетарной истории; проекции выполняют SQL-адаптеры."""

from __future__ import annotations

import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import (
    Annotated,
    Literal,
    Self,
    TypeAlias,
    TypeVar,
    get_args,
)

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
JsonObject: TypeAlias = dict[str, JsonValue]
T = TypeVar("T")
NonEmptyTuple: TypeAlias = Annotated[tuple[T, ...], Field(min_length=1)]


class DataModel(BaseModel):
    """Общие настройки Pydantic; предметные данные не зависят от инфраструктуры."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


HistoryCategory = Literal[
    "message",  # SUM сообщений сессии и собственных входов выбранной операции.
    "assembly",  # LAST условий по key выбранной операции; управляет отбором.
    "tool_call",  # SUM определений вызовов из включённых assistant-сообщений.
    "tool_result",  # LAST состояния и содержимого ответа по key=call_id.
    "compact",  # LAST границы; пересказ входит первым сообщением активного диалога.
    "prompt",  # LAST set/remove по key секции инструкций; role=system.
    "context",  # LAST set/remove по key объявления; role=user.
    "setting",  # LAST целого набора параметров по key=model/sampling/...
    "tool_catalog",  # LAST доступности инструмента по полному имени.
    "tool_schema",  # LAST схемы внутри непрерывной доступности имени.
    "operational",  # Учёт исполнения, команды, разрешения и прочие служебные данные.
]

CATEGORIES = get_args(HistoryCategory)


class TextPart(DataModel):
    """Текст сохраняется буквально, включая пустую строку."""

    kind: Literal["text"] = "text"
    text: str


class ImagePart(DataModel):
    """Изображение уже загружено; в JSON байты представлены base64."""

    kind: Literal["image"] = "image"
    media_type: NonEmptyStr
    data: bytes


class ResourcePart(DataModel):
    """Ссылка на ресурс и уже полученное содержимое целиком либо фрагментом.

    content=None — ресурс не загружен; () — загруженный пустой ресурс."""

    kind: Literal["resource"] = "resource"
    name: NonEmptyStr
    uri: NonEmptyStr
    media_type: NonEmptyStr | None
    content: (
        tuple[Annotated[TextPart | ImagePart, Field(discriminator="kind")], ...] | None
    )
    line_start: PositiveInt | None = None
    line_end: PositiveInt | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def valid_fragment(self) -> Self:
        if self.truncated:
            if self.content is None:
                raise ValueError("Only loaded content can be truncated")

        if self.line_start is None:
            if self.line_end is not None:
                raise ValueError("line_start and line_end must be set together")

            return self

        if self.line_end is None:
            raise ValueError("line_start and line_end must be set together")

        if self.line_end < self.line_start:
            raise ValueError("line_end precedes line_start")

        return self


class ReasoningPart(DataModel):
    """Рассуждение; подпись и её формат передаются без интерпретации."""

    kind: Literal["reasoning"] = "reasoning"
    text: str
    signature: NonEmptyStr | None
    format: NonEmptyStr | None

    @model_validator(mode="after")
    def signature_has_format(self) -> Self:
        if (self.signature is None) != (self.format is None):
            raise ValueError("signature and format must be set together")
        return self


class ToolCallReferencePart(DataModel):
    """
    Позиция вызова внутри assistant-сообщения; определение хранится в ToolCallRecord.
    """

    kind: Literal["tool_call_reference"] = "tool_call_reference"
    call_id: NonEmptyStr


class ToolReferencePart(DataModel):
    """Ссылка на найденный инструмент и использованную запись каталога."""

    kind: Literal["tool_reference"] = "tool_reference"
    name: NonEmptyStr
    catalog_revision: NonEmptyStr


ContentPart = Annotated[
    TextPart | ImagePart | ResourcePart,
    Field(discriminator="kind"),
]
MessagePart = Annotated[
    TextPart | ImagePart | ResourcePart | ReasoningPart | ToolCallReferencePart,
    Field(discriminator="kind"),
]
ToolResultPart = Annotated[
    TextPart  # Текст результата, ошибки или состояния задачи.
    | ImagePart  # Изображение, возвращённое инструментом, например скриншот.
    | ResourcePart  # Возвращённый ресурс: URI и полученное содержимое.
    | ToolReferencePart,  # Результат tool_search: ссылка на выбранный инструмент.
    Field(discriminator="kind"),
]


class ToolCatalogItem(DataModel):
    """Краткое описание инструмента; полное имя хранится в ToolAddedRecord.key."""

    description: str


class ToolDefinition(DataModel):
    """Полная схема инструмента; имя хранится в ToolSchemaRecord.key."""

    description: str
    parameters: JsonObject
    deferred: bool


class SkillDefinition(DataModel):
    """Доступный навык; инструкции могут быть получены заранее или при активации."""

    name: NonEmptyStr
    description: str
    source_uri: NonEmptyStr | None = None
    instructions: str | None = None  # None — ещё не получены; пустая строка — получены.

    @model_validator(mode="after")
    def has_source_or_instructions(self) -> Self:
        if self.source_uri is None and self.instructions is None:
            raise ValueError("A skill requires a source or saved instructions")
        return self


class AgentCatalogItem(DataModel):
    """
    Описание доступного типа агента; конфигурация запуска фиксируется в дочерней
    истории.
    """

    name: NonEmptyStr
    description: str
    source_uri: NonEmptyStr | None = None


# Общие значения стадий; поле status относится к исполнению своего вызова или задачи.
ExecutionStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled", "interrupted"
]


class TaskSnapshot(DataModel):
    """
    Наблюдение задачи task_id из API; revision меняется worker'ом, status относится к
    задаче.
    """

    task_id: NonEmptyStr
    revision: PositiveInt
    description: NonEmptyStr
    status: ExecutionStatus
    execution_id: NonEmptyStr | None = None
    child_session_id: NonEmptyStr | None = (
        None  # Диалог сабагента, если вызов запускает его.
    )
    output_uri: NonEmptyStr | None = None
    result: JsonValue = None
    error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        failed = self.status in ("failed", "interrupted")
        has_error = self.error is not None
        if failed != has_error:
            raise ValueError("Failed and interrupted tasks require an error")

        if self.status not in ("pending", "running"):
            return self

        if self.result is not None:
            raise ValueError("An unfinished task cannot have a final result")

        return self


class EnvironmentState(DataModel):
    """
    Последние наблюдённые свойства одной среды; None означает неизвестное значение.
    """

    working_directory: NonEmptyStr | None  # cwd: один текущий каталог указанной среды.
    workspace_roots: tuple[
        NonEmptyStr, ...
    ]  # Корни открытых проектов; () — не сообщены.
    platform: NonEmptyStr | None
    os_version: NonEmptyStr | None
    shell: NonEmptyStr | None
    temporary_directory: NonEmptyStr | None


class RecordBase(DataModel):
    """
    Одна строка истории; производитель задаёт операцию, хранилище — session_id и seq.
    """

    class Payload(DataModel):
        pass

    session_id: NonEmptyStr | None = None
    seq: PositiveInt | None = None
    record_id: NonEmptyStr
    operation_id: NonEmptyStr
    operation_position: NonNegativeInt
    parent_operation_id: NonEmptyStr | None

    kind: NonEmptyStr
    category: HistoryCategory = "operational"
    role: Literal["system", "user", "assistant", "tool"] | None
    key: NonEmptyStr | None
    operation: Literal["set", "remove"] = "set"
    copied_from_id: NonEmptyStr | None = None

    created_at: AwareDatetime
    version: NonEmptyStr
    payload: Payload

    @model_validator(mode="after")
    def valid_address_and_parent(self) -> Self:
        if (self.session_id is None) != (self.seq is None):
            raise ValueError("session_id and seq must be assigned together")

        if self.parent_operation_id == self.operation_id:
            raise ValueError("An operation cannot be its own parent")

        return self


class MessageRecord(RecordBase):
    """Общий формат содержимого и проверка роли сообщений категории message.

    Наследники: UserMessageRecord, RequestMessageRecord, ResourceMessageRecord,
    AssistantMessageRecord. Наследники дополняют payload, не переопределяя content.
    """

    class Payload(RecordBase.Payload):
        content: NonEmptyTuple[MessagePart]

    category: Literal["message"] = "message"
    scope: Literal["session", "request"] = "session"
    role: Literal["user", "assistant"]
    operation: Literal["set"] = "set"
    payload: Payload

    @model_validator(mode="after")
    def valid_content_role(self) -> Self:
        if self.role == "assistant":
            return self

        for part in self.payload.content:
            if isinstance(part, (ReasoningPart, ToolCallReferencePart)):
                raise ValueError(
                    f"message role {self.role!r} cannot contain {part.kind!r}"
                )

        return self


class PromptRecord(RecordBase):
    """Общий контракт добавления и удаления системных инструкций по key."""

    class Payload(RecordBase.Payload):
        content: tuple[TextPart, ...] = ()

    category: Literal["prompt"] = "prompt"
    scope: Literal["session", "request"] = "session"
    role: Literal["system"] = "system"
    key: NonEmptyStr
    payload: Payload

    @model_validator(mode="after")
    def content_matches_operation(self) -> Self:
        if self.operation == "remove":
            if self.payload.content:
                raise ValueError("Removed prompt must have empty content")

            return self

        if not self.payload.content:
            raise ValueError("Active prompt requires content")

        return self


class ContextRecord(RecordBase):
    """Общий контракт skills, memory, среды и других заменяемых объявлений по key."""

    class Payload(RecordBase.Payload):
        content: tuple[TextPart, ...] = ()

    category: Literal["context"] = "context"
    scope: Literal["session", "request"] = "session"
    role: Literal["user"] = "user"
    key: NonEmptyStr
    payload: Payload

    @model_validator(mode="after")
    def content_matches_operation(self) -> Self:
        if self.operation == "remove":
            if self.payload.content:
                raise ValueError("Removed context must have empty content")

            return self

        if not self.payload.content:
            raise ValueError("Active context requires content")

        return self


class SystemPromptAddedRecord(PromptRecord):
    """
    Сохранённая секция системных инструкций по key; scope определяет область действия.
    """

    class Payload(PromptRecord.Payload):
        source_uri: NonEmptyStr | None = (
            None  # Источник инструкции; LLM-сборщик не читает его.
        )

    kind: Literal["system_prompt_added"] = "system_prompt_added"
    operation: Literal["set"] = "set"
    payload: Payload


class SystemPromptRemovedRecord(PromptRecord):
    """
    Снять секцию по key в области scope; для request отмена действует в выбранной
    операции.
    """

    kind: Literal["system_prompt_removed"] = "system_prompt_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class SkillAddedRecord(ContextRecord):
    """
    Каталог навыка с готовым объявлением; полные инструкции сохраняет
    SkillActivatedRecord.
    """

    class Payload(ContextRecord.Payload):
        skill: SkillDefinition

    kind: Literal["skill_added"] = "skill_added"
    operation: Literal["set"] = "set"
    payload: Payload


class SkillRemovedRecord(ContextRecord):
    """Снять доступность навыка; ранее активированные инструкции сохраняются."""

    kind: Literal["skill_removed"] = "skill_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class SkillActivatedRecord(PromptRecord):
    """
    Полученные инструкции навыка с подставленными аргументами; отменяются
    SystemPromptRemovedRecord по key.
    """

    class Payload(PromptRecord.Payload):
        name: NonEmptyStr
        source_uri: NonEmptyStr | None = None

    kind: Literal["skill_activated"] = "skill_activated"
    operation: Literal["set"] = "set"
    payload: Payload


class MemoryAddedRecord(ContextRecord):
    """
    Готовый текст выбранной памяти до замены или отмены; source_uri хранит источник без
    чтения при сборке.
    """

    class Payload(ContextRecord.Payload):
        source_uri: NonEmptyStr | None = None

    kind: Literal["memory_added"] = "memory_added"
    operation: Literal["set"] = "set"
    payload: Payload


class MemoryRemovedRecord(ContextRecord):
    """Прекратить включение выбранной памяти в запросы; исходные записи остаются."""

    kind: Literal["memory_removed"] = "memory_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class AgentAddedRecord(ContextRecord):
    """Полный снимок одного доступного типа агента и готовое объявление для модели."""

    class Payload(ContextRecord.Payload):
        agent: AgentCatalogItem

    kind: Literal["agent_added"] = "agent_added"
    operation: Literal["set"] = "set"
    payload: Payload


class AgentRemovedRecord(ContextRecord):
    """Снять тип агента из каталога; уже запущенные задачи продолжают работу."""

    kind: Literal["agent_removed"] = "agent_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class EnvironmentRecord(ContextRecord):
    """Полный снимок одной среды; state=None явно снимает её объявление."""

    class Payload(ContextRecord.Payload):
        state: EnvironmentState | None

    kind: Literal["environment"] = "environment"
    payload: Payload

    @model_validator(mode="after")
    def description_matches_state(self) -> Self:
        if (self.operation == "remove") != (self.payload.state is None):
            raise ValueError("Removed environment must have operation=remove")
        return self


class DateRecord(ContextRecord):
    """Наблюдённая календарная дата в выбранной зоне; отличается от времени записи."""

    class Payload(ContextRecord.Payload):
        date: datetime.date
        timezone: NonEmptyStr

    kind: Literal["date"] = "date"
    key: NonEmptyStr = "calendar.current"
    operation: Literal["set"] = "set"
    payload: Payload


class ToolAddedRecord(RecordBase):
    """Инструмент доступен агенту по полному имени."""

    class Payload(RecordBase.Payload):
        tool: ToolCatalogItem

    kind: Literal["tool_added"] = "tool_added"
    category: Literal["tool_catalog"] = "tool_catalog"
    operation: Literal["set"] = "set"
    key: NonEmptyStr  # Полное имя инструмента.
    payload: Payload


class ToolRemovedRecord(RecordBase):
    """
    Инструмент отсутствует в актуальном каталоге; remove также исключает сохранённую
    схему.
    """

    kind: Literal["tool_removed"] = "tool_removed"
    category: Literal["tool_catalog"] = "tool_catalog"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class ToolSchemaRecord(RecordBase):
    """
    Схема, полученная через tool_search; LAST по имени после последнего удаления из
    каталога.
    """

    class Payload(RecordBase.Payload):
        catalog_revision: (
            NonEmptyStr  # record_id add, подтверждающего доступность инструмента.
        )
        tool: ToolDefinition

    kind: Literal["tool_schema"] = "tool_schema"
    category: Literal["tool_schema"] = "tool_schema"
    operation: Literal["set"] = "set"
    key: NonEmptyStr  # Полное имя инструмента.
    payload: Payload


class ParameterChoice(DataModel):
    """Допустимое значение параметра агента; value_id адресует выбор клиента."""

    value_id: NonEmptyStr
    name: NonEmptyStr
    value: JsonValue


class ParameterOptionRecord(RecordBase):
    """Объявляет допустимые изменения одного параметра; LAST по key.

    source_kinds/source_key адресуют обновления и удаления постоянной настройки.
    value_path — путь внутри payload; пустой путь выбирает payload целиком.
    Текущее значение берётся из снимка, а не дублируется в описании параметра.
    """

    class Payload(RecordBase.Payload):
        name: NonEmptyStr
        description: str | None = None
        source_kinds: NonEmptyTuple[NonEmptyStr]
        source_key: NonEmptyStr | None = None
        value_path: tuple[NonEmptyStr, ...]
        choices: NonEmptyTuple[ParameterChoice]

        def choose(self, value_id: str) -> ParameterChoice:
            for choice in self.choices:
                if choice.value_id == value_id:
                    return choice

            raise ValueError(f"Unknown parameter choice {value_id!r}")

        @model_validator(mode="after")
        def unique_choices(self) -> Self:
            if len({c.value_id for c in self.choices}) != len(self.choices):
                raise ValueError("Parameter choice IDs must be unique")
            return self

    kind: Literal["parameter_option"] = "parameter_option"
    key: NonEmptyStr
    payload: Payload


class ParameterOptionRemovedRecord(RecordBase):
    """Убирает возможность менять параметр; сохранённое значение настройки не меняет."""

    kind: Literal["parameter_option_removed"] = "parameter_option_removed"
    key: NonEmptyStr
    operation: Literal["remove"] = "remove"


class ParameterRecord(RecordBase):
    """
    Атомарный набор полей инференса по key; производитель проверяет значения, сборщик
    переносит их буквально.
    """

    class Payload(RecordBase.Payload):
        value: Annotated[JsonObject, Field(min_length=1)]

    category: Literal["setting"] = "setting"
    scope: Literal["session", "request"] = "session"
    operation: Literal["set"] = "set"
    key: NonEmptyStr
    payload: Payload

    @model_validator(mode="after")
    def parameters_only(self) -> Self:
        if self.payload.value.keys() & {"messages", "tools"}:
            raise ValueError(
                "messages and tools are assembled from their own history records"
            )
        return self


class ParameterRemovedRecord(RecordBase):
    """
    Отмена набора по key в области scope; пустой payload не содержит новых значений.
    """

    category: Literal["setting"] = "setting"
    scope: Literal["session", "request"] = "session"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class ModelRecord(ParameterRecord):
    """Выбранный инференс и готовые поля выбора модели.

    В категории setting ключ model зарезервирован для выбора модели и подключения.
    payload.value входит в тело запроса; payload.provider адресует личное подключение.
    При provider=None сборка возвращает missing_provider до выбора подключения.
    """

    class Payload(ParameterRecord.Payload):
        provider: (
            NonEmptyStr | None
        )  # Личное подключение; None до выбора пользователем.
        profile_id: NonEmptyStr | None = None  # Выбранный пресет настроек модели.

    kind: Literal["model"] = "model"
    key: Literal["model"] = "model"
    payload: Payload


class SamplingRecord(ParameterRecord):
    """Параметры выборки в форме выбранного инференса."""

    kind: Literal["sampling"] = "sampling"
    key: Literal["sampling"] = "sampling"


class SamplingRemovedRecord(ParameterRemovedRecord):
    """Параметры выборки: отмена постоянно либо для выбранного operation_id."""

    kind: Literal["sampling_removed"] = "sampling_removed"
    key: Literal["sampling"] = "sampling"


class InferenceRecord(ParameterRecord):
    """Лимиты вывода и параметры reasoning в форме выбранного инференса."""

    kind: Literal["inference"] = "inference"
    key: Literal["inference"] = "inference"


class InferenceRemovedRecord(ParameterRemovedRecord):
    """Параметры инференса: отмена постоянно либо для выбранного operation_id."""

    kind: Literal["inference_removed"] = "inference_removed"
    key: Literal["inference"] = "inference"


class ProviderParametersRecord(ParameterRecord):
    """Прочие поля запроса выбранного инференса."""

    kind: Literal["provider_parameters"] = "provider_parameters"
    key: Literal["provider_parameters"] = "provider_parameters"


class ProviderParametersRemovedRecord(ParameterRemovedRecord):
    """Параметры провайдера: отмена постоянно либо для выбранного operation_id."""

    kind: Literal["provider_parameters_removed"] = "provider_parameters_removed"
    key: Literal["provider_parameters"] = "provider_parameters"


class ResponseFormatRecord(ParameterRecord):
    """Формат генерируемого ответа одного запроса."""

    kind: Literal["response_format"] = "response_format"
    scope: Literal["request"] = "request"
    key: Literal["response_format"] = "response_format"


class ResponseFormatRemovedRecord(ParameterRemovedRecord):
    """Формат ответа: отмена для одного запроса."""

    kind: Literal["response_format_removed"] = "response_format_removed"
    scope: Literal["request"] = "request"
    key: Literal["response_format"] = "response_format"


class ToolChoiceRecord(ParameterRecord):
    """Политика выбора tools для одного запроса в форме инференса."""

    kind: Literal["tool_choice"] = "tool_choice"
    scope: Literal["request"] = "request"
    key: Literal["tool_choice"] = "tool_choice"


class ToolChoiceRemovedRecord(ParameterRemovedRecord):
    """Выбор tools: отмена для одного запроса."""

    kind: Literal["tool_choice_removed"] = "tool_choice_removed"
    scope: Literal["request"] = "request"
    key: Literal["tool_choice"] = "tool_choice"


class StreamingRecord(ParameterRecord):
    """Потоковая выдача одного запроса в форме инференса."""

    kind: Literal["streaming"] = "streaming"
    scope: Literal["request"] = "request"
    key: Literal["streaming"] = "streaming"


class StreamingRemovedRecord(ParameterRemovedRecord):
    """Потоковая выдача: отмена для одного запроса."""

    kind: Literal["streaming_removed"] = "streaming_removed"
    scope: Literal["request"] = "request"
    key: Literal["streaming"] = "streaming"


class AssemblyContextRecord(RecordBase):
    """
    AssemblyContextRecord указывает сборщику, какую часть существующей переписки
    приложить к заданию для LLM на компактизацию.
    Само задание находится в RequestMessageRecord, а параметры модели — в записях
    настроек с тем же operation_id.

    Например, runtime решил выполнить компактизацию. Вся переписка слишком большая,
    поэтому сначала нужно пересказать только участок с seq от 100 до 200.

    Runtime сохраняет одну операцию:

    operation_id = compact-1

    ├── operation_position=0: AssemblyContextRecord
    │   category = assembly
    │   scope = request
    │   payload = {
    │       "mode": "conversation",
    │       "from_seq": 100,
    │       "through_seq": 200
    │   }
    │
    ├── operation_position=1: RequestMessageRecord
    │   category = message
    │   scope = request
    │   payload.content = [
    │       {"kind": "text", "text": "Перескажи переписку. Сохрани решения и
    незавершённые задачи."}
    │   ]
    │
    └── operation_position=2: SamplingRecord
        category = setting
        scope = request
        payload.value = {"temperature": 0.2}

    Затем runtime вызывает сборку с operation_id="compact-1".

    Сборщик получает из этих записей три разных указания:
    - AssemblyContextRecord — взять переписку в диапазоне 100…200.
    - RequestMessageRecord — добавить к выбранной переписке текст задания на пересказ.
    - SamplingRecord — использовать temperature=0.2 вместо постоянного значения.
    В messages попадут выбранная переписка и задание «Перескажи переписку…». Поля mode,
    from_seq, through_seq в инференс не отправляются: они нужны SQL-сборщику для выбора
    строк.
    """

    class Payload(RecordBase.Payload):
        mode: Literal["conversation", "isolated"]
        from_seq: PositiveInt | None = None
        through_seq: NonNegativeInt | None = None

        @model_validator(mode="after")
        def valid_range(self) -> Self:
            if self.mode == "isolated":
                if self.from_seq is not None:
                    raise ValueError("An isolated operation has no conversation range")

                if self.through_seq is not None:
                    raise ValueError("An isolated operation has no conversation range")

                return self

            if self.from_seq is None:
                return self

            if self.through_seq is None:
                return self

            if self.through_seq < self.from_seq:
                raise ValueError("through_seq precedes from_seq")

            return self

    kind: Literal["assembly_context"] = "assembly_context"
    category: Literal["assembly"] = "assembly"
    scope: Literal["request"] = "request"
    key: Literal["context"] = "context"
    operation: Literal["set"] = "set"
    payload: Payload


class RequestMessageRecord(MessageRecord):
    """Служебное задание LLM, например пересказ или классификация разрешения.

    Входит в messages только при выборе своего operation_id; текст готовит
    производитель.
    """

    kind: Literal["request_message"] = "request_message"
    scope: Literal["request"] = "request"
    role: Literal["user"] = "user"


class UserMessageRecord(MessageRecord):
    """
    Ввод пользователя; operation_id общий с приложенными настройками, текст остаётся в
    диалоге.
    """

    kind: Literal["user_message"] = "user_message"
    scope: Literal["session"] = "session"
    role: Literal["user"] = "user"


class ResourceMessageRecord(MessageRecord):
    """Материалы, добавленные клиентом или агентом: выбранный фрагмент, файл, ресурс.

    related_message_id связывает материал с предыдущей записью, не ограничивая его одним
    запросом.
    """

    class Payload(MessageRecord.Payload):
        source: (
            NonEmptyStr  # Например client.editor, resource.prefetch или file.watcher.
        )
        related_message_id: NonEmptyStr | None = None

    kind: Literal["resource_message"] = "resource_message"
    role: Literal["user"] = "user"
    payload: Payload

    @model_validator(mode="after")
    def has_resource(self) -> Self:
        for part in self.payload.content:
            if isinstance(part, ResourcePart):
                return self

        raise ValueError("a resource message must include a resource")


class CompactMessageRecord(RecordBase):
    """
    Успешный пересказ и начало активного контекста; сохраняется атомарно с переносимыми
    данными.

    reason фиксирует причину сжатия; команда или неудачная попытка пересказа границу не
    создаёт.
    """

    class Payload(RecordBase.Payload):
        reason: NonEmptyStr
        content: NonEmptyTuple[TextPart]

    kind: Literal["compact_message"] = "compact_message"
    category: Literal["compact"] = "compact"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    payload: Payload


class AssistantMessageRecord(MessageRecord):
    """
    Ответ LLM; operation_id объединяет текст, определения calls, начальные статусы и
    usage.

    parent_operation_id адресует попытку. completion отличает полный ответ от
    частичного.
    record_id выделяется до первого чанка; сохранение полного сообщения выполняется
    после стрима.
    """

    class Payload(MessageRecord.Payload):
        completion: Literal["complete", "cancelled", "output_limit"] = "complete"

    kind: Literal["assistant_message"] = "assistant_message"
    parent_operation_id: NonEmptyStr = Field(...)
    scope: Literal["session"] = "session"
    role: Literal["assistant"] = "assistant"
    payload: Payload

    @model_validator(mode="after")
    def completed_tool_calls(self) -> Self:
        if self.payload.completion == "complete":
            return self

        for part in self.payload.content:
            if isinstance(part, ToolCallReferencePart):
                raise ValueError("an incomplete message cannot contain tool calls")

        return self


class RequestOutputRecord(RecordBase):
    """
    Ответ служебного LLM-вызова; принадлежит операции ответа, в основной диалог не
    включается.

    Успешный пересказ затем сохраняется как CompactMessageRecord; ответ классификатора
    обрабатывает политика разрешений. Получение ответа само по себе контекст не меняет.
    """

    kind: Literal["request_output"] = "request_output"
    parent_operation_id: NonEmptyStr = Field(...)
    category: Literal["operational"] = "operational"
    role: Literal["assistant"] = "assistant"
    payload: AssistantMessageRecord.Payload


class ToolCallRecord(RecordBase):
    """
    Определение вызова из AssistantMessageRecord; входит в ту же операцию ответа и
    связывает call_id с task_id.
    """

    class Payload(RecordBase.Payload):
        assistant_record_id: (
            NonEmptyStr  # Сообщение с ToolCallReferencePart для этого call_id.
        )
        name: NonEmptyStr
        arguments: JsonObject
        task_id: NonEmptyStr | None = (
            None  # Исполняющая задача; None для непосредственного ответа.
        )

    kind: Literal["tool_call"] = "tool_call"
    category: Literal["tool_call"] = "tool_call"
    role: Literal["assistant"] = "assistant"
    key: NonEmptyStr  # call_id из ответа LLM; совпадает со ссылкой в assistant.content.
    operation: Literal["set"] = "set"
    payload: Payload


class ToolResultRecord(RecordBase):
    """Состояние и содержимое ответа вызову key=call_id; сборщик выбирает LAST по seq.

    status относится к вызову; state.status — к наблюдаемой задаче.
    source_record_id закрепляет content/state конкретной записи, сохраняя собственный
    status.
    """

    class Payload(RecordBase.Payload):
        call_record_id: NonEmptyStr  # ToolCallRecord, которому отвечает эта запись.
        status: ExecutionStatus
        state: TaskSnapshot | None = (
            None  # Прочитанный снимок, если ответ содержит наблюдение задачи.
        )
        source_record_id: NonEmptyStr | None = (
            None  # Ранее сохранённый ToolResultRecord с собственным content/state.
        )
        content: tuple[ToolResultPart, ...] = ()

        @model_validator(mode="after")
        def single_source(self) -> Self:
            if self.source_record_id is None:
                return self

            if self.content:
                raise ValueError("a referenced result supplies the entire content")

            if self.state is not None:
                raise ValueError(
                    "a referenced result supplies the entire observed state"
                )

            return self

    kind: Literal["tool_result"] = "tool_result"
    category: Literal["tool_result"] = "tool_result"
    role: Literal["tool"] = "tool"
    key: NonEmptyStr  # call_id; последовательные записи обновляют ответ одному вызову.
    operation: Literal["set"] = "set"
    payload: Payload


class ElicitationRequestRecord(RecordBase):
    """
    Вопрос или внешнее действие; сохраняется до отправки клиенту, LLM не требуется.
    """

    class Payload(RecordBase.Payload):
        source: NonEmptyStr  # Обработчик агента или полное имя инструмента.
        message: NonEmptyStr
        mode: Literal["form", "url"]
        related_message_id: NonEmptyStr | None = None
        call_id: NonEmptyStr | None = None
        requested_schema: JsonObject | None = None
        url: AnyHttpUrl | None = (
            None  # Для credentials — внешний процесс, без секретов в URL.
        )

        @model_validator(mode="after")
        def valid_request(self) -> Self:
            if self.call_id is not None and self.related_message_id is None:
                raise ValueError("A tool-bound elicitation requires its source message")
            if self.mode == "form":
                if self.requested_schema is None or self.url is not None:
                    raise ValueError(
                        "Form elicitation requires a schema and forbids a URL"
                    )
            elif self.url is None or self.requested_schema is not None:
                raise ValueError(
                    "URL elicitation requires a URL and forbids a form schema"
                )
            return self

    kind: Literal["elicitation_request"] = "elicitation_request"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class ElicitationResponseRecord(RecordBase):
    """Ответ пользователя; accept для URL означает только согласие открыть ссылку."""

    class Payload(RecordBase.Payload):
        elicitation_id: NonEmptyStr  # record_id исходного ElicitationRequestRecord.
        action: Literal["accept", "decline", "cancel"]
        content: JsonObject | None = (
            None  # Проверенный ответ формы без секретов; для URL — None.
        )

        @model_validator(mode="after")
        def content_requires_accept(self) -> Self:
            if self.action != "accept" and self.content is not None:
                raise ValueError("Only an accepted form can carry content")
            return self

    kind: Literal["elicitation_response"] = "elicitation_response"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class ElicitationCompletedRecord(RecordBase):
    """Фактический итог внешнего URL-действия; credentials в запись не попадают."""

    class Payload(RecordBase.Payload):
        elicitation_id: NonEmptyStr  # record_id исходного URL-запроса.
        outcome: Literal["completed", "failed", "cancelled"]
        error: NonEmptyStr | None = None  # Код ошибки; без секретов.

        @model_validator(mode="after")
        def error_matches_outcome(self) -> Self:
            if (self.outcome == "failed") != (self.error is not None):
                raise ValueError("Only a failed external action must carry an error")
            return self

    kind: Literal["elicitation_completed"] = "elicitation_completed"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class AgentCommandOwner(DataModel):
    """Команду объявляет и исполняет агент; например /compact."""

    kind: Literal["agent"] = "agent"


class ClientCommandOwner(DataModel):
    """Команду объявляет клиент и исполняет его обработчик."""

    kind: Literal["client"] = "client"


class McpPromptCommandOwner(DataModel):
    """MCP-промпт; имя команды mcp__<server>__<prompt> определяет адрес prompts/get."""

    kind: Literal["mcp_prompt"] = "mcp_prompt"


class McpAppCommandOwner(DataModel):
    """Команда MCP App; объявивший её MCP задаёт инструмент и ресурс интерфейса."""

    kind: Literal["mcp_app"] = "mcp_app"
    tool_name: NonEmptyStr  # Полное имя mcp__<server>__<tool>.
    resource_uri: NonEmptyStr


SlashCommandOwner = Annotated[
    AgentCommandOwner | ClientCommandOwner | McpPromptCommandOwner | McpAppCommandOwner,
    Field(discriminator="kind"),
]


class CommandAddedRecord(RecordBase):
    """Объявляет доступную команду; LAST по key обновляет описание и владельца."""

    class Payload(RecordBase.Payload):
        description: str
        owner: SlashCommandOwner
        input_hint: str | None = None

    kind: Literal["command_added"] = "command_added"
    key: NonEmptyStr  # Публичное имя команды без / и пробелов.
    payload: Payload


class CommandRemovedRecord(RecordBase):
    """Убирает команду из доступного каталога; прежние вызовы остаются в истории."""

    kind: Literal["command_removed"] = "command_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr


class SlashCommandRecord(RecordBase):
    """
    Сохранённый вызов команды; владелец задаёт маршрут, text сохраняет исходный ввод.
    """

    class Payload(RecordBase.Payload):
        name: NonEmptyStr  # Имя без начального /.
        # Исходный ввод, например /compact сохрани решения по архитектуре.
        text: NonEmptyStr
        arguments: JsonObject  # Разобранные аргументы, {} если их нет.
        owner: SlashCommandOwner

    kind: Literal["slash_command"] = "slash_command"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class SlashCommandResultRecord(RecordBase):
    """Окончательный результат команды; command_id ссылается на её вызов."""

    class Payload(RecordBase.Payload):
        command_id: NonEmptyStr
        outcome: Literal["succeeded", "failed", "cancelled"]
        content: tuple[ContentPart, ...] = ()
        structured_content: (
            JsonObject | None
        )  # В том числе данные для интерфейса MCP App.
        error: NonEmptyStr | None

    kind: Literal["slash_command_result"] = "slash_command_result"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload

    @model_validator(mode="after")
    def error_matches_outcome(self) -> Self:
        if (self.payload.outcome == "failed") != (self.payload.error is not None):
            raise ValueError("Only a failed command must carry an error")
        return self


class PermissionRule(DataModel):
    """Правило сессии для одного инструмента, указанного полным именем."""

    tool: NonEmptyStr
    decision: Literal["allow", "deny", "ask"]


class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    DONT_ASK = "dont_ask"
    BYPASS = "bypass_permissions"
    AUTO = "auto"

    @property
    def label(self) -> str:
        match self:
            case self.DEFAULT:
                return "Ask before tools"
            case self.ACCEPT_EDITS:
                return "Accept edits"
            case self.PLAN:
                return "Plan"
            case self.DONT_ASK:
                return "Deny unapproved tools"
            case self.BYPASS:
                return "Bypass permissions"
            case self.AUTO:
                return "Automatic approval"


class PermissionStateRecord(RecordBase):
    """Полный снимок режима и правил разрешений текущей сессии; действует последний."""

    class Payload(RecordBase.Payload):
        mode: PermissionMode
        rules: tuple[PermissionRule, ...]

    kind: Literal["permission_state"] = "permission_state"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class PermissionOption(DataModel):
    """Выбор пользователя: разрешение или отказ для вызова либо всей сессии."""

    option_id: NonEmptyStr
    name: NonEmptyStr
    decision: Literal["allow", "deny"]
    scope: Literal["call", "session"]


class PermissionRequestRecord(RecordBase):
    """Сохранённый вопрос о разрешении выполнить конкретный tool call."""

    class Payload(RecordBase.Payload):
        related_message_id: NonEmptyStr  # AssistantMessageRecord с исходным вызовом.
        call_id: NonEmptyStr
        title: NonEmptyStr
        description: str | None = None
        options: NonEmptyTuple[PermissionOption]

        @model_validator(mode="after")
        def unique_options(self) -> Self:
            if len({item.option_id for item in self.options}) != len(self.options):
                raise ValueError("Permission option IDs must be unique")
            return self

    kind: Literal["permission_request"] = "permission_request"
    payload: Payload


class PermissionResponseRecord(RecordBase):
    """Ответ одного клиента; отмена не разрешает выполнение инструмента."""

    class Payload(RecordBase.Payload):
        permission_request_id: (
            NonEmptyStr  # record_id конкретного PermissionRequestRecord.
        )
        action: Literal["selected", "cancelled"]
        option_id: NonEmptyStr | None = None

        @model_validator(mode="after")
        def selected_option(self) -> Self:
            if (self.action == "selected") != (self.option_id is not None):
                raise ValueError("Only a selected response has an option_id")
            return self

    kind: Literal["permission_response"] = "permission_response"
    payload: Payload


class PermissionDecisionRecord(RecordBase):
    """Решение по одному call_id; само по себе не меняет правила сессии."""

    class Payload(RecordBase.Payload):
        call_id: NonEmptyStr
        decision: Literal["allow", "deny"]
        reason: str

    kind: Literal["permission_decision"] = "permission_decision"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class NoticeRecord(RecordBase):
    """
    Сообщение о работе агента для клиента; уровень и готовый текст сохраняются в
    истории.
    """

    class Payload(RecordBase.Payload):
        level: Literal["info", "warning", "error"]
        content: NonEmptyTuple[TextPart]

    kind: Literal["notice"] = "notice"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class LlmRequestAttemptRecord(RecordBase):
    """Одна завершённая попытка инференса: одна неизменяемая строка отдельной операции.

    Создаётся после ответа, ошибки или отмены; повтор имеет новую операцию и свой итог.
    Ответ ссылается на попытку через parent_operation_id; debug-поля не влияют на
    сборку.
    """

    class Payload(NoticeRecord.Payload):
        attempt: PositiveInt
        status: Literal["completed", "failed", "cancelled"]
        started_at: AwareDatetime
        method: NonEmptyStr
        url: AnyHttpUrl
        status_code: Annotated[int, Field(ge=100, le=599)] | None
        duration_ms: NonNegativeInt
        error: NonEmptyStr | None = None
        retry_after_ms: NonNegativeInt | None = None
        request_body: JsonObject | None = (
            None  # Готовое тело из сборщика, только для debug.
        )
        response_body: JsonObject | None = (
            None  # Полученный ответ, если сохранён для debug.
        )

        @model_validator(mode="after")
        def valid_outcome(self) -> Self:
            if self.status == "failed":
                if self.error is None:
                    raise ValueError("A failed attempt requires an error")

                return self

            if self.status == "cancelled":
                return self

            if self.error is not None:
                raise ValueError("A completed attempt cannot carry an error")

            if self.status_code is None:
                raise ValueError("A completed attempt requires an HTTP status")

            if not HTTPStatus.OK <= self.status_code < HTTPStatus.MULTIPLE_CHOICES:
                raise ValueError(
                    "A completed attempt requires a successful HTTP status"
                )

            return self

    kind: Literal["llm_request_attempt"] = "llm_request_attempt"
    category: Literal["operational"] = "operational"
    operation_position: Literal[0] = 0
    operation: Literal["set"] = "set"
    payload: Payload


class SubagentSessionRecord(RecordBase):
    """Происхождение дочерней сессии; записывается в неё перед первым запросом."""

    class Payload(RecordBase.Payload):
        parent_session_id: NonEmptyStr
        parent_task_id: NonEmptyStr
        context_through_seq: NonNegativeInt | None = (
            None  # Срез родительской истории; None — без наследования.
        )

    kind: Literal["subagent_session"] = "subagent_session"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class TokenUsage(DataModel):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    reasoning_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0


class TokenUsageRecord(RecordBase):
    """
    Расход токенов ответа; operation_id и parent_operation_id совпадают с операцией
    ответа.

    Сохраняется при наличии счётчиков, включая частичный ответ; в LLM-запрос не входит.
    """

    class Payload(RecordBase.Payload):
        usage: TokenUsage

    kind: Literal["token_usage"] = "token_usage"
    parent_operation_id: NonEmptyStr = Field(...)
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class TurnEndRecord(RecordBase):
    """Исход завершённого хода и его длительность."""

    class Payload(RecordBase.Payload):
        turn_id: NonEmptyStr
        outcome: Literal["completed", "cancelled", "failed", "limit"]
        duration_ms: NonNegativeInt
        error: NonEmptyStr | None = None

    kind: Literal["turn_end"] = "turn_end"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class SessionDeletedRecord(RecordBase):
    """
    Закрывает сессию для новых обращений; сохранённая история остаётся доступна
    администратору.
    """

    kind: Literal["session_deleted"] = "session_deleted"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"


class SessionForkedRecord(RecordBase):
    """Фиксирует источник независимой копии истории; создаётся в новой сессии."""

    class Payload(RecordBase.Payload):
        source_session_id: NonEmptyStr
        through_seq: PositiveInt

    kind: Literal["session_forked"] = "session_forked"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


# Все допустимые конкретные записи; выбор модели — только по kind.
HistoryRecord = Annotated[
    SystemPromptAddedRecord
    | SystemPromptRemovedRecord
    | AgentAddedRecord
    | AgentRemovedRecord
    | SkillAddedRecord
    | SkillRemovedRecord
    | SkillActivatedRecord
    | MemoryAddedRecord
    | MemoryRemovedRecord
    | EnvironmentRecord
    | DateRecord
    | ToolAddedRecord
    | ToolRemovedRecord
    | ToolSchemaRecord
    | ParameterOptionRecord
    | ParameterOptionRemovedRecord
    | ModelRecord
    | SamplingRecord
    | SamplingRemovedRecord
    | InferenceRecord
    | InferenceRemovedRecord
    | ProviderParametersRecord
    | ProviderParametersRemovedRecord
    | ResponseFormatRecord
    | ResponseFormatRemovedRecord
    | ToolChoiceRecord
    | ToolChoiceRemovedRecord
    | StreamingRecord
    | StreamingRemovedRecord
    | AssemblyContextRecord
    | RequestMessageRecord
    | RequestOutputRecord
    | UserMessageRecord
    | ResourceMessageRecord
    | CompactMessageRecord
    | AssistantMessageRecord
    | ToolCallRecord
    | ToolResultRecord
    | ElicitationRequestRecord
    | ElicitationResponseRecord
    | ElicitationCompletedRecord
    | SlashCommandRecord
    | CommandAddedRecord
    | CommandRemovedRecord
    | SlashCommandResultRecord
    | PermissionStateRecord
    | PermissionRequestRecord
    | PermissionResponseRecord
    | PermissionDecisionRecord
    | LlmRequestAttemptRecord
    | SubagentSessionRecord
    | TokenUsageRecord
    | NoticeRecord
    | TurnEndRecord
    | SessionDeletedRecord
    | SessionForkedRecord,
    Field(discriminator="kind"),
]


class History(DataModel):
    """
    Сохранённые строки одной сессии в порядке seq; допускается чтение среза истории.
    """

    format_version: Literal[2] = 2
    session_id: NonEmptyStr
    rows: tuple[HistoryRecord, ...]

    @model_validator(mode="after")
    def committed_rows(self) -> Self:
        previous_seq = 0
        record_ids: set[str] = set()
        positions: set[tuple[str, int]] = set()
        parents: dict[str, str | None] = {}
        for row in self.rows:
            if row.session_id != self.session_id:
                raise ValueError("History contains rows of another session")

            if row.seq is None:
                raise ValueError("History contains an uncommitted row")

            if row.seq <= previous_seq:
                raise ValueError("History rows must have strictly increasing seq")

            if row.record_id in record_ids:
                raise ValueError("record_id must be unique in the session")

            position = (row.operation_id, row.operation_position)
            if position in positions:
                raise ValueError(
                    "operation_position must be unique within an operation"
                )

            if row.operation_id in parents:
                if parents[row.operation_id] != row.parent_operation_id:
                    raise ValueError(
                        "All parts of an operation must have the same parent"
                    )

            previous_seq = row.seq
            record_ids.add(row.record_id)
            positions.add(position)
            parents[row.operation_id] = row.parent_operation_id

        return self


HISTORY_RECORD_ADAPTER = TypeAdapter(HistoryRecord)
# Неизвестный kind и неверная форма любой записи — ValidationError.


class HistoryWriteError(Exception):
    """Исключение операции записи с причиной и идентификаторами входных записей."""

    def __init__(
        self,
        *,
        session_id: str,
        record_ids: tuple[str, ...],
        code: str,
        message: str,
    ) -> None:
        self.session_id = session_id
        self.record_ids = record_ids
        self.code = code
        super().__init__(message)
