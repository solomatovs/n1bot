# Модели единого журнала и публичные контракты сборки

Журнал один: сообщения, команды, настройки, попытки запросов к LLM, токены и события агента.
Любые новые записи сначала попадают в ограниченную очередь `HistoryWriteQueue`.
Она передаёт сообщения в персистентное хранилище строго в порядке поступления.
Хранилище атомарно назначает `seq` и сохраняет сообщения; только тогда они добавлены
в историю. Неудавшиеся попытки записи повторяются.
Harness читает журнал и показывает только записи с `harness_view="include"`.
Ожидающие сообщения из снимка очереди отображаются по тому же признаку.

После завершения каждого отдельного сообщения цикл агента читает из хранилища
порцию после `processed_seq`. Успешная обработка продвигает этот курсор;
конца хода ждать не нужно. Для сборки загружается история до конца прочитанной
порции, включая предшествующие ей записи.

Единственный источник содержимого запроса — персистентное хранилище истории. До первого
обращения к модели в него записываются модель, параметры запроса, действующие
системные промпты, каталоги инструментов и схемы, выбранные для этого запроса.
Сборщик провайдера не дополняет запрос настройками процесса или значениями по умолчанию.

Ниже — целевые определения моделей и публичных методов. Модели содержат
конкретные поля; `NotImplementedError` обозначает тело алгоритма,
которое нужно реализовать.

## 1 Общие значения

Исходные данные типизированы отдельно от готового представления для LLM.

```python
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal, Protocol, Self, TypeAlias, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
JsonObject: TypeAlias = dict[str, JsonValue]
T = TypeVar("T")
NonEmptyTuple: TypeAlias = Annotated[tuple[T, ...], Field(min_length=1)]


class DataModel(BaseModel):
    """Данные без инфраструктуры; вложенный JSON сборщик не изменяет."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


class LlmPayload(DataModel):
    """Готовый текст содержимого и отдельно сохранённые структурные поля запроса."""

    protocol: NonEmptyStr
    text: str | None = Field(default=None, strict=True)  # Содержимое «Привет»; у сообщения обязателен str.
    data: JsonValue  # Роль/нативные блоки, схема, параметры или null для дельты без тела.
    key: NonEmptyStr | None = None  # Ключ промпта или полное имя инструмента для LAST/SUM.
    catalog_revision: NonEmptyStr | None = None  # Для выбранной схемы инструмента.


class TextPart(DataModel):
    """Текст сохраняется буквально, включая пустую строку."""

    kind: Literal["text"] = "text"
    text: str


class ImagePart(DataModel):
    """Изображение уже загружено; в JSON байты представлены base64."""

    kind: Literal["image"] = "image"
    media_type: NonEmptyStr
    data: bytes


class ResourceLinkPart(DataModel):
    """Ссылка на ресурс без его загрузки при сборке."""

    kind: Literal["resource_link"] = "resource_link"
    name: NonEmptyStr
    uri: NonEmptyStr
    media_type: NonEmptyStr | None


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
    """Законченный вызов с разобранными JSON-аргументами."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: NonEmptyStr
    name: NonEmptyStr
    arguments: JsonObject


class ToolReferencePart(DataModel):
    """Ссылка на выбранный tool определённой ревизии; сама схему не загружает."""

    kind: Literal["tool_reference"] = "tool_reference"
    name: NonEmptyStr
    catalog_revision: NonEmptyStr


UserPart = Annotated[
    TextPart | ImagePart | ResourceLinkPart,
    Field(discriminator="kind"),
]
AssistantPart = Annotated[
    TextPart | ReasoningPart | ToolCallPart,
    Field(discriminator="kind"),
]
ToolResultPart = Annotated[
    TextPart | ImagePart | ResourceLinkPart | ToolReferencePart,
    Field(discriminator="kind"),
]


class ToolCatalogItem(DataModel):
    """Доступный для поиска инструмент; запись каталога не включает схему в запрос."""

    name: NonEmptyStr
    description: str


class ToolDefinition(DataModel):
    """Полное определение; name — уникальный ключ инструмента в запросе."""

    name: NonEmptyStr
    description: str
    parameters: JsonObject
    deferred: bool


class ModelRef(DataModel):
    """Адрес модели без URL, ключей доступа и объекта клиента."""

    provider: NonEmptyStr
    model: NonEmptyStr


class TextResponseFormat(DataModel):
    kind: Literal["text"] = "text"


class JsonSchemaResponseFormat(DataModel):
    kind: Literal["json_schema"] = "json_schema"
    name: NonEmptyStr
    json_schema: JsonObject
    strict: bool


ResponseFormat = Annotated[
    TextResponseFormat | JsonSchemaResponseFormat,
    Field(discriminator="kind"),
]


class AutoToolChoice(DataModel):
    kind: Literal["auto"] = "auto"


class NoToolChoice(DataModel):
    kind: Literal["none"] = "none"


class RequiredToolChoice(DataModel):
    kind: Literal["required"] = "required"


class NamedToolChoice(DataModel):
    kind: Literal["named"] = "named"
    name: NonEmptyStr


ToolChoice = Annotated[
    AutoToolChoice | NoToolChoice | RequiredToolChoice | NamedToolChoice,
    Field(discriminator="kind"),
]


```

## 2 Проприетарная история агента

Все конкретные записи наследуют один конверт и различаются только по `kind`.
`LlmRecord`, `CommandRecord` и `ServiceRecord` — объединения типов, а не разные журналы.
`prompt_type` — свободный смысловой ключ промпта; класс записи задаёт `kind`.
Все инструменты используют единый список и ключ — полное имя. Для MCP имя имеет
формат `mcp__<server>__<tool>`: принадлежность серверу уже содержится в нём.

Каждая модель записи явно задаёт две независимые политики отбора; в `RecordBase` у них нет
значений по умолчанию. В конкретных моделях указана исходная политика, а в JSON
сохраняются фактические значения обоих полей, включая значения по умолчанию.

Только `llm_selection` определяет участие записи в сборке: `exclude` исключает её,
`from_compact` выбирает после последнего `context_start`, `from_start` — с первого
сообщения чата. Без границы `from_compact` тоже начинает с первого сообщения.
После отбора применяются `LAST`/`SUM` соответствующей категории.

Только `harness_view` определяет отображение: `include` показывает запись,
`exclude` скрывает её. Harness не ограничивает историю границей компактизации.
Значения можно явно задать при создании записи; `kind` задаёт форму данных
и правило сборки/отображения, но не заменяет проверку этих полей.
Оба признака не влияют на сохранение записи, `processed_seq` и маршрутизацию команд.

У каждой записи два поля контракта LLM: `llm_selection` задаёт отбор,
`llm_payload` хранит подготовленное содержимое. `harness_view` относится только к UI.
`content`, `tool`, `model`, `parameters` и остальные предметные поля — исходные
структурированные данные. Даже при отличии исходных данных от подготовленного
представления сборщик использует только `llm_payload`.

`llm_payload.text: str` — готовый текст содержимого сообщения: например `Привет`.
Он создаётся один раз до очереди и сохраняется в истории вместе с исходными данными.
При каждом запросе сборщик заново создаёт сообщение провайдера и берёт его текст
буквально из этого поля. Он не рендерит исходные структуры и не сериализует их в текст.
Смена шаблонов, рендерера или настроек процесса не меняет текст старой записи.

`llm_payload.data` содержит остальные подготовленные поля: роль, идентификаторы,
нативные блоки изображений, reasoning, tool calls; для соответствующих категорий —
схемы и параметры запроса. Текст содержимого не дублируется в `data`.
Нативные строковые значения внутри структур, например arguments или подпись,
тоже сохраняются готовыми; сборщик не восстанавливает их из исходных данных.
`protocol`, `key` и `catalog_revision` нужны для сборки и сами в запрос не отправляются.

Для каждого сообщения, участвующего в SUM, `text` обязателен, включая пустую строку
для сообщения без видимого текста. У записей параметров, схем и управляющих дельт
`text=None`: они изменяют соответствующие категории запроса и не создают сообщения.
Системный промпт также содержит сохранённый текст. Сборщик может выбрать или исключить
запись целиком, но не изменять её текст, в том числе после удаления инструмента.

Инвариант: текст выбранной записи в каждом запросе имеет те же UTF-8-байты,
что и сохранённый `text`. Запрещены strip, замена переносов, нормализация Unicode,
подстановка переменных, обёртки и повторный рендеринг. JSON-кодирование транспортного
конверта может экранировать символы; значение текста после декодирования остаётся тем же.

`llm_selection="exclude"` допускает `llm_payload=None`; наличие payload само по себе
не включает запись в запрос. Выбранная запись требует подготовленный payload.
Исходные поля никогда не служат резервным источником при его отсутствии.

