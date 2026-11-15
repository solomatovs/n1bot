"""Проприетарная история: одна типизированная модель на строку PostgreSQL records."""

from __future__ import annotations

from datetime import date as CalendarDate
from enum import StrEnum
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
    "input",  # Сохранённый ввод; в диалог LLM включается через admission.
    "admission",  # SUM ссылок на принятый ввод в порядке принятия.
    "request_message",  # SUM входов только выбранного request_id.
    "request_context",  # LAST правила выбора диалога для request_id.
    "message",  # SUM диалога от компактизации.
    "compact",  # Пересказ и начало активного контекста.
    "prompt",  # LAST add/remove по ключу системной инструкции.
    "context",  # LAST снимка контекста по ключу в активном диапазоне.
    "setting",  # LAST целой категории настроек; key=model/sampling/...
    "tool_catalog",  # LAST доступности инструмента по полному имени.
    "tool_schema",  # LAST схемы внутри непрерывной доступности имени.
    "request",  # Граница входа логического запроса.
    "operational",  # Служебные записи о работе агента; в LLM-запрос не входят.
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
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start and line_end must be set together")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end precedes line_start")
        if self.truncated and self.content is None:
            raise ValueError("Only loaded content can be truncated")
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


class ToolCallPart(DataModel):
    """Вызов в сообщении LLM; call_id связывает его с ToolResultRecord."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: NonEmptyStr
    name: NonEmptyStr
    arguments: JsonObject


class ToolReferencePart(DataModel):
    """Ссылка на найденный инструмент и использованную запись каталога."""

    kind: Literal["tool_reference"] = "tool_reference"
    name: NonEmptyStr
    catalog_revision: NonEmptyStr


UserPart = Annotated[
    TextPart | ImagePart | ResourcePart,
    Field(discriminator="kind"),
]
AssistantPart = Annotated[
    TextPart | ReasoningPart | ToolCallPart,
    Field(discriminator="kind"),
]
ToolResultPart = Annotated[
    TextPart | ImagePart | ResourcePart | ToolReferencePart,
    Field(discriminator="kind"),
]


class ToolCatalogItem(DataModel):
    """Имя и описание доступного инструмента без полной схемы."""

    description: str


class ToolDefinition(DataModel):
    """Полное определение инструмента с уникальным именем в каталоге агента."""

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
    """Описание доступного типа агента; конфигурация запуска фиксируется в дочерней истории."""

    name: NonEmptyStr
    description: str
    source_uri: NonEmptyStr | None = None


TaskExecutionMode = Literal["foreground", "background"]
TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TaskTerminalStatus = Literal["completed", "failed", "cancelled"]


class TaskSnapshot(DataModel):
    """Полное наблюдённое состояние задачи; output_uri адресует вывод, result хранит полученный итог."""

    execution_mode: (
        TaskExecutionMode  # foreground — родитель ждёт; background — продолжает работу.
    )
    description: NonEmptyStr
    status: TaskStatus
    execution_id: NonEmptyStr | None = None
    child_session_id: NonEmptyStr | None = (
        None  # Диалог сабагента, если вызов запускает его.
    )
    output_uri: NonEmptyStr | None = None
    result: JsonValue = None
    error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("Only a failed task has an error, and it requires one")
        if self.status in ("pending", "running") and self.result is not None:
            raise ValueError("An unfinished task cannot have a final result")
        return self


class EnvironmentState(DataModel):
    """Последние наблюдённые свойства одной среды; None означает неизвестное значение."""

    working_directory: NonEmptyStr | None  # cwd: один текущий каталог указанной среды.
    workspace_roots: tuple[
        NonEmptyStr, ...
    ]  # Корни открытых проектов; () — не сообщены.
    platform: NonEmptyStr | None
    os_version: NonEmptyStr | None
    shell: NonEmptyStr | None
    temporary_directory: NonEmptyStr | None


class RecordBase(DataModel):
    """Одно сообщение = одна строка records; session_id и seq назначает хранилище."""

    class Payload(DataModel):
        pass

    session_id: NonEmptyStr | None = None
    seq: PositiveInt | None = None
    record_id: NonEmptyStr  # Уникален среди всех записей одной сессии.

    kind: NonEmptyStr
    category: HistoryCategory = "operational"
    role: Literal["system", "user", "assistant", "tool"] | None = None

    key: NonEmptyStr | None = None
    operation: Literal["set", "remove"] = "set"
    request_id: NonEmptyStr | None = None
    # Предыдущая запись той же сессии, скопированная при компактизации.
    copied_from_id: NonEmptyStr | None = None

    created_at: AwareDatetime
    version: NonEmptyStr  # Версия агента, создавшего сообщение.
    payload: Payload

    @model_validator(mode="after")
    def address_is_complete(self) -> Self:
        if (self.session_id is None) != (self.seq is None):
            raise ValueError("session_id and seq must be assigned together")
        return self


class SystemPromptAddedRecord(RecordBase):
    """Инструкция указанного типа: постоянная либо только для request_id."""

    class Payload(RecordBase.Payload):
        source_uri: NonEmptyStr | None = (
            None  # Источник инструкции; LLM-сборщик не читает его.
        )
        content: NonEmptyTuple[TextPart]

    kind: Literal["system_prompt_added"] = "system_prompt_added"
    category: Literal["prompt"] = "prompt"
    role: Literal["system"] = "system"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr = Field(
        default=...
    )  # Свободный ключ, например agent.role или project.rules.
    payload: Payload


class SystemPromptRemovedRecord(RecordBase):
    """Удалить промпт постоянно либо исключить его только из request_id."""

    kind: Literal["system_prompt_removed"] = "system_prompt_removed"
    category: Literal["prompt"] = "prompt"
    role: Literal["system"] = "system"
    operation: Literal["remove"] = "remove"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr = Field(default=...)


class SkillAddedRecord(RecordBase):
    """Полный снимок одного доступного навыка и готовое объявление для модели."""

    class Payload(RecordBase.Payload):
        skill: SkillDefinition
        content: NonEmptyTuple[TextPart]

    kind: Literal["skill_added"] = "skill_added"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    request_id: None = None
    key: NonEmptyStr = Field(
        default=...
    )  # Устойчивая идентичность навыка, например skill:workspace:review.
    payload: Payload


class SkillRemovedRecord(RecordBase):
    """Снять доступность навыка; ранее активированные инструкции сохраняются."""

    kind: Literal["skill_removed"] = "skill_removed"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr = Field(default=...)


class SkillActivatedRecord(RecordBase):
    """Полученные инструкции навыка с уже подставленными аргументами вызова."""

    class Payload(RecordBase.Payload):
        name: NonEmptyStr
        source_uri: NonEmptyStr | None = None
        content: NonEmptyTuple[TextPart]

    kind: Literal["skill_activated"] = "skill_activated"
    category: Literal["prompt"] = "prompt"
    role: Literal["system"] = "system"
    operation: Literal["set"] = "set"
    key: NonEmptyStr = Field(
        default=...
    )  # Та же идентичность, что в каталоге, но категория prompt.
    payload: Payload


class MemoryAddedRecord(RecordBase):
    """Полный снимок выбранной памяти, действующий до замены или отмены."""

    class Payload(RecordBase.Payload):
        source_uri: NonEmptyStr | None = None
        content: NonEmptyTuple[TextPart]

    kind: Literal["memory_added"] = "memory_added"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    key: NonEmptyStr = Field(
        default=...
    )  # Устойчивая идентичность фрагмента памяти, например memory:workspace:project.
    payload: Payload


class MemoryRemovedRecord(RecordBase):
    """Прекратить включение выбранной памяти в запросы; исходные записи остаются."""

    kind: Literal["memory_removed"] = "memory_removed"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr = Field(default=...)


class AgentAddedRecord(RecordBase):
    """Полный снимок одного доступного типа агента и готовое объявление для модели."""

    class Payload(RecordBase.Payload):
        agent: AgentCatalogItem
        content: NonEmptyTuple[TextPart]

    kind: Literal["agent_added"] = "agent_added"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    request_id: None = None
    key: NonEmptyStr = Field(
        default=...
    )  # Например agent:explore; не идентификатор запущенной задачи.
    payload: Payload


class AgentRemovedRecord(RecordBase):
    """Снять тип агента из каталога; уже запущенные задачи продолжают работу."""

    kind: Literal["agent_removed"] = "agent_removed"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr = Field(default=...)


class EnvironmentRecord(RecordBase):
    """Полный снимок одной среды; state=None явно снимает её объявление."""

    class Payload(RecordBase.Payload):
        state: EnvironmentState | None
        content: tuple[TextPart, ...] = ()

    kind: Literal["environment"] = "environment"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr = Field(default=...)
    payload: Payload

    @model_validator(mode="after")
    def description_matches_state(self) -> Self:
        if (self.operation == "remove") != (self.payload.state is None):
            raise ValueError("Removed environment must have operation=remove")
        return self

    @model_validator(mode="after")
    def state_matches_content(self) -> Self:
        if (self.payload.state is None) != (len(self.payload.content) == 0):
            raise ValueError(
                "An active environment requires content; a removed one must have none"
            )
        return self


class DateRecord(RecordBase):
    """Наблюдённая календарная дата в выбранной зоне; отличается от времени записи."""

    class Payload(RecordBase.Payload):
        date: CalendarDate
        timezone: NonEmptyStr
        content: NonEmptyTuple[TextPart]

    kind: Literal["date"] = "date"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    key: NonEmptyStr = "calendar.current"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr | None = None
    payload: Payload


class ToolAddedRecord(RecordBase):
    """Инструмент доступен агенту по полному имени."""

    class Payload(RecordBase.Payload):
        tool: ToolCatalogItem

    kind: Literal["tool_added"] = "tool_added"
    category: Literal["tool_catalog"] = "tool_catalog"
    operation: Literal["set"] = "set"
    key: NonEmptyStr = Field(default=...)  # Полное имя инструмента.
    payload: Payload


class ToolRemovedRecord(RecordBase):
    """Инструмент стал недоступен агенту."""

    kind: Literal["tool_removed"] = "tool_removed"
    category: Literal["tool_catalog"] = "tool_catalog"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr = Field(default=...)


class ToolSchemaRecord(RecordBase):
    """Полученное определение инструмента со ссылкой на использованную запись каталога."""

    class Payload(RecordBase.Payload):
        catalog_revision: (
            NonEmptyStr  # record_id add, подтверждающего доступность инструмента.
        )
        tool: ToolDefinition

    kind: Literal["tool_schema"] = "tool_schema"
    category: Literal["tool_schema"] = "tool_schema"
    operation: Literal["set"] = "set"
    key: NonEmptyStr = Field(default=...)  # Полное имя инструмента.
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

            raise ValueError(f"Parameter {self.name!r} has no choice {value_id!r}")

        @model_validator(mode="after")
        def unique_choices(self) -> Self:
            if len({c.value_id for c in self.choices}) != len(self.choices):
                raise ValueError("Parameter choice IDs must be unique")
            return self

    kind: Literal["parameter_option"] = "parameter_option"
    key: NonEmptyStr = Field(default=...)
    payload: Payload


class ParameterOptionRemovedRecord(RecordBase):
    """Убирает возможность менять параметр; сохранённое значение настройки не меняет."""

    kind: Literal["parameter_option_removed"] = "parameter_option_removed"
    key: NonEmptyStr = Field(default=...)
    operation: Literal["remove"] = "remove"


class ParameterRecord(RecordBase):
    """Атомарный набор готовых полей запроса; ключ задаёт логическую категорию."""

    class Payload(RecordBase.Payload):
        value: Annotated[JsonObject, Field(min_length=1)]

    category: Literal["setting"] = "setting"
    operation: Literal["set"] = "set"
    key: NonEmptyStr = Field(default=...)
    payload: Payload

    @model_validator(mode="after")
    def parameters_only(self) -> Self:
        if self.payload.value.keys() & {"messages", "tools"}:
            raise ValueError(
                "messages and tools are assembled from their own history records"
            )
        return self


class ParameterRemovedRecord(RecordBase):
    """Отмена целого набора параметров; payload содержит только метаданные записи."""

    category: Literal["setting"] = "setting"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr = Field(default=...)


class ModelRecord(ParameterRecord):
    """Выбранный инференс и готовые поля выбора модели."""

    class Payload(ParameterRecord.Payload):
        provider: NonEmptyStr | None  # None, пока пользователь не выбрал подключение.
        profile_id: NonEmptyStr | None = None  # Пресет настроек модели, если выбран.

    kind: Literal["model"] = "model"
    key: Literal["model"] = "model"
    payload: Payload


class SamplingRecord(ParameterRecord):
    """Параметры выборки в форме выбранного инференса."""

    kind: Literal["sampling"] = "sampling"
    key: Literal["sampling"] = "sampling"


class SamplingRemovedRecord(ParameterRemovedRecord):
    """Параметры выборки: отмена постоянно либо для request_id."""

    kind: Literal["sampling_removed"] = "sampling_removed"
    key: Literal["sampling"] = "sampling"


class InferenceRecord(ParameterRecord):
    """Лимиты вывода и параметры reasoning в форме выбранного инференса."""

    kind: Literal["inference"] = "inference"
    key: Literal["inference"] = "inference"


class InferenceRemovedRecord(ParameterRemovedRecord):
    """Параметры инференса: отмена постоянно либо для request_id."""

    kind: Literal["inference_removed"] = "inference_removed"
    key: Literal["inference"] = "inference"


class ProviderParametersRecord(ParameterRecord):
    """Прочие поля запроса выбранного инференса."""

    kind: Literal["provider_parameters"] = "provider_parameters"
    key: Literal["provider_parameters"] = "provider_parameters"


class ProviderParametersRemovedRecord(ParameterRemovedRecord):
    """Параметры провайдера: отмена постоянно либо для request_id."""

    kind: Literal["provider_parameters_removed"] = "provider_parameters_removed"
    key: Literal["provider_parameters"] = "provider_parameters"


class ResponseFormatRecord(ParameterRecord):
    """Формат генерируемого ответа одного запроса."""

    kind: Literal["response_format"] = "response_format"
    key: Literal["response_format"] = "response_format"
    request_id: NonEmptyStr = Field(default=...)


class ResponseFormatRemovedRecord(ParameterRemovedRecord):
    """Формат ответа: отмена для одного запроса."""

    kind: Literal["response_format_removed"] = "response_format_removed"
    key: Literal["response_format"] = "response_format"
    request_id: NonEmptyStr = Field(default=...)


class ToolChoiceRecord(ParameterRecord):
    """Политика выбора tools для одного запроса в форме инференса."""

    kind: Literal["tool_choice"] = "tool_choice"
    key: Literal["tool_choice"] = "tool_choice"
    request_id: NonEmptyStr = Field(default=...)


class ToolChoiceRemovedRecord(ParameterRemovedRecord):
    """Выбор tools: отмена для одного запроса."""

    kind: Literal["tool_choice_removed"] = "tool_choice_removed"
    key: Literal["tool_choice"] = "tool_choice"
    request_id: NonEmptyStr = Field(default=...)


class StreamingRecord(ParameterRecord):
    """Потоковая выдача одного запроса в форме инференса."""

    kind: Literal["streaming"] = "streaming"
    key: Literal["streaming"] = "streaming"
    request_id: NonEmptyStr = Field(default=...)


class StreamingRemovedRecord(ParameterRemovedRecord):
    """Потоковая выдача: отмена для одного запроса."""

    kind: Literal["streaming_removed"] = "streaming_removed"
    key: Literal["streaming"] = "streaming"
    request_id: NonEmptyStr = Field(default=...)


class RequestContextRecord(RecordBase):
    """Выбирает входной диалог одного запроса; сохраняется до границы request_id.

    conversation: основной диалог и снимки контекста плюс входы этого запроса.
    isolated: только входы этого запроса и настройки; основной диалог и tools исключены.
    from_seq/through_seq ограничивают основной диалог, а не настройки и инструкции.
    """

    class Payload(RecordBase.Payload):
        mode: Literal["conversation", "isolated"]
        from_seq: PositiveInt | None = None
        through_seq: NonNegativeInt | None = None

        @model_validator(mode="after")
        def isolated_has_no_range(self) -> Self:
            if self.mode == "isolated" and (
                self.from_seq is not None or self.through_seq is not None
            ):
                raise ValueError("An isolated request has no conversation range")
            return self

    kind: Literal["request_context"] = "request_context"
    category: Literal["request_context"] = "request_context"
    request_id: NonEmptyStr = Field(default=...)
    payload: Payload


class RequestMessageRecord(RecordBase):
    """Готовый ввод только для request_id; не становится репликой основного диалога."""

    class Payload(RecordBase.Payload):
        content: NonEmptyTuple[UserPart]

    kind: Literal["request_message"] = "request_message"
    category: Literal["request_message"] = "request_message"
    role: Literal["user"] = "user"
    request_id: NonEmptyStr = Field(default=...)
    payload: Payload


class LlmRequestBoundaryRecord(RecordBase):
    """Позволяет восстановить тело LLM-запроса, найти ответ и узнать расход токенов.

    record_id — идентификатор записи LlmRequestBoundaryRecord, уникальный в сессии.
    request_id связанных записей равен LlmRequestBoundaryRecord.record_id:
    - разовые настройки задают параметры сборки;
    - LlmRequestAttemptRecord содержит попытки отправки, длительность и ошибки;
    - AssistantMessageRecord содержит полученный ответ;
    - TokenUsageRecord содержит расход токенов.

    seq фиксирует конец входной истории для восстановления тела запроса.
    Сборщик использует предшествующие сообщения, инструкции, tools и настройки
    от последнего CompactMessageRecord либо с начала чата.
    Добавленные позже сообщения не изменяют повторную сборку запроса.
    Агент сохраняет LlmRequestBoundaryRecord до отправки; created_at — время подготовки.
    Сведения о выполнении сохраняются в связанных LlmRequestAttemptRecord.
    """

    kind: Literal["llm_request_boundary"] = "llm_request_boundary"
    category: Literal["request"] = "request"
    operation: Literal["set"] = "set"


class UserMessageRecord(RecordBase):
    """Сохранённый ввод пользователя; MessageAcceptedRecord включает текст в контекст LLM."""

    class Payload(RecordBase.Payload):
        content: NonEmptyTuple[UserPart]

    kind: Literal["user_message"] = "user_message"
    category: Literal["input"] = "input"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    payload: Payload


class ResourceMessageRecord(RecordBase):
    """Материалы, добавленные клиентом или агентом: выбранный фрагмент, файл, ресурс.

    related_message_id связывает материал с предыдущей записью, не ограничивая его одним запросом.
    """

    class Payload(RecordBase.Payload):
        source: (
            NonEmptyStr  # Например client.editor, resource.prefetch или file.watcher.
        )
        related_message_id: NonEmptyStr | None = None
        content: NonEmptyTuple[
            Annotated[TextPart | ResourcePart, Field(discriminator="kind")]
        ]

    kind: Literal["resource_message"] = "resource_message"
    category: Literal["input"] = "input"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    payload: Payload

    @model_validator(mode="after")
    def has_resource(self) -> Self:
        if not any(isinstance(part, ResourcePart) for part in self.payload.content):
            raise ValueError("A resource message must include a resource")
        return self


class MessageAcceptedRecord(RecordBase):
    """Включает сохранённый ввод в диалог LLM на позиции собственного seq.

    source_record_id — предыдущая запись категории input в этой сессии.
    Текст и роль берутся из исходной записи; повторное принятие запрещено.
    Производитель добавляет запись после сохранения результатов текущих tools.
    """

    class Payload(RecordBase.Payload):
        source_record_id: NonEmptyStr

    kind: Literal["message_accepted"] = "message_accepted"
    category: Literal["admission"] = "admission"
    payload: Payload


class MessageRejectedRecord(MessageAcceptedRecord):
    """Закрывает ещё не принятый ввод при отмене; текст не поступает в LLM."""

    class Payload(MessageAcceptedRecord.Payload):
        reason: NonEmptyStr

    kind: Literal["message_rejected"] = "message_rejected"
    operation: Literal["remove"] = "remove"
    payload: Payload


class CompactMessageRecord(RecordBase):
    """Результат успешной компактизации и первое сообщение нового контекста.

    reason — причина сжатия, например manual или context_limit.
    """

    class Payload(RecordBase.Payload):
        reason: NonEmptyStr
        content: NonEmptyTuple[TextPart]

    kind: Literal["compact_message"] = "compact_message"
    category: Literal["compact"] = "compact"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    payload: Payload


class AssistantMessageRecord(RecordBase):
    """Итог одного сообщения LLM: текст, reasoning и/или вызовы tools в исходном порядке.

    request_id указывает источник ответа; сообщение остаётся в контексте следующих запросов.
    completion отличает полный ответ от текста, сохранённого при отмене или лимите.
    """

    class Payload(RecordBase.Payload):
        content: NonEmptyTuple[AssistantPart]
        completion: Literal["complete", "cancelled", "output_limit"] = "complete"

    kind: Literal["assistant_message"] = "assistant_message"
    category: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr = Field(default=...)
    payload: Payload

    @model_validator(mode="after")
    def completed_tool_calls(self) -> Self:
        if self.payload.completion != "complete" and any(
            isinstance(part, ToolCallPart) for part in self.payload.content
        ):
            raise ValueError("an incomplete message cannot contain tool calls")
        return self


class RequestOutputRecord(AssistantMessageRecord):
    """Ответ служебного LLM-вызова; связан с request_id, в основной диалог не включается.

    Успешный пересказ затем сохраняется как CompactMessageRecord; ответ классификатора
    обрабатывает политика разрешений. Получение ответа само по себе контекст не меняет.
    """

    kind: Literal["request_output"] = "request_output"
    category: Literal["operational"] = "operational"


class ToolResultRecord(RecordBase):
    """Один окончательный результат; отказ и отмена имеют is_error=True."""

    class Payload(RecordBase.Payload):
        call_id: NonEmptyStr
        content: tuple[ToolResultPart, ...] = ()
        is_error: bool

    kind: Literal["tool_result"] = "tool_result"
    category: Literal["message"] = "message"
    role: Literal["tool"] = "tool"
    operation: Literal["set"] = "set"
    payload: Payload


class ElicitationRequestRecord(RecordBase):
    """Вопрос или внешнее действие; сохраняется до отправки клиенту, LLM не требуется."""

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
        content: JsonObject | None = None  # Проверенный ответ формы; для URL — None.

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
    key: NonEmptyStr = Field(default=...)  # Публичное имя команды без / и пробелов.
    payload: Payload


class CommandRemovedRecord(RecordBase):
    """Убирает команду из доступного каталога; прежние вызовы остаются в истории."""

    kind: Literal["command_removed"] = "command_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr = Field(default=...)


class SlashCommandRecord(RecordBase):
    """Сохранённый вызов команды; владелец задаёт маршрут, text сохраняет исходный ввод."""

    class Payload(RecordBase.Payload):
        name: NonEmptyStr  # Имя без начального /.
        text: NonEmptyStr  # Исходный ввод, например /compact сохрани решения по архитектуре.
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
        content: tuple[UserPart, ...] = ()
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


class PermissionMode(StrEnum):
    """Политика разрешений инструментов, сохраняемая в истории сессии."""

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


class PermissionRule(DataModel):
    """Правило сессии для одного инструмента, указанного полным именем."""

    tool: NonEmptyStr
    decision: Literal["allow", "deny", "ask"]


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
        permission_request_id: NonEmptyStr
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
    """Общие поля уведомлений агента; в историю входят только конкретные наследники.

    payload.level — важность события; payload.content — подготовленное пояснение.
    """

    class Payload(RecordBase.Payload):
        level: Literal["info", "warning", "error"]
        content: NonEmptyTuple[TextPart]

    payload: Payload


class TextNoticeRecord(NoticeRecord):
    """Самостоятельное уведомление без дополнительных структурированных данных."""

    kind: Literal["notice"] = "notice"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"


class LlmRequestAttemptRecord(NoticeRecord):
    """Одна завершённая попытка запроса к LLM, включая неуспешные попытки и повторы."""

    class Payload(NoticeRecord.Payload):
        attempt: PositiveInt
        method: NonEmptyStr
        url: NonEmptyStr
        status_code: Annotated[int, Field(ge=100, le=599)] | None
        duration_ms: NonNegativeInt
        error: NonEmptyStr | None = None
        retry_after_ms: NonNegativeInt | None = None

    kind: Literal["llm_request_attempt"] = "llm_request_attempt"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr = Field(default=...)
    payload: Payload

    @model_validator(mode="after")
    def attempt_has_outcome(self) -> Self:
        if self.payload.status_code is None and self.payload.error is None:
            raise ValueError("an attempt must have a response or an error")
        return self


class TaskStateRecord(NoticeRecord):
    """Событие исполнителя: состояние и готовый текст сохраняются сразу, в любой момент хода."""

    class Payload(NoticeRecord.Payload):
        task_id: NonEmptyStr  # Например task:tests-1; один идентификатор на попытку выполнения.
        state: TaskSnapshot
        related_message_id: (
            NonEmptyStr  # Сохранённое сообщение, содержащее исходный вызов.
        )
        call_id: NonEmptyStr  # Конкретный ToolCallPart внутри этого сообщения.

    kind: Literal["task_state"] = "task_state"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload


class TaskContextRecord(RecordBase):
    """Снимок фоновой задачи или её необработанного итога, подготовленный для LLM."""

    class Payload(RecordBase.Payload):
        related_message_id: NonEmptyStr  # Исходная TaskStateRecord в этой сессии.
        state: TaskSnapshot
        content: NonEmptyTuple[TextPart]

    kind: Literal["task_context"] = "task_context"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    request_id: None = None
    key: NonEmptyStr = Field(default=...)  # task_id из TaskStateRecord.
    payload: Payload

    @model_validator(mode="after")
    def background_task(self) -> Self:
        if self.payload.state.execution_mode != "background":
            raise ValueError("Only background tasks have a separate context snapshot")
        return self


class TaskNotificationRecord(NoticeRecord):
    """Итог фоновой задачи в диалоге; автоматическое событие, не ответ пользователя."""

    class Payload(NoticeRecord.Payload):
        task_id: NonEmptyStr
        status: TaskTerminalStatus
        related_message_id: NonEmptyStr  # Терминальная TaskStateRecord в этой сессии.

    kind: Literal["task_notification"] = "task_notification"
    category: Literal["message"] = "message"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    payload: Payload


class TaskAcknowledgedRecord(RecordBase):
    """Итог обработан: снять задачу с учёта в активном контексте, сохранив её историю."""

    class Payload(RecordBase.Payload):
        notification_id: NonEmptyStr
        related_message_id: (
            NonEmptyStr  # Ответ агента или явная команда, обработавшая итог.
        )

    kind: Literal["task_acknowledged"] = "task_acknowledged"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr = Field(default=...)  # task_id принятого результата.
    payload: Payload


class SubagentSessionRecord(RecordBase):
    """Происхождение дочерней сессии; записывается в неё перед первым запросом."""

    class Payload(RecordBase.Payload):
        parent_session_id: NonEmptyStr
        parent_task_id: NonEmptyStr
        parent_request_id: NonEmptyStr | None = (
            None  # Граница наследуемого контекста; None — без наследования.
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
    """Итоговые счётчики токенов одного запроса."""

    class Payload(RecordBase.Payload):
        usage: TokenUsage

    kind: Literal["token_usage"] = "token_usage"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr = Field(default=...)
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
    """Закрывает сессию для новых обращений; сохранённая история остаётся доступна администратору."""

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
    | RequestContextRecord
    | RequestMessageRecord
    | RequestOutputRecord
    | LlmRequestBoundaryRecord
    | UserMessageRecord
    | MessageAcceptedRecord
    | MessageRejectedRecord
    | ResourceMessageRecord
    | CompactMessageRecord
    | AssistantMessageRecord
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
    | TaskStateRecord
    | TaskContextRecord
    | TaskNotificationRecord
    | TaskAcknowledgedRecord
    | SubagentSessionRecord
    | TokenUsageRecord
    | TextNoticeRecord
    | TurnEndRecord
    | SessionDeletedRecord
    | SessionForkedRecord,
    Field(discriminator="kind"),
]


class History(DataModel):
    """Подтверждённая история; rows содержит строки records без изменения их формы."""

    format_version: Literal[2] = 2
    session_id: NonEmptyStr
    rows: tuple[HistoryRecord, ...]

    @model_validator(mode="after")
    def committed_rows(self) -> Self:
        if any(
            row.session_id != self.session_id or row.seq is None for row in self.rows
        ):
            raise ValueError("History contains only committed rows of this session")
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