```python
LlmSelection = Literal["exclude", "from_compact", "from_start"]
HarnessView = Literal["exclude", "include"]


class RecordBase(DataModel):
    """Сообщение до и после очереди; id и время создания при записи не меняются."""

    id: NonEmptyStr
    created_at: AwareDatetime
    llm_selection: LlmSelection
    harness_view: HarnessView
    llm_payload: LlmPayload | None = None

    @model_validator(mode="after")
    def selected_record_has_payload(self) -> Self:
        if self.llm_selection != "exclude" and self.llm_payload is None:
            raise ValueError("Selected record requires a prepared llm_payload")
        data_only = {
            "model", "sampling", "inference", "provider_parameters",
            "response_format", "tool_choice", "streaming",
            "tool_added", "tool_removed", "tool_schema", "system_prompt_removed",
        }
        if self.llm_selection != "exclude" and self.kind not in data_only:
            if self.llm_payload.text is None:
                raise ValueError("Selected message requires stored text, including an empty string")
        return self


class SystemPromptAddedRecord(RecordBase):
    """Полный промпт одного типа; повторное добавление заменяет активный."""

    kind: Literal["system_prompt_added"] = "system_prompt_added"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    prompt_type: NonEmptyStr  # Свободный ключ, например agent.role или project.rules.
    content: NonEmptyTuple[TextPart]


class SystemPromptRemovedRecord(RecordBase):
    """Удалить активный промпт указанного типа."""

    kind: Literal["system_prompt_removed"] = "system_prompt_removed"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    prompt_type: NonEmptyStr


class ToolAddedRecord(RecordBase):
    """Доступность инструмента по полному имени; повторный add сохраняет выбранную схему."""

    kind: Literal["tool_added"] = "tool_added"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    tool: ToolCatalogItem


class ToolRemovedRecord(RecordBase):
    """Удалить инструмент по полному имени и отменить его выбранную схему."""

    kind: Literal["tool_removed"] = "tool_removed"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    tool_name: NonEmptyStr


class ToolSchemaRecord(RecordBase):
    """LAST определения по имени, пока инструмент непрерывно доступен."""

    kind: Literal["tool_schema"] = "tool_schema"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    catalog_revision: NonEmptyStr  # id ToolAddedRecord этого инструмента, использованного при поиске.
    tool: ToolDefinition


class ModelRecord(RecordBase):
    """Выбор провайдера и модели; остальные категории не меняет."""

    kind: Literal["model"] = "model"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    model: ModelRef


class SamplingRecord(RecordBase):
    """Параметры выборки: temperature, top_p, top_k, seed и подобные."""

    kind: Literal["sampling"] = "sampling"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    parameters: JsonObject


class InferenceRecord(RecordBase):
    """Параметры инференса: лимиты вывода, reasoning и другие настройки выполнения."""

    kind: Literal["inference"] = "inference"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    parameters: JsonObject


class ProviderParametersRecord(RecordBase):
    """Специфичные поля провайдера, не записанные в других категориях."""

    kind: Literal["provider_parameters"] = "provider_parameters"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    parameters: JsonObject


class ResponseFormatRecord(RecordBase):
    """Формат ответа: текст/JSON Schema; None явно снимает настройку."""

    kind: Literal["response_format"] = "response_format"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    format: ResponseFormat | None


class ToolChoiceRecord(RecordBase):
    """Режим выбора инструмента; None явно снимает настройку."""

    kind: Literal["tool_choice"] = "tool_choice"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    choice: ToolChoice | None


class StreamingRecord(RecordBase):
    """Потоковая выдача; None означает не передавать поле stream провайдеру."""

    kind: Literal["streaming"] = "streaming"
    llm_selection: LlmSelection = "from_start"
    harness_view: HarnessView = "exclude"
    enabled: bool | None


ParameterRecord = (
    ModelRecord | SamplingRecord | InferenceRecord | ProviderParametersRecord
    | ResponseFormatRecord | ToolChoiceRecord | StreamingRecord
)


class UserMessageRecord(RecordBase):
    """Ввод пользователя; до подтверждения сохранения ещё не входит в History."""

    kind: Literal["user_message"] = "user_message"
    llm_selection: LlmSelection = "from_compact"
    harness_view: HarnessView = "include"
    content: NonEmptyTuple[UserPart]


class CompactMessageRecord(RecordBase):
    """Пересказ предыдущего контекста, подготовленный при успешной компактизации.

    Начинает новый контекст после ContextStartRecord; роль заранее записана в llm_payload.
    """

    kind: Literal["compact_message"] = "compact_message"
    llm_selection: LlmSelection = "from_compact"
    harness_view: HarnessView = "include"
    content: NonEmptyTuple[TextPart]


class AssistantMessageRecord(RecordBase):
    """Одно полное сообщение LLM: текст, reasoning и/или вызовы tools в исходном порядке.

    request_id связывает сообщение с попытками запроса и расходом токенов.
    """

    kind: Literal["assistant_message"] = "assistant_message"
    llm_selection: LlmSelection = "from_compact"
    harness_view: HarnessView = "include"
    request_id: NonEmptyStr
    content: NonEmptyTuple[AssistantPart]


class ToolResultRecord(RecordBase):
    """Один окончательный результат; отказ и отмена имеют is_error=True."""

    kind: Literal["tool_result"] = "tool_result"
    llm_selection: LlmSelection = "from_compact"
    harness_view: HarnessView = "include"
    call_id: NonEmptyStr
    content: tuple[ToolResultPart, ...]
    is_error: bool


class ContextStartRecord(RecordBase):
    """Граница отбора для записей с llm_selection=from_compact; выбирается последний маркер.

    Записи с llm_selection=from_start этой границей не ограничиваются.
    """

    kind: Literal["context_start"] = "context_start"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    reason: Literal["initial", "compaction", "branch", "summary"]


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


class SlashCommandRecord(RecordBase):
    """Сохранённый вызов команды; владелец задаёт маршрут, text сохраняет ввод для чата."""

    kind: Literal["slash_command"] = "slash_command"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    name: NonEmptyStr  # Имя без начального /.
    text: NonEmptyStr  # Исходный ввод, например /compact сохрани решения по архитектуре.
    arguments: JsonObject  # Разобранные аргументы, {} если их нет.
    owner: SlashCommandOwner


class SlashCommandResultRecord(RecordBase):
    """Окончательный результат команды для чата; command_id ссылается на её вызов."""

    kind: Literal["slash_command_result"] = "slash_command_result"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    command_id: NonEmptyStr
    outcome: Literal["succeeded", "failed", "cancelled"]
    content: tuple[UserPart, ...]
    structured_content: JsonObject | None  # В том числе данные для интерфейса MCP App.
    error: NonEmptyStr | None

    @model_validator(mode="after")
    def error_matches_outcome(self) -> Self:
        if (self.outcome == "failed") != (self.error is not None):
            raise ValueError("Only a failed command must carry an error")
        return self


CommandRecord = SlashCommandRecord | SlashCommandResultRecord


class QueueStateRecord(RecordBase):
    """Состояние записи истории для чата и диагностики."""

    kind: Literal["queue_state"] = "queue_state"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    pending_batch_ids: tuple[NonEmptyStr, ...]
    pending_messages: NonNegativeInt
    in_flight_batch_id: NonEmptyStr | None
    last_error: NonEmptyStr | None


class PermissionRule(DataModel):
    tool: NonEmptyStr
    decision: Literal["allow", "deny", "ask"]


class PermissionStateRecord(RecordBase):
    """Режим и правила исполнения; сборщик LLM их не применяет."""

    kind: Literal["permission_state"] = "permission_state"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    mode: Literal[
        "default", "accept_edits", "plan",
        "dont_ask", "bypass_permissions", "auto",
    ]
    rules: tuple[PermissionRule, ...]


class PermissionDecisionRecord(RecordBase):
    """Решение о разрешении вызова; сам результат хранится отдельно в журнале."""

    kind: Literal["permission_decision"] = "permission_decision"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    call_id: NonEmptyStr
    decision: Literal["allow", "deny"]
    reason: str


class LlmRequestAttemptRecord(RecordBase):
    """Одна завершённая попытка запроса к LLM, включая неуспешные попытки и повторы."""

    kind: Literal["llm_request_attempt"] = "llm_request_attempt"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    request_id: NonEmptyStr
    attempt: PositiveInt
    method: NonEmptyStr
    url: NonEmptyStr
    status_code: Annotated[int, Field(ge=100, le=599)] | None
    duration_ms: NonNegativeInt
    error: NonEmptyStr | None = None
    retry_after_ms: NonNegativeInt | None = None

    @model_validator(mode="after")
    def attempt_has_outcome(self) -> Self:
        if self.status_code is None and self.error is None:
            raise ValueError("an attempt must have a response or an error")
        return self


class TokenUsage(DataModel):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    reasoning_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0


class TokenUsageRecord(RecordBase):
    """Итоговые счётчики одного запроса; не являются содержимым промпта."""

    kind: Literal["token_usage"] = "token_usage"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    request_id: NonEmptyStr
    usage: TokenUsage


class NoticeRecord(RecordBase):
    """Уведомление человеку для отображения в чате."""

    kind: Literal["notice"] = "notice"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    level: Literal["info", "warning", "error"]
    content: NonEmptyTuple[TextPart]


class AssistantStreamRecord(RecordBase):
    """Фрагмент для чата; message_id — id будущего AssistantMessageRecord."""

    kind: Literal["assistant_stream"] = "assistant_stream"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    request_id: NonEmptyStr
    message_id: NonEmptyStr
    index: NonNegativeInt
    text_delta: str
    reasoning_delta: str


class TurnEndRecord(RecordBase):
    """Результат хода для чата и диагностики."""

    kind: Literal["turn_end"] = "turn_end"
    llm_selection: LlmSelection = "exclude"
    harness_view: HarnessView = "include"
    turn_id: NonEmptyStr
    outcome: Literal["completed", "cancelled", "failed", "limit"]
    duration_ms: NonNegativeInt
    error: NonEmptyStr | None = None


LlmRecord = (
    SystemPromptAddedRecord | SystemPromptRemovedRecord
    | ToolAddedRecord | ToolRemovedRecord
    | ToolSchemaRecord
    | ParameterRecord
    | UserMessageRecord | CompactMessageRecord
    | AssistantMessageRecord | ToolResultRecord
)

ServiceRecord = (
    QueueStateRecord | PermissionStateRecord | PermissionDecisionRecord
    | LlmRequestAttemptRecord | TokenUsageRecord | NoticeRecord
    | AssistantStreamRecord | TurnEndRecord
)

HistoryRecord = Annotated[
    LlmRecord | ContextStartRecord | CommandRecord | ServiceRecord,
    Field(discriminator="kind"),
]


class HistoryEntry(DataModel):
    """Сохранённая запись; seq назначает хранилище в транзакции добавления."""

    seq: PositiveInt
    record: HistoryRecord


class History(DataModel):
    """Подтверждённый хранилищем журнал сессии, включая записи, не нужные LLM."""

    format_version: Literal[1] = 1
    session_id: NonEmptyStr
    records: tuple[HistoryEntry, ...]


class HistoryPortion(DataModel):
    """Необработанная порция из одного снимка хранилища, строго после after_seq."""

    session_id: NonEmptyStr
    after_seq: NonNegativeInt
    records: tuple[HistoryEntry, ...]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if any(entry.seq != self.after_seq + index + 1 for index, entry in enumerate(self.records)):
            raise ValueError("Portion must be contiguous and start after after_seq")
        return self

    @property
    def through_seq(self) -> int:
        return self.records[-1].seq if self.records else self.after_seq


HISTORY_RECORD_ADAPTER = TypeAdapter(HistoryRecord)
# Неизвестный kind и неверная форма любой записи — ValidationError.
```

### Сообщения, ожидающие сохранения

```python
class QueuedBatch(DataModel):
    """Неделимая пачка типизированных сообщений; в History её ещё нет."""

    id: NonEmptyStr
    enqueued_at: AwareDatetime
    records: NonEmptyTuple[HistoryRecord]


class HistoryWritePolicy(DataModel):
    """Настройки буфера записи; не являются параметрами LLM-запроса."""

    max_pending_messages: PositiveInt
    retry_delays_ms: NonEmptyTuple[PositiveInt]

    def retry_delay_ms(self, failed_attempts: PositiveInt) -> int:
        """После исчерпания списка задержек используется последняя."""
        if failed_attempts < 1:
            raise ValueError("failed_attempts must be positive")
        return self.retry_delays_ms[min(failed_attempts, len(self.retry_delays_ms)) - 1]


class PendingBatch(QueuedBatch):
    """Пачка в очереди без seq; повтор использует те же id, время и содержимое."""

    attempts: NonNegativeInt
    last_error: NonEmptyStr | None


class QueueSnapshot(DataModel):
    """Снимок единственного буфера; pending включает и записываемую сейчас пачку."""

    pending: tuple[PendingBatch, ...]
    in_flight_batch_id: NonEmptyStr | None
    durable_seq: NonNegativeInt
    max_pending_messages: PositiveInt

    @property
    def pending_messages(self) -> int:
        return sum(len(batch.records) for batch in self.pending)
```

## 3 Сборка подготовленного payload

`ProviderRequestBuilder` — чистый сборщик конкретного протокола провайдера.
Его единственный вход — сохранённая `History`, выход — готовое JSON-тело запроса.
Он читает `llm_selection` и `llm_payload`; исходные поля сообщения используются
производителями и harness, но не восстанавливают значения исходящего запроса.
Реализация выбирается по протоколу последнего выбранного `ModelRecord.llm_payload`.
Неподдерживаемый протокол — ошибка; запасной модели или протокола у сборщика нет.

```python
class BuildIssue(DataModel):
    code: NonEmptyStr
    message: NonEmptyStr
    record_id: NonEmptyStr | None = None
    seq: PositiveInt | None = None


class RequestBuildError(ValueError):
    def __init__(self, issue: BuildIssue) -> None:
        self.issue = issue
        super().__init__(issue.message)


def select_llm_entries(history: History) -> tuple[HistoryEntry, ...]:
    """Проверить порядок и выбрать записи только по llm_selection."""
    boundary_seq = 0
    seen: set[str] = set()
    for index, entry in enumerate(history.records):
        if entry.seq != index + 1 or entry.record.id in seen:
            raise RequestBuildError(BuildIssue(
                code="invalid_history", message="History seq/id are inconsistent",
                record_id=entry.record.id, seq=entry.seq,
            ))
        seen.add(entry.record.id)
        if isinstance(entry.record, ContextStartRecord):
            boundary_seq = entry.seq
    return tuple(entry for entry in history.records if (
        entry.record.llm_selection == "from_start"
        or (entry.record.llm_selection == "from_compact" and entry.seq > boundary_seq)
    ))


def select_harness_entries(history: History) -> tuple[HistoryEntry, ...]:
    """Harness независимо читает весь журнал по harness_view."""
    return tuple(entry for entry in history.records if entry.record.harness_view == "include")


def copy_llm_data(entry: HistoryEntry, protocol: str) -> JsonValue:
    """Скопировать структурные поля; текст содержимого читается отдельно."""
    payload = entry.record.llm_payload
    if payload is None:
        raise RequestBuildError(BuildIssue(
            code="missing_llm_payload", message="Prepared LLM payload is missing",
            record_id=entry.record.id, seq=entry.seq,
        ))
    if payload.protocol != protocol:
        raise RequestBuildError(BuildIssue(
            code="incompatible_llm_payload", message="Payload was prepared for a different protocol",
            record_id=entry.record.id, seq=entry.seq,
        ))
    return deepcopy(payload.data)


def read_llm_text(entry: HistoryEntry) -> str:
    """Вернуть сохранённую строку буквально; пустая строка является готовым текстом."""
    payload = entry.record.llm_payload
    if payload is None or payload.text is None:
        raise RequestBuildError(BuildIssue(
            code="missing_llm_text", message="Stored message text is missing",
            record_id=entry.record.id, seq=entry.seq,
        ))
    return payload.text


class ProviderRequestBuilder(Protocol):
    """Сборщик одного формата: отбор, LAST/SUM, проверки и композиция готовых фрагментов."""

    def build(self, history: History) -> JsonObject:
        """Создать запрос из истории: текст копируется буквально, структуры собираются по LAST/SUM."""
        ...
```

```python
# 1. select_llm_entries выбирает записи по llm_selection. kind задаёт категорию
#    LAST/SUM, но не заменяет отбор. Исходные content/tool/model/parameters
#    не используются для повторного рендеринга или восстановления готовых данных.
#
# 2. LAST отдельно для model, sampling, inference, provider_parameters,
#    response_format, tool_choice, streaming. Все семь категорий обязательны.
#    Каждая выбранная запись содержит готовые нативные поля в llm_payload.data.
#    data={} очищает необязательные параметры категории; ModelRecord задаёт модель.
#    Пропущенные поля не дополняются настройками процесса. Пересекающиеся ключи
#    категорий или подмена messages/tools — ошибка. Протокол также берётся из истории.
#
# 3. SUM промптов по llm_payload.key: add заменяет активный ключ, remove удаляет.
#    Неизвестный remove — ошибка. Порядок активных промптов — seq последних add.
#    Текст каждого промпта читается из llm_payload.text без добавления символов.
#    Если протокол принимает одну строку system, SUM соединяет сохранённые строки
#    буквально; необходимые разделители должны уже содержаться в самих строках.
#
# 4. SUM доступности tools по key. ToolRemovedRecord отменяет доступность и схему;
#    ToolAddedRecord без фактического remove сохраняет прежнюю выбранную схему.
#    ToolSchemaRecord — LAST по имени в пределах непрерывной доступности:
#    data содержит готовую схему, catalog_revision — id add, использованного поиском.
#    Результат поиска по прежнему add допустим, если между ними не было remove.
#    Remove неактивного имени, схема без доступности или чужая ревизия — ошибка.
#    Новое подключение после remove не восстанавливает старую схему автоматически.
#    Порядок итоговых схем — seq последних ToolSchemaRecord.
#
# 5. SUM сообщений создаёт новые объекты провайдера при КАЖДОЙ сборке запроса.
#    Для каждого сообщения текст берётся только через read_llm_text(entry).
#    Для example-chat.v1 это message["content"] = read_llm_text(entry);
#    role, tool_calls, tool_call_id и прочие поля копируются из llm_payload.data.
#    Повторный рендеринг, JSON-сериализация исходного content в строку, strip,
#    нормализация переносов/Unicode, текстовые обёртки и склейка сообщений запрещены.
#    Пустая сохранённая строка остаётся пустой строкой, а не заменяется на null.
#    Размещение строки и нативных блоков определяет формат провайдера; это сборка
#    транспортной структуры, не создание нового текста. В data нет второй копии текста.
#    Два источника текста в одной записи — ошибка, без выбора одного из них молча.
#
# 6. Порядок сообщений — seq. Если у служебной записи, команды или маркера явно
#    включён llm_selection и подготовлен текст с data, применяется то же SUM.
#    kind не может молча исключить запись. Вызовы и результаты проверяются
#    по подготовленным нативным данным; незакрытые, повторные или несогласованные
#    вызовы — явная ошибка. Исторические вызовы не требуют наличия схем в текущих tools.
#
# 7. Удаление инструмента исключает его схему из tools. Уже сохранённые сообщения
#    поиска, вызовов и результатов остаются неизменными, включая текст ссылок.
#    Сборщик не вырезает из старых сообщений слова, ссылки или структурные блоки.
#    Изменить содержимое можно только новой записью через очередь и правилами LAST/SUM.
#
# 8. Перед включением проверяются protocol и форма data. Несовместимость — ошибка,
#    без преобразования старых записей. text=None допустим у параметров, схем и
#    управляющих дельт, но не у выбранного сообщения. Отсутствующие payload/text
#    не восстанавливаются из исходных полей. Неизвестный kind отвергает модель истории.
#
# 9. Каждый сохранённый text сохраняет UTF-8-байты в любой сборке, включающей запись,
#    даже после изменения других записей или шаблонов. Для одного префикса истории
#    результат также повторяем целиком. Выходные структуры не разделяют изменяемый
#    JSON с историей. JSON-кодирование HTTP-конверта не меняет значения его строк.

```

## 4 Запись истории, чтение порций и команды

`HistoryWriteQueue` — единственный вход записи. `HistoryConsumer` читает только
сохранённые порции; `SlashCommandRouter` маршрутизирует сохранённые команды.
Эти компоненты не передаются в `ProviderRequestBuilder`.

```python
class HistoryStore(Protocol):
    """Граница хранения этапа записи; сборщики не зависят от этого порта."""

    async def create(self, session_id: NonEmptyStr) -> None:
        """Создать пустой журнал; записи принимаются только через очередь."""
        ...

    async def append(
        self,
        session_id: NonEmptyStr,
        records: NonEmptyTuple[HistoryRecord],
    ) -> NonEmptyTuple[HistoryEntry]:
        """Назначить последовательные seq и сохранить всю пачку одной транзакцией.

        Полный повтор тех же id/данных возвращает прежние HistoryEntry без новой записи.
        Частичное совпадение id, изменение данных или порядка — ошибка целостности.
        Откат транзакции не расходует номера. Единственный писатель — очередь.
        """
        ...

    async def read_after(
        self, session_id: NonEmptyStr, after_seq: NonNegativeInt,
    ) -> HistoryPortion:
        """В одном снимке прочитать ВСЕ сохранённые записи с seq > after_seq по возрастанию.

        Нет записей — пустая порция. after_seq за концом журнала — явная ошибка.
        Атомарно сохранённая пачка не может быть прочитана частично.
        """
        ...

    async def load(
        self, session_id: NonEmptyStr, *, through_seq: NonNegativeInt | None = None,
    ) -> History:
        """Прочитать сохранённый префикс 1..through_seq включительно, со служебными записями.

        None — весь журнал в момент чтения; граница за его концом — явная ошибка.
        """
        ...


class HistoryWriteError(Exception):
    """Хранилище не подтвердило запись; буфер сохраняется для повторной попытки."""


class HistoryQueueFull(Exception):
    """Новая пачка целиком отклонена; принятые ранее сообщения не затронуты."""

    def __init__(self, *, pending: int, incoming: int, limit: int) -> None:
        self.pending = pending
        self.incoming = incoming
        self.limit = limit
        super().__init__(f"History queue full: {pending} + {incoming} > {limit}")


class HistoryWriteQueue:
    """Единый вход всех записей сессии: ограниченный FIFO-буфер и его сохранение."""

    def __init__(
        self, session_id: NonEmptyStr, store: HistoryStore, policy: HistoryWritePolicy,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._policy = policy

    async def open(self) -> None:
        """Прочитать подтверждённый конец журнала перед приёмом сообщений; один раз."""
        raise NotImplementedError

    def enqueue(self, batch: QueuedBatch) -> PendingBatch:
        """Атомарно проверить лимит, скопировать пачку без seq и разбудить run.

        Переполнение — HistoryQueueFull без изменения буфера.
        Повторные id пачки/записи — ValueError. Возвращается независимая копия.
        """
        raise NotImplementedError

    async def flush_once(self) -> bool:
        """Одна попытка append головы; False при HistoryWriteError, пачка остаётся.

        Возвращённые хранилищем seq продвигают durable_seq; подтверждённая пачка снимается.
        Результат append не подменяет чтение истории для сборки. Пустой буфер — True.
        Одновременно выполняется не более одного append, включая вызовы из run/drain.
        """
        raise NotImplementedError

    async def run(self) -> None:
        """Фоновый цикл: flush_once по enqueue; после ошибки задержка по policy и повтор.

        Новая пачка не обгоняет голову и не сокращает задержку её повтора.
        Цикл завершается отменой задачи; отмена не очищает буфер.
        """
        raise NotImplementedError

    async def drain(self) -> None:
        """Через тот же flush_once и задержки дождаться записи принятого к вызову префикса.

        Новые поступления не расширяют барьер. Отмена ожидания не удаляет сообщения.
        """
        raise NotImplementedError

    def snapshot(self) -> QueueSnapshot:
        """Вернуть независимую копию буфера, состояния записи и лимита."""
        raise NotImplementedError


class HistoryConsumer:
    """Чтение порций для одного цикла агента; хранит только курсор, а не копию истории."""

    def __init__(
        self, session_id: NonEmptyStr, store: HistoryStore, processed_seq: NonNegativeInt,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._processed_seq = processed_seq

    @property
    def processed_seq(self) -> int:
        return self._processed_seq

    async def read_pending(self) -> HistoryPortion:
        """Прочитать новые записи после курсора; чтение не подтверждает обработку."""
        return await self._store.read_after(self._session_id, self._processed_seq)

    async def load_history(self, portion: HistoryPortion) -> History:
        """Загрузить из хранилища весь вход сборщика до границы выбранной порции."""
        self._validate_portion(portion)
        return await self._store.load(self._session_id, through_seq=portion.through_seq)

    def acknowledge(self, portion: HistoryPortion) -> None:
        """После успешной обработки продвинуть курсор только до конца этой порции."""
        self._validate_portion(portion)
        self._processed_seq = portion.through_seq

    def _validate_portion(self, portion: HistoryPortion) -> None:
        if portion.session_id != self._session_id or portion.after_seq != self._processed_seq:
            raise ValueError("Foreign or stale history portion")


class SlashCommandExecutor(Protocol):
    """Исполнитель одного владельца; выполняет команду и подготавливает записи результата."""

    async def execute(self, command: SlashCommandRecord) -> QueuedBatch:
        """Вернуть результат и изменения истории одной пачкой, без прямой записи в store.

        Пачка содержит один SlashCommandResultRecord с command_id=command.id.
        Остальные записи явно описывают эффекты команды, например новый контекст.
        """
        ...


class SlashCommandRouter:
    """Маршрутизация по записанному владельцу; не собирает LLM-запрос и не хранит историю."""

    def __init__(
        self, *, agent: SlashCommandExecutor, client: SlashCommandExecutor,
        mcp_prompt: SlashCommandExecutor, mcp_app: SlashCommandExecutor,
    ) -> None:
        self._executors = {
            "agent": agent, "client": client, "mcp_prompt": mcp_prompt, "mcp_app": mcp_app,
        }

    async def execute(self, entry: HistoryEntry) -> QueuedBatch:
        """Исполнить slash-команду из прочитанной порции; возвращённую пачку принимает общая очередь."""
        if not isinstance(entry.record, SlashCommandRecord):
            raise ValueError("Expected a persisted slash command")
        command = entry.record
        batch = await self._executors[command.owner.kind].execute(command)
        results = tuple(record for record in batch.records if isinstance(record, SlashCommandResultRecord))
        if len(results) != 1 or results[0].command_id != command.id:
            raise ValueError("Executor must return one result for this command")
        return batch


```

## 5 Единственный путь записи истории

```python
# Производитель формирует исходные данные и llm_payload один раз, затем:
# HistoryRecord -> HistoryWriteQueue.enqueue(QueuedBatch)
# -> HistoryWriteQueue.flush_once() -> HistoryStore.append(HistoryRecord)
# -> сохранённые HistoryEntry с назначенными хранилищем seq.
# В очередь поступают все типы сообщений: ввод, настройки, MCP, ответы, результаты
# инструментов, slash-команды, их результаты и служебные события.
# Производители не вызывают HistoryStore.append.
# Очередь и хранилище сохраняют text буквально и data без изменений, не вызывая рендерер.
# Повтор записи использует прежний текст, даже если шаблоны или настройки изменились.
# Изменение подготовленного содержимого — новая запись через ту же очередь.
# Сборщик не вызывает производителей, не рендерит исходные поля и не исправляет payload.
#
# Очередь одной сессии обслуживается одним писателем. Пачки сохраняются целиком,
# FIFO по enqueue; created_at/enqueued_at и тип сообщения не меняют порядок.
# open читает конец сохранённого журнала. run выполняется фоновой задачей.
# Публичный flush_once позволяет выполнить ту же попытку явно, в том числе в тесте.
# Очередь отвечает за запись; последовательность действий LLM/tools задаёт агент.
# Принятие сообщения в очередь само по себе не запускает LLM-запрос.
#
# N = policy.max_pending_messages. Считаются сообщения во ВСЕХ принятых,
# неподтверждённых пачках, включая текущий append. При pending + incoming > N
# enqueue выбрасывает HistoryQueueFull: вся новая пачка отклонена, старые остаются.
# Частичный приём и вытеснение старых сообщений запрещены. В очереди seq вообще нет.
# Ошибку получает вызывающий код; для её показа не нужна обходная запись в историю.
#
# При enqueue копируется содержимое; присвоение seq принадлежит только хранилищу.
# Повтор append использует ТОЧНО те же id, created_at, порядок и данные без seq.
# Хранилище в одной транзакции проверяет id, назначает непрерывные номера после
# конца журнала и сохраняет всю пачку. Сообщение считается добавленным только после commit.
# Снять пачку из буфера можно только после подтверждения HistoryStore.append.
# Ошибка оставляет её в голове; attempts и last_error доступны через snapshot.
# Повторы идут с policy.retry_delays_ms; последняя задержка повторяется до успеха.
# Поздние пачки не обходят неуспешную. Нет удаления после некоторого числа попыток.
# Если запись состоялась, но подтверждение потеряно, повтор по тем же id возвращает
# прежние seq. Новых записей и новых номеров не появляется.
# Конфликт id/содержимого/порядка — ошибка целостности; он явно завершает фоновую задачу,
# сохраняет буфер и не маскируется повтором как временная HistoryWriteError.
#
# Принято в очередь не означает сохранено. drain — барьер записи уже принятого
# префикса. При завершении агента прекращается приём, выполняется drain, затем
# отменяется run. Отмена/ошибка ожидания не считается успешным сохранением.
# Буфер находится в памяти: это повторы в живом процессе, а не гарантия при его падении.
# Ссылки catalog_revision указывают record.id, поэтому назначение seq их не меняет.
# QueueStateRecord — служебный снимок, а не источник сообщений для сборки запроса.
# Он проходит ту же очередь, учитывается в N и не порождает рекурсивные снимки.
# durable_seq — последний подтверждённый очереди номер записи; processed_seq —
# последний успешно обработанный номер у HistoryConsumer. Это разные границы.
# Курсор обработки не берётся из durable_seq и не меняется при enqueue/append.
#
# Производитель ставит в очередь поток как AssistantStreamRecord и один полный
# ответ как AssistantMessageRecord. В LLM-проекцию входит только полный ответ.
# При отмене открытого вызова в очередь поступает ToolResultRecord(is_error=True).
#
# В начале разговора производитель ставит в очередь ModelRecord, SamplingRecord,
# InferenceRecord, ProviderParametersRecord, ResponseFormatRecord, ToolChoiceRecord
# и StreamingRecord, включая явно пустые значения. Изменение одной категории
# представляется только её сообщением. Связанные изменения идут одной QueuedBatch.
# Сборщик не читает конфигурацию, модель по умолчанию, часы или файлы промпта.
#
# Производитель ставит в очередь SystemPromptAddedRecord с полным промптом одного типа.
# Замена: ещё один add того же prompt_type. Отмена: SystemPromptRemovedRecord.
# Производитель не обязан искать предыдущий add или вызывать сборщик.
#
# При ЛЮБОМ обновлении MCP производитель последовательно сравнивает предыдущий
# и полученный полные списки по именам. Для исчезнувших имён он создаёт ToolRemovedRecord,
# для каждого имени текущего списка — ToolAddedRecord, включая неизменные инструменты.
# Связанные дельты поступают одной QueuedBatch; отказ enqueue не продвигает исходный
# список производителя. Сравнение списков относится к этапу появления сообщений.
# При отключении MCP удаляется каждое доступное имя с его префиксом mcp__<server>__.
# Пустой список удаляет оставшиеся имена; если их не было, дельт инструментов нет.
# Само переподключение не означает исчезновения инструмента. Неизменные имена
# не получают искусственный remove, поэтому их выбранные схемы сохраняются.
# Имена уже содержат MCP-префикс; сборщик их не создаёт и не разбирает.
# Собственные инструменты используют те же ToolAddedRecord/ToolRemovedRecord.
# При обновлении MCP известен список инструментов; изменение полных схем не определяется.
#
# После ToolSearch производитель ставит в очередь ToolResultRecord со ссылками и
# отдельный ToolSchemaRecord для каждого выбранного инструмента одной QueuedBatch,
# которая сохраняется целиком. До подтверждения записи схемы не участвуют в запросах.
# catalog_revision — id ToolAddedRecord выбранного имени на момент поиска, а не id,
# подставленный после завершения поиска. Исходная схема записана в tool, готовая нативная — в llm_payload.data.
# ToolSearch и другие изначально загруженные tools также получают явный ToolSchemaRecord.
# Наличие определения в живом MCP или старом результате поиска не заменяет эту запись.
# Обновление MCP само не пишет определения. Схемы оставшихся tools продолжают действовать.
# Если вызов по старой схеме вернул ошибку, модель может снова вызвать ToolSearch:
# новая ToolSchemaRecord заменит старую по LAST без удаления инструмента из каталога.
# Ошибка сама не является доказательством изменения схемы и ничего не обновляет автоматически.
#
# Результат сжатия поступает в очередь одной неделимой пачкой, затем в тот же журнал:
# ContextStartRecord -> CompactMessageRecord -> дельты tools -> сохраняемые сообщения.
# Для каждого доступного инструмента фиксируется add; для исчезнувших — remove.
# Настройки действуют по LAST, промпты/tools — по SUM всех предыдущих дельт.
# Изменения промптов фиксируются своими add/remove, настроек — записями своих категорий.
# Сам маркер и повторные add не сбрасывают схемы. Только remove фактически
# исчезнувших инструментов отменяют их выбранные схемы.
# Сохраняются целые пары вызов-результат; новые сообщения получают новые id,
# а seq назначает хранилище при сохранении пачки.
# Harness продолжает показывать старые сообщения и события с harness_view=include.
# Сборщик не восстанавливает сообщения предыдущего контекста.
# При переносе сохраняемых сообщений производитель переносит и готовый payload.
# При изменении содержимого готовится новый payload; сборщик не делает это сам.
```

### Slash-команды и их владельцы

```python
# Владелец — тот, кто объявил команду, а не источник её вызова. Все команды могут
# вызываться пользователем из чата. owner фиксируется в SlashCommandRecord при приёме
# вызова по объявлению команды; при воспроизведении её не перенаправляют по новому меню.
#
# agent: команда агента, например /compact; её исполняет агент.
# client: команда клиента; обработчик клиента выполняет её и возвращает результат.
# mcp_prompt: команда MCP-промпта; обработчик получает данные через prompts/get.
# mcp_app: команда MCP App; обработчик вызывает записанный tool_name через tools/call
# и передаёт данные приложению resource_uri. Интерфейс отображает клиент.
# Для MCP адрес сервера определяется полным именем; отдельное поле сервера не нужно.
#
# SlashCommandRecord -> общая очередь -> store с seq -> HistoryConsumer.read_pending().
# Цикл агента передаёт сохранённую запись в SlashCommandRouter.execute(entry).
# Возвращённая QueuedBatch -> та же очередь -> store -> следующая прочитанная порция.
# Ни ввод команды, ни её результат не должны обходить этот путь ради немедленного показа.
# Чат показывает ещё не сохранённый ввод из снимка очереди только при harness_view=include,
# с соответствующим статусом ожидания записи.
#
# Вызов отображается по text, результат — по command_id, outcome и content.
# Интерфейс MCP App берёт resource_uri из owner исходного вызова и данные результата.
# У SlashCommandResultRecord по умолчанию llm_selection=exclude; решение отбора принимает поле.
# Изменения модели, промптов, tools и компактизация фиксируются своими записями.
# CompactMessageRecord используется только для пересказа после компактизации.
# Вызов инструмента вручную через MCP App не создаёт ответ на несуществующий LLM tool_call.
#
# /compact сохраняется как SlashCommandRecord(owner=AgentCommandOwner()).
# При успехе результат и новый контекст записываются одной пачкой:
# SlashCommandResultRecord -> ContextStartRecord(reason="compaction")
# -> CompactMessageRecord -> сохраняемые сообщения и дельты.
# При неудаче записывается failed-результат; текущий контекст сохраняется.
#
# failed/cancelled — окончательные исходы команды, а не повод исполнить её повторно.
# Повтор сохранения результата использует готовую пачку, а не повторный execute.
# Курсор обработки продвигается по правилам общего цикла; сборщик команды не маршрутизирует.
```

### Чтение после завершения каждого сообщения

```python
# Один цикл обработки на сессию; запись в хранилище может идти параллельно.
#
# 1. Завершённое сообщение пользователя, LLM или tool поступает в общую очередь.
#    Чтение порций независимо проверяет уже сохранённые данные: ждать опустошения
#    очереди не требуется. Если новое сообщение ещё не сохранено, оно попадёт
#    в следующую порцию после commit, который разбудит цикл обработки.
#    Части AssistantStreamRecord остаются служебными записями; завершённый ответ
#    представлен AssistantMessageRecord. Ждать TurnEndRecord не требуется.
#
# 2. После КАЖДОГО завершения сообщения: portion = await consumer.read_pending().
#    Начальная граница полного чтения — processed_seq=0. Хранилище возвращает
#    все записи с seq > processed_seq в одном снимке и в порядке seq.
#    Пустая порция — нечего обрабатывать; курсор не меняется, запроса к LLM нет.
#
# 3. Непустая порция поступает в общий алгоритм цикла агента. Когда его следующий
#    шаг — обращение к LLM: history = await consumer.load_history(portion), затем
#    request = builder.build(history). Все данные прочитаны из хранилища.
#    Вход сборщика — полный префикс 1..portion.through_seq, а не один новый хвост:
#    ранние настройки, промпты, доступность tools и выбранные схемы участвуют в LAST/SUM.
#    Чтение префикса ограничено границей порции: более поздняя запись не попадает
#    случайно в уже начатую сборку. Очередь, результат append и память исполнителя
#    не дополняют history. Сборщик ничего не знает о processed_seq.
#
# 4. consumer.acknowledge(portion) вызывается только после успешной обработки
#    выбранной порции общим алгоритмом. Успешное чтение или один build не являются
#    подтверждением. При ошибке обработки курсор прежний; ошибка явно передаётся выше.
#    Продвижение — ровно до portion.through_seq, а не до нового конца журнала.
#
# 5. Новые ответы, результаты tools и служебные события снова идут через ту же
#    очередь в то же хранилище. После завершения следующего отдельного сообщения
#    повторяется чтение после processed_seq, даже если текущий ход ещё продолжается.
#    Сохранение новых данных будит свободный цикл. Если он занят, проверка происходит
#    на ближайшей границе сообщения. Пробуждение — сигнал перечитать хранилище,
#    а не передача сообщений в обход него; повторные сигналы не дублируют обработку.
#
# Обработка порции не обязательно означает новый HTTP-запрос к LLM: служебные
# записи учитываются и пропускаются LLM-проекцией; slash-команда маршрутизируется
# владельцу; вызов tool требует выполнения инструмента; незавершённый поток/набор
# результатов требует продолжения обработки.
# Готовность к следующему LLM-запросу определяет цикл агента. Ошибки порядка,
# обнаруженные сборщиком, не превращаются в пустую порцию и не исправляются молча.
#
# HistoryConsumer хранит курсор текущего цикла. Он не является новым журналом,
# не кэширует вход сборщика и не гарантирует восстановление исполнения после сбоя.
# В тестах чистой сборки History создаётся напрямую; runtime читает её только из store.
```

## 6 Проверки готового payload

Примеры используют явно заданный тестовый протокол `example-chat.v1`.
Это форма тестового тела запроса, а не объявление API какого-либо сервиса.
Fixture содержит исходные данные, отдельно сохранённые text и data. Производитель
подготовил их до записи; тест сборщика не вызывает рендерер. Проверяется совпадение
UTF-8-байтов текста, а не только эквивалентность JSON-объектов.

```python
from datetime import datetime, timezone
import asyncio

TEST_PROTOCOL = "example-chat.v1"


def provider_builder() -> ProviderRequestBuilder:
    """Точка подключения реализации протокола для контрактных тестов."""
    raise NotImplementedError


def prepared_user(record_id: str, text: str) -> UserMessageRecord:
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return UserMessageRecord(
        id=record_id, created_at=at, content=(TextPart(text=text),),
        llm_payload=LlmPayload(protocol=TEST_PROTOCOL, text=text, data={"role": "user"}),
    )


def add_records(history: History, *records: HistoryRecord) -> History:
    return History(session_id=history.session_id, records=(
        *history.records,
        *(HistoryEntry(seq=len(history.records) + index + 1, record=record) for index, record in enumerate(records)),
    ))
```

```python
def journal_fixture() -> History:
    return History.model_validate_json(r"""
{
  "format_version": 1,
  "session_id": "session-1",
  "records": [
    {
      "seq": 1,
      "record": {
        "kind": "model",
        "harness_view": "exclude",
        "model": {
          "provider": "example",
          "model": "model-from-history"
        },
        "id": "r1",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "model": "model-from-history"
          },
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 2,
      "record": {
        "kind": "sampling",
        "harness_view": "exclude",
        "parameters": {
          "temperature": 0.2,
          "top_k": 17
        },
        "id": "r2",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "temperature": 0.2,
            "top_k": 17
          },
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 3,
      "record": {
        "kind": "inference",
        "harness_view": "exclude",
        "parameters": {
          "max_output_tokens": 1024,
          "reasoning": {
            "effort": "medium"
          }
        },
        "id": "r3",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "max_output_tokens": 1024,
            "reasoning": {
              "effort": "medium"
            }
          },
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 4,
      "record": {
        "kind": "provider_parameters",
        "harness_view": "exclude",
        "parameters": {
          "stream_options": {
            "include_usage": true
          },
          "custom_backend_parameter": {
            "mode": "recorded"
          }
        },
        "id": "r4",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "stream_options": {
              "include_usage": true
            },
            "custom_backend_parameter": {
              "mode": "recorded"
            }
          },
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 5,
      "record": {
        "kind": "response_format",
        "harness_view": "exclude",
        "format": null,
        "id": "r5",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {},
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 6,
      "record": {
        "kind": "tool_choice",
        "harness_view": "exclude",
        "choice": {
          "kind": "auto"
        },
        "id": "r6",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "tool_choice": "auto"
          },
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 7,
      "record": {
        "kind": "streaming",
        "harness_view": "exclude",
        "enabled": true,
        "id": "r7",
        "created_at": "2026-01-01T00:00:00Z",
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "stream": true
          },
          "key": null,
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 8,
      "record": {
        "kind": "system_prompt_added",
        "harness_view": "exclude",
        "id": "r8",
        "created_at": "2026-01-01T00:00:00Z",
        "prompt_type": "agent.role",
        "content": [
          {
            "kind": "text",
            "text": "Отвечай кратко."
          }
        ],
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "role": "system"
          },
          "key": "agent.role",
          "catalog_revision": null,
          "text": "Отвечай кратко."
        }
      }
    },
    {
      "seq": 9,
      "record": {
        "kind": "tool_added",
        "harness_view": "exclude",
        "id": "r9",
        "created_at": "2026-01-01T00:00:00Z",
        "tool": {
          "name": "mcp__database__list_tables",
          "description": "Список таблиц"
        },
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": null,
          "key": "mcp__database__list_tables",
          "catalog_revision": null,
          "text": null
        }
      }
    },
    {
      "seq": 10,
      "record": {
        "kind": "tool_schema",
        "harness_view": "exclude",
        "id": "r10",
        "created_at": "2026-01-01T00:00:00Z",
        "catalog_revision": "r9",
        "tool": {
          "name": "mcp__database__list_tables",
          "description": "Список таблиц",
          "parameters": {
            "type": "object",
            "properties": {}
          },
          "deferred": false
        },
        "llm_selection": "from_start",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "type": "function",
            "function": {
              "name": "mcp__database__list_tables",
              "description": "Список таблиц",
              "parameters": {
                "type": "object",
                "properties": {}
              }
            },
            "deferred": false
          },
          "key": "mcp__database__list_tables",
          "catalog_revision": "r9",
          "text": null
        }
      }
    },
    {
      "seq": 11,
      "record": {
        "kind": "user_message",
        "harness_view": "include",
        "id": "r11",
        "created_at": "2026-01-01T00:00:00Z",
        "content": [
          {
            "kind": "text",
            "text": "Покажи таблицы."
          }
        ],
        "llm_selection": "from_compact",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "role": "user"
          },
          "key": null,
          "catalog_revision": null,
          "text": "Покажи таблицы."
        }
      }
    },
    {
      "seq": 12,
      "record": {
        "kind": "assistant_message",
        "harness_view": "include",
        "id": "r12",
        "created_at": "2026-01-01T00:00:00Z",
        "request_id": "request-1",
        "content": [
          {
            "kind": "tool_call",
            "call_id": "call-1",
            "name": "mcp__database__list_tables",
            "arguments": {}
          }
        ],
        "llm_selection": "from_compact",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "role": "assistant",
            "tool_calls": [
              {
                "id": "call-1",
                "type": "function",
                "function": {
                  "name": "mcp__database__list_tables",
                  "arguments": "{ }"
                }
              }
            ]
          },
          "key": null,
          "catalog_revision": null,
          "text": ""
        }
      }
    },
    {
      "seq": 13,
      "record": {
        "kind": "llm_request_attempt",
        "harness_view": "include",
        "id": "r13",
        "created_at": "2026-01-01T00:00:00Z",
        "request_id": "request-1",
        "attempt": 1,
        "method": "POST",
        "url": "https://example.invalid/inference",
        "status_code": 200,
        "duration_ms": 25,
        "llm_selection": "exclude",
        "llm_payload": null
      }
    },
    {
      "seq": 14,
      "record": {
        "kind": "token_usage",
        "harness_view": "include",
        "id": "r14",
        "created_at": "2026-01-01T00:00:00Z",
        "request_id": "request-1",
        "usage": {
          "input_tokens": 32,
          "output_tokens": 8
        },
        "llm_selection": "exclude",
        "llm_payload": null
      }
    },
    {
      "seq": 15,
      "record": {
        "kind": "tool_result",
        "harness_view": "include",
        "id": "r15",
        "created_at": "2026-01-01T00:00:00Z",
        "call_id": "call-1",
        "content": [
          {
            "kind": "text",
            "text": "users, orders"
          }
        ],
        "is_error": false,
        "llm_selection": "from_compact",
        "llm_payload": {
          "protocol": "example-chat.v1",
          "data": {
            "role": "tool",
            "tool_call_id": "call-1"
          },
          "key": null,
          "catalog_revision": null,
          "text": "users, orders"
        }
      }
    }
  ]
}
    """)
```

```python
def test_selected_record_requires_prepared_payload() -> None:
    raw = journal_fixture().records[10].record.model_dump()
    raw.pop("llm_payload")
    try:
        HISTORY_RECORD_ADAPTER.validate_python(raw)
    except ValidationError:
        pass
    else:
        raise AssertionError("Included records must not fall back to source fields")
    raw["llm_selection"] = "exclude"
    assert HISTORY_RECORD_ADAPTER.validate_python(raw).llm_payload is None


def test_selected_message_requires_text_but_accepts_empty_string() -> None:
    raw = journal_fixture().records[10].record.model_dump()
    raw["llm_payload"].pop("text")
    try:
        HISTORY_RECORD_ADAPTER.validate_python(raw)
    except ValidationError:
        pass
    else:
        raise AssertionError("Raw content must not substitute for missing stored text")
    raw["llm_payload"]["text"] = ""
    assert HISTORY_RECORD_ADAPTER.validate_python(raw).llm_payload.text == ""


def test_stored_text_round_trip_preserves_utf8_bytes() -> None:
    for index, text in enumerate(("", "Привет", "  a\r\nb\n\t", "e\u0301 ≠ é", '<tag>"\\</tag> 😀')):
        record = prepared_user(f"literal-{index}", text)
        restored = UserMessageRecord.model_validate_json(record.model_dump_json())
        entry = HistoryEntry(seq=1, record=restored)
        assert read_llm_text(entry).encode("utf-8") == text.encode("utf-8")


def test_ready_payload_is_independent_of_source_data() -> None:
    history = journal_fixture()
    raw = history.model_dump()
    raw["records"][10]["record"]["content"] = [{"kind": "text", "text": "Другой исходный текст"}]
    raw["records"][11]["record"]["content"][0]["arguments"] = {"changed": True}
    changed = History.model_validate(raw)
    assert read_llm_text(changed.records[10]) == "Покажи таблицы."
    assert copy_llm_data(changed.records[10], TEST_PROTOCOL) == {"role": "user"}
    data = copy_llm_data(changed.records[11], TEST_PROTOCOL)
    assert data["tool_calls"][0]["function"]["arguments"] == "{ }"
    assert changed.records[11].record.content[0].arguments == {"changed": True}


def test_copied_payload_does_not_mutate_the_journal() -> None:
    history = journal_fixture()
    before = history.model_dump_json()
    body = copy_llm_data(history.records[11], TEST_PROTOCOL)
    body["tool_calls"][0]["function"]["arguments"] = "changed"
    assert history.model_dump_json() == before


def test_wrong_protocol_is_an_error_without_conversion() -> None:
    entry = journal_fixture().records[10]
    try:
        copy_llm_data(entry, "different-protocol.v1")
    except RequestBuildError as error:
        assert error.issue.code == "incompatible_llm_payload"
        assert error.issue.record_id == entry.record.id
    else:
        raise AssertionError("No implicit rendering for another protocol")


def test_all_history_record_models_declare_selection_and_harness_view() -> None:
    expected = {
        ("from_start", "exclude"): (
            "system_prompt_added", "system_prompt_removed", "tool_added", "tool_removed", "tool_schema",
            "model", "sampling", "inference", "provider_parameters", "response_format", "tool_choice", "streaming",
        ),
        ("from_compact", "include"): ("user_message", "compact_message", "assistant_message", "tool_result"),
        ("exclude", "include"): (
            "context_start", "slash_command", "slash_command_result", "queue_state",
            "permission_state", "permission_decision", "llm_request_attempt", "token_usage",
            "notice", "assistant_stream", "turn_end",
        ),
    }
    schemas = HISTORY_RECORD_ADAPTER.json_schema()["$defs"].values()
    models = [s["properties"] for s in schemas if {"kind", "id", "created_at"} <= s.get("properties", {}).keys()]
    actual = {}
    for fields in models:
        assert set(fields["llm_selection"]["enum"]) == {"exclude", "from_compact", "from_start"}
        assert set(fields["harness_view"]["enum"]) == {"exclude", "include"}
        assert "llm_payload" in fields
        actual[fields["kind"]["const"]] = (fields["llm_selection"]["default"], fields["harness_view"]["default"])
    assert actual == {kind: views for views, kinds in expected.items() for kind in kinds}


def test_selection_and_harness_view_are_independent() -> None:
    history = journal_fixture()
    expected = [entry.seq for entry in select_llm_entries(history)]
    raw = history.model_dump()
    for entry in raw["records"]:
        record = entry["record"]
        record["harness_view"] = "exclude" if record["harness_view"] == "include" else "include"
    changed = History.model_validate(raw)
    assert [entry.seq for entry in select_llm_entries(changed)] == expected
    assert [entry.seq for entry in select_harness_entries(changed)] == list(range(1, 11))
    for entry in raw["records"]:
        entry["record"]["llm_selection"] = "exclude"
    excluded = History.model_validate(raw)
    assert select_llm_entries(excluded) == ()
    assert select_harness_entries(excluded) == excluded.records[:10]


def test_compaction_keeps_global_payloads_and_only_new_dialogue() -> None:
    original = journal_fixture()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    history = add_records(
        original,
        ContextStartRecord(id="boundary", created_at=at, reason="compaction"),
        CompactMessageRecord(
            id="summary", created_at=at, content=(TextPart(text="Исходный пересказ"),),
            llm_payload=LlmPayload(protocol=TEST_PROTOCOL, text="Готовый пересказ", data={"role": "user"}),
        ),
    )
    assert [entry.seq for entry in select_llm_entries(history)] == [*range(1, 11), 17]
    assert [entry.seq for entry in select_harness_entries(history)] == [11, 12, 13, 14, 15, 16, 17]
    assert read_llm_text(history.records[-1]) == "Готовый пересказ"


def test_integrity_checks_also_cover_excluded_records() -> None:
    raw = journal_fixture().model_dump()
    raw["records"][12]["seq"] = 99
    try:
        select_llm_entries(History.model_validate(raw))
    except RequestBuildError as error:
        assert error.issue.code == "invalid_history"
    else:
        raise AssertionError("Filtering must not hide invalid history")


def test_builder_preserves_stored_text_and_native_fields() -> None:
    history = journal_fixture()
    before = history.model_dump_json()
    request = provider_builder().build(history)
    assert request["model"] == "model-from-history"
    assert request["temperature"] == 0.2 and request["top_k"] == 17
    assert request["stream"] is True
    assert "response_format" not in request
    assert len(request["messages"]) == 4
    for message, index in zip(request["messages"], (7, 10, 11, 14), strict=True):
        payload = history.records[index].record.llm_payload
        assert message["content"].encode("utf-8") == payload.text.encode("utf-8")
        assert {key: value for key, value in message.items() if key != "content"} == payload.data
    assert request["tools"] == [history.records[9].record.llm_payload.data]
    assert request["messages"][2]["tool_calls"][0]["function"]["arguments"] == "{ }"
    assert history.model_dump_json() == before


def test_old_message_text_is_byte_identical_in_following_requests() -> None:
    original = journal_fixture()
    raw = original.model_dump()
    text = "  Строка\r\n\t e\u0301 😀  "
    raw["records"][10]["record"]["llm_payload"]["text"] = text
    original = History.model_validate(raw)
    extended = add_records(original, prepared_user("next-user", "Следующий вопрос"))
    builder = provider_builder()
    for history in (original, extended, original):
        request = builder.build(history)
        assert request["messages"][1]["content"].encode("utf-8") == text.encode("utf-8")


def test_builder_rejects_duplicate_text_in_data() -> None:
    raw = journal_fixture().model_dump()
    raw["records"][10]["record"]["llm_payload"]["data"]["content"] = "Второй источник текста"
    history = History.model_validate(raw)
    builder = provider_builder()
    try:
        builder.build(history)
    except RequestBuildError as error:
        assert error.issue.code == "conflicting_message_text"
        assert error.issue.record_id == "r11"
    else:
        raise AssertionError("The text field must be the only source of message text")


def test_builder_does_not_recreate_payload_from_changed_source() -> None:
    history = journal_fixture()
    raw = history.model_dump()
    raw["records"][0]["record"]["model"]["model"] = "not-the-prepared-model"
    raw["records"][1]["record"]["parameters"] = {"temperature": 999}
    raw["records"][7]["record"]["content"] = [{"kind": "text", "text": "Иной исходный промпт"}]
    raw["records"][9]["record"]["tool"]["parameters"] = {"changed": True}
    raw["records"][10]["record"]["content"] = [{"kind": "text", "text": "Иной исходный текст"}]
    changed = History.model_validate(raw)
    builder = provider_builder()
    assert builder.build(changed) == builder.build(history)


def test_builder_preserves_text_with_images_and_native_reasoning() -> None:
    raw = journal_fixture().model_dump()
    user_payload = raw["records"][10]["record"]["llm_payload"]
    user_payload["text"] = "  Посмотри\r\n"
    # У example-chat.v1 изображения — отдельное структурное поле сообщения.
    user_payload["data"]["images"] = [{"media_type": "image/png", "data": "aW1hZ2U="}]
    assistant_data = raw["records"][11]["record"]["llm_payload"]["data"]
    assistant_data["reasoning"] = {"text": "Готовое рассуждение", "signature": "signed-value"}
    history = History.model_validate(raw)
    request = provider_builder().build(history)
    assert request["messages"][1]["content"].encode("utf-8") == user_payload["text"].encode("utf-8")
    assert request["messages"][1]["images"] == user_payload["data"]["images"]
    assert request["messages"][2]["reasoning"] == assistant_data["reasoning"]


def test_explicit_selection_includes_service_payload() -> None:
    history = journal_fixture()
    selected = NoticeRecord(
        id="included-notice", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        level="info", content=(TextPart(text="Исходное уведомление"),),
        llm_selection="from_compact", harness_view="exclude",
        llm_payload=LlmPayload(protocol=TEST_PROTOCOL,
            text="Подготовленное уведомление для модели", data={"role": "user"}),
    )
    request = provider_builder().build(add_records(history, selected))
    assert request["messages"][-1] == {
        "role": "user", "content": "Подготовленное уведомление для модели",
    }


def test_builder_is_repeatable_and_returns_independent_json() -> None:
    history = journal_fixture()
    before = history.model_dump_json()
    builder = provider_builder()
    expected = builder.build(history)
    changed = builder.build(history)
    changed["messages"][2]["tool_calls"][0]["function"]["arguments"] = "changed"
    assert history.model_dump_json() == before
    assert builder.build(history) == expected


def test_prompt_add_remove_uses_saved_text_and_key() -> None:
    history = journal_fixture()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = history.records[7].record.llm_payload.key
    replacement = SystemPromptAddedRecord(
        id="prompt-replaced", created_at=at, prompt_type="source-only-type",
        content=(TextPart(text="Исходный текст"),),
        llm_payload=LlmPayload(protocol=TEST_PROTOCOL, key=key,
            text="Новый готовый промпт\n", data={"role": "system"}),
    )
    builder = provider_builder()
    changed = add_records(history, replacement)
    assert builder.build(changed)["messages"][0] == {
        "role": "system", "content": "Новый готовый промпт\n",
    }
    removed = add_records(changed, SystemPromptRemovedRecord(
        id="prompt-removed", created_at=at, prompt_type="source-only-type",
        llm_payload=LlmPayload(protocol=TEST_PROTOCOL, key=key, data=None),
    ))
    assert builder.build(removed)["messages"] == builder.build(history)["messages"][1:]


def test_last_parameter_payload_replaces_only_its_category() -> None:
    history = journal_fixture()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    changed = add_records(history, SamplingRecord(
        id="sampling-clear", created_at=at, parameters={"source_only": 1},
        llm_payload=LlmPayload(protocol=TEST_PROTOCOL, data={}),
    ))
    builder = provider_builder()
    expected = builder.build(history)
    del expected["temperature"]
    del expected["top_k"]
    assert builder.build(changed) == expected


def test_tool_removal_discards_schema_and_add_does_not_restore_it() -> None:
    history = journal_fixture()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = "mcp__database__list_tables"
    changed = add_records(
        history,
        ToolRemovedRecord(id="removed", created_at=at, tool_name=key,
            llm_payload=LlmPayload(protocol=TEST_PROTOCOL, key=key, data=None)),
        ToolAddedRecord(id="returned", created_at=at, tool=history.records[8].record.tool,
            llm_payload=LlmPayload(protocol=TEST_PROTOCOL, key=key, data=None)),
    )
    assert provider_builder().build(changed)["tools"] == []


def test_repeated_tool_add_preserves_selected_native_schema() -> None:
    history = journal_fixture()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    repeated = history.records[8].record.model_copy(update={"id": "repeated", "created_at": at}, deep=True)
    builder = provider_builder()
    assert builder.build(add_records(history, repeated))["tools"] == builder.build(history)["tools"]


def test_removal_excludes_schema_without_rewriting_old_message() -> None:
    raw = journal_fixture().model_dump()
    payload = raw["records"][-1]["record"]["llm_payload"]
    payload["text"] = "Найден tool mcp__database__list_tables.  \r\n"
    history = History.model_validate(raw)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    changed = add_records(history, ToolRemovedRecord(
        id="removed", created_at=at, tool_name="mcp__database__list_tables",
        llm_payload=LlmPayload(protocol=TEST_PROTOCOL, key="mcp__database__list_tables", data=None),
    ))
    before = changed.model_dump_json()
    builder = provider_builder()
    request = builder.build(changed)
    assert request["tools"] == []
    assert request["messages"] == builder.build(history)["messages"]
    assert request["messages"][-1]["content"].encode("utf-8") == payload["text"].encode("utf-8")
    assert changed.model_dump_json() == before

```

Функции с `provider_builder()` — контрактные тесты будущей реализации.
Исполняемые проверки моделей, чтения payload и отбора не подменяют эти тесты.
Для каждого провайдера нужны также проверки всех категорий LAST, конфликтов полей,
дельт промптов, поздних схем, пар tool-call/result и неподдерживаемой формы data.
В fixtures должны различаться исходные данные и подготовленные значения; иначе
тест не обнаружит повторный рендеринг. Отдельно сохраняются пустые строки, null,
пробелы, порядок блоков, строковые arguments, изображения и reasoning.

## 7 Проверки доставки в историю

Очередь и чтение сохранённых порций проверяются отдельно от рендеринга и сборки.
Следующий fake реализует порт хранения; runtime-писателем остаётся только очередь.

```python
class FakeHistoryStore:
    """Только для тестов: отказ до записи, потеря подтверждения и задержанный append."""

    def __init__(self) -> None:
        self.histories: dict[str, History] = {}
        self.attempts: list[tuple[HistoryRecord, ...]] = []
        self.fail_before_commit = False
        self.lose_next_ack = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def create(self, session_id: NonEmptyStr) -> None:
        if session_id in self.histories:
            raise ValueError("Session already exists")
        self.histories[session_id] = History(session_id=session_id, records=())

    async def append(
        self, session_id: NonEmptyStr, records: NonEmptyTuple[HistoryRecord],
    ) -> NonEmptyTuple[HistoryEntry]:
        self.attempts.append(tuple(record.model_copy(deep=True) for record in records))
        self.entered.set()
        await self.release.wait()
        if self.fail_before_commit:
            raise HistoryWriteError("Storage unavailable")
        current = self.histories[session_id]
        if not records or len({record.id for record in records}) != len(records):
            raise ValueError("Empty batch or duplicate id")
        by_id = {entry.record.id: entry for entry in current.records}
        existing = tuple(by_id[record.id] for record in records if record.id in by_id)
        if existing:
            if tuple(entry.record for entry in existing) != records or any(
                entry.seq != existing[0].seq + index for index, entry in enumerate(existing)
            ):
                raise ValueError("Partial or conflicting replay")
            saved = existing
        else:
            saved = tuple(HistoryEntry(
                seq=len(current.records) + index + 1, record=record.model_copy(deep=True),
            ) for index, record in enumerate(records))
            self.histories[session_id] = History(
                session_id=session_id, records=(*current.records, *saved),
            )
        if self.lose_next_ack:
            self.lose_next_ack = False
            raise HistoryWriteError("Acknowledgement lost")
        return tuple(entry.model_copy(deep=True) for entry in saved)

    async def read_after(self, session_id: NonEmptyStr, after_seq: NonNegativeInt) -> HistoryPortion:
        history = self.histories[session_id]
        if not 0 <= after_seq <= len(history.records):
            raise ValueError("Cursor outside history")
        return HistoryPortion(
            session_id=session_id, after_seq=after_seq,
            records=tuple(entry.model_copy(deep=True) for entry in history.records[after_seq:]),
        )

    async def load(
        self, session_id: NonEmptyStr, *, through_seq: NonNegativeInt | None = None,
    ) -> History:
        history = self.histories[session_id]
        end = len(history.records) if through_seq is None else through_seq
        if not 0 <= end <= len(history.records):
            raise ValueError("Boundary outside history")
        return History(
            session_id=session_id,
            records=tuple(entry.model_copy(deep=True) for entry in history.records[:end]),
        )


def input_batch(batch_id: str, *record_ids: str) -> QueuedBatch:
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return QueuedBatch(
        id=batch_id, enqueued_at=at,
        records=tuple(prepared_user(record_id, record_id) for record_id in record_ids),
    )


async def opened_write_queue(limit: int) -> tuple[HistoryWriteQueue, FakeHistoryStore]:
    store = FakeHistoryStore()
    await store.create("queue-test")
    queue = HistoryWriteQueue(
        "queue-test", store,
        HistoryWritePolicy(max_pending_messages=limit, retry_delays_ms=(1, 2)),
    )
    await queue.open()
    return queue, store


async def test_full_queue_rejects_entire_new_batch_without_reserving_seq() -> None:
    queue, store = await opened_write_queue(limit=3)
    queue.enqueue(input_batch("first", "u1", "u2"))
    before = queue.snapshot().model_dump_json()
    try:
        queue.enqueue(input_batch("rejected", "u3", "u4"))
    except HistoryQueueFull as exc:
        assert (exc.pending, exc.incoming, exc.limit) == (2, 2, 3)
    else:
        raise AssertionError("The entire new batch must be rejected")
    assert queue.snapshot().model_dump_json() == before
    assert (await store.load("queue-test")).records == ()
    accepted = queue.enqueue(input_batch("fits", "u5"))
    assert "seq" not in accepted.records[0].model_dump()
    assert queue.snapshot().pending_messages == 3
    await queue.drain()
    saved = await store.load("queue-test")
    assert [entry.seq for entry in saved.records] == [1, 2, 3]
    assert [entry.record.id for entry in saved.records] == ["u1", "u2", "u5"]


async def test_failed_head_is_retried_unchanged_before_later_batches() -> None:
    queue, store = await opened_write_queue(limit=3)
    first = queue.enqueue(input_batch("first", "u1", "u2"))
    store.fail_before_commit = True
    assert await queue.flush_once() is False
    failed = queue.snapshot()
    assert failed.durable_seq == 0 and failed.pending_messages == 2
    assert failed.pending[0].attempts == 1 and failed.pending[0].last_error
    assert failed.pending[0].records == first.records
    queue.enqueue(input_batch("later", "u3"))
    store.fail_before_commit = False
    assert await queue.flush_once() is True
    assert store.attempts[0] == store.attempts[1] == first.records
    assert queue.snapshot().pending_messages == 1
    assert await queue.flush_once() is True
    saved = await store.load("queue-test")
    assert [entry.record.id for entry in saved.records] == ["u1", "u2", "u3"]
    assert [entry.seq for entry in saved.records] == [1, 2, 3]
    assert queue.snapshot().pending == ()


async def test_lost_confirmation_replays_batch_without_duplicate_records() -> None:
    queue, store = await opened_write_queue(limit=2)
    queue.enqueue(input_batch("first", "u1", "u2"))
    store.lose_next_ack = True
    assert await queue.flush_once() is False
    assert queue.snapshot().pending_messages == 2
    assert queue.snapshot().durable_seq == 0
    assert len((await store.load("queue-test")).records) == 2
    assert await queue.flush_once() is True
    assert store.attempts[0] == store.attempts[1]
    assert len((await store.load("queue-test")).records) == 2
    assert queue.snapshot().pending == ()
    assert queue.snapshot().durable_seq == 2


async def test_in_flight_batch_still_counts_towards_capacity() -> None:
    queue, store = await opened_write_queue(limit=2)
    queue.enqueue(input_batch("first", "u1", "u2"))
    store.release.clear()
    writing = asyncio.create_task(queue.flush_once())
    try:
        await asyncio.wait_for(store.entered.wait(), timeout=1)
        assert queue.snapshot().in_flight_batch_id == "first"
        assert queue.snapshot().pending_messages == 2
        try:
            queue.enqueue(input_batch("rejected", "u3"))
        except HistoryQueueFull:
            pass
        else:
            raise AssertionError("In-flight messages must still count towards N")
    finally:
        store.release.set()
        await asyncio.wait_for(writing, timeout=1)
    assert queue.snapshot().pending_messages == 0
```

Отдельные проверки реализации: `run()` повторяет попытки без новых сообщений;
новый `enqueue` не сокращает задержку повтора; `run`/`drain`/`flush_once` не пишут
одновременно; отмена сохраняет буфер; `drain` ждёт только принятый к вызову префикс.
Каждый вариант `HistoryRecord`, включая служебные записи, проходит через ту же
очередь и учитывается в N. Для всех производителей проверяется отсутствие прямого
вызова `HistoryStore.append`.

### Курсор обработки и источник данных сборщика

Эти примеры исполняются с `FakeHistoryStore` и `HistoryConsumer` без очереди
и LLM. Они проверяют чтение сохранённых данных и границы порций.

```python
async def test_store_assigns_seq_at_commit_and_replay_returns_same_entries() -> None:
    store = FakeHistoryStore()
    await store.create("session")
    records = input_batch("first", "u1", "u2").records
    store.fail_before_commit = True
    try:
        await store.append("session", records)
    except HistoryWriteError:
        pass
    else:
        raise AssertionError("The write must fail")
    assert (await store.load("session")).records == ()
    store.fail_before_commit = False
    store.lose_next_ack = True
    try:
        await store.append("session", records)
    except HistoryWriteError:
        pass
    else:
        raise AssertionError("The acknowledgement must be lost")
    committed = await store.load("session")
    replayed = await store.append("session", records)
    assert replayed == committed.records
    assert [entry.seq for entry in replayed] == [1, 2]
    assert tuple(entry.record for entry in replayed) == records
    assert await store.load("session") == committed
    assert all("seq" not in record.model_dump() for record in records)


async def test_read_pending_does_not_acknowledge_or_drop_earlier_context() -> None:
    store = FakeHistoryStore()
    prepared = History.model_validate(journal_fixture())
    await store.create(prepared.session_id)
    await store.append(prepared.session_id, tuple(entry.record for entry in prepared.records))
    consumer = HistoryConsumer(prepared.session_id, store, processed_seq=11)
    portion = await consumer.read_pending()
    assert [entry.seq for entry in portion.records] == [12, 13, 14, 15]
    assert consumer.processed_seq == 11
    assert await consumer.read_pending() == portion
    history = await consumer.load_history(portion)
    assert history == prepared  # Параметры, промпты и схемы до seq=12 сохранены во входе.
    assert consumer.processed_seq == 11
    consumer.acknowledge(portion)
    assert consumer.processed_seq == 15
    empty = await consumer.read_pending()
    assert empty.records == () and empty.through_seq == 15


async def test_new_writes_stay_outside_current_portion_and_its_request_history() -> None:
    store = FakeHistoryStore()
    await store.create("session")
    await store.append("session", input_batch("first", "u1").records)
    consumer = HistoryConsumer("session", store, processed_seq=0)
    first = await consumer.read_pending()
    await store.append("session", input_batch("later", "u2").records)
    history = await consumer.load_history(first)
    assert [entry.record.id for entry in history.records] == ["u1"]
    consumer.acknowledge(first)
    assert consumer.processed_seq == 1
    following = await consumer.read_pending()
    assert following.after_seq == 1 and following.through_seq == 2
    assert [entry.record.id for entry in following.records] == ["u2"]


async def test_consumer_advances_between_messages_without_turn_end() -> None:
    store = FakeHistoryStore()
    await store.create("session")
    consumer = HistoryConsumer("session", store, processed_seq=0)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    messages = (
        prepared_user("user", "Вопрос"),
        AssistantMessageRecord(
            id="assistant", created_at=at, request_id="request",
            content=(TextPart(text="Ответ"),),
            llm_payload=LlmPayload(protocol=TEST_PROTOCOL, text="Ответ", data={"role": "assistant"}),
        ),
    )
    for expected_seq, message in enumerate(messages, start=1):
        await store.append("session", (message,))
        portion = await consumer.read_pending()
        assert len(portion.records) == 1
        assert portion.records[0].record == message
        consumer.acknowledge(portion)
        assert consumer.processed_seq == expected_seq
    assert (await consumer.read_pending()).records == ()
    assert all(not isinstance(entry.record, TurnEndRecord) for entry in (await store.load("session")).records)


async def test_stale_acknowledgement_cannot_move_cursor() -> None:
    store = FakeHistoryStore()
    await store.create("session")
    await store.append("session", input_batch("first", "u1").records)
    consumer = HistoryConsumer("session", store, processed_seq=0)
    portion = await consumer.read_pending()
    consumer.acknowledge(portion)
    try:
        consumer.acknowledge(portion)
    except ValueError:
        pass
    else:
        raise AssertionError("An already processed portion must be rejected")
    assert consumer.processed_seq == 1
```

Интеграционные тесты цикла агента должны проверять вызов чтения после каждого
сообщения внутри одного хода, пробуждение свободного цикла после commit,
сохранение курсора при ошибке обработки, отсутствие LLM-запроса на пустую или
чисто служебную порцию и прохождение ответов LLM/tools через общую очередь.
Записи, ожидающие записи в очереди, не должны появляться во входе сборщика.

### Маршрутизация команд

```python
class RecordingCommandExecutor:
    """Исполнитель для теста маршрутизации без клиента, MCP и LLM."""

    def __init__(self, *, wrong_command_id: bool = False) -> None:
        self.received: list[str] = []
        self.wrong_command_id = wrong_command_id

    async def execute(self, command: SlashCommandRecord) -> QueuedBatch:
        self.received.append(command.id)
        return QueuedBatch(
            id=f"batch-{command.id}", enqueued_at=command.created_at,
            records=(SlashCommandResultRecord(
                id=f"result-{command.id}", created_at=command.created_at,
                command_id="different" if self.wrong_command_id else command.id,
                outcome="succeeded", content=(), structured_content=None, error=None,
            ),),
        )


async def test_slash_command_routes_to_recorded_owner() -> None:
    agent, client, prompt, app = (RecordingCommandExecutor() for _ in range(4))
    router = SlashCommandRouter(agent=agent, client=client, mcp_prompt=prompt, mcp_app=app)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cases = (
        ("compact", AgentCommandOwner(), agent),
        ("compact", ClientCommandOwner(), client),  # То же имя, другой владелец.
        ("mcp__db__explain", McpPromptCommandOwner(), prompt),
        ("mcp__db__dashboard", McpAppCommandOwner(
            tool_name="mcp__db__show_dashboard", resource_uri="ui://dashboard",
        ), app),
    )
    for seq, (name, owner, executor) in enumerate(cases, start=1):
        command = SlashCommandRecord(
            id=f"command-{seq}", created_at=at, name=name, text=f"/{name}", arguments={}, owner=owner,
        )
        entry = HistoryEntry(seq=seq, record=command)
        persisted = HistoryEntry.model_validate_json(entry.model_dump_json())
        batch = await router.execute(persisted)
        assert executor.received == [command.id]
        assert len(batch.records) == 1 and batch.records[0].command_id == command.id
        assert all("seq" not in type(record).model_fields for record in batch.records)
    assert [executor.received for executor in (agent, client, prompt, app)] == [
        ["command-1"], ["command-2"], ["command-3"], ["command-4"],
    ]


async def test_router_rejects_result_for_different_command() -> None:
    executor = RecordingCommandExecutor(wrong_command_id=True)
    router = SlashCommandRouter(agent=executor, client=executor, mcp_prompt=executor, mcp_app=executor)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entry = HistoryEntry(seq=1, record=SlashCommandRecord(
        id="command", created_at=at, name="compact", text="/compact", arguments={}, owner=AgentCommandOwner(),
    ))
    try:
        await router.execute(entry)
    except ValueError:
        pass
    else:
        raise AssertionError("A command result must refer to its invocation")
```

Для интеграции команд проверяются: сохранение вызова до исполнения; прохождение
результата через общую очередь; отсутствие повторного исполнения при повторе записи;
отсутствие ContextStartRecord при неудачной компактизации; атомарное сохранение
маркера и пересказа при успешной. Сборщик не должен вызывать исполнителей команд.

## 8 Порядок реализации

Сначала реализуются модели истории, `ProviderRequestBuilder` для каждого протокола
и его тесты с готовой сериализованной историей. Они не требуют очереди, MCP,
рендереров, настроек процесса или сетевого клиента. На этом этапе допустимо,
что существующие производители ещё не создают подходящие записи.

Затем производители подготавливают исходные данные и `llm_payload`, подключаются
`HistoryWriteQueue`, хранилище, `HistoryConsumer` и `SlashCommandRouter`.
Для производителей проверяется однократная подготовка payload; для очереди —
передача его без изменений при каждой попытке. Сборщик не получает обходных
источников данных ради подключения этих компонентов.
