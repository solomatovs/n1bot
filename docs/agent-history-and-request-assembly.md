# История агента и сборка LLM-запроса

Каждая модель сообщения соответствует одной строке `records`; рядом показан её JSON.
`Payload` описывает JSON-столбец. Общие модели значений показаны как вложенные фрагменты.
Примеры независимы; `session_id` и `seq` в них уже назначены хранилищем.

## 1 Общие значения

```python
from __future__ import annotations

from datetime import date as CalendarDate
from typing import (
    Annotated,
    Literal,
    Protocol,
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
```

```python
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
```

JSON базового класса; самостоятельной записью журнала не является:

```json
{}
```

Категории и общие поля строк; конкретные модели ниже уточняют `kind` и `payload`.

```python
HistoryCategory = Literal[
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
```

```python
class TextPart(DataModel):
    """Текст сохраняется буквально, включая пустую строку."""

    kind: Literal["text"] = "text"
    text: str
```

```json
{
  "kind": "text",
  "text": "Привет"
}
```

```python
class ImagePart(DataModel):
    """Изображение уже загружено; в JSON байты представлены base64."""

    kind: Literal["image"] = "image"
    media_type: NonEmptyStr
    data: bytes
```

```json
{
  "kind": "image",
  "media_type": "image/png",
  "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk-A8AAQUBAScY42YAAAAASUVORK5CYII="
}
```

```python
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
        if self.line_start is not None and self.line_end < self.line_start:
            raise ValueError("line_end precedes line_start")
        if self.truncated and self.content is None:
            raise ValueError("Only loaded content can be truncated")
        return self
```

```json
{
  "kind": "resource",
  "name": "app.py",
  "uri": "file:///workspace/boba/app.py",
  "media_type": "text/x-python",
  "content": [
    {
      "kind": "text",
      "text": "def greet():\n    return \"Привет\"\n"
    }
  ],
  "line_start": 10,
  "line_end": 11,
  "truncated": false
}
```

```python
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
```

```json
{
  "kind": "reasoning",
  "text": "Нужно получить список таблиц.",
  "signature": null,
  "format": null
}
```

```python
class ToolCallPart(DataModel):
    """Вызов в сообщении LLM; call_id связывает его с ToolResultRecord."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: NonEmptyStr
    name: NonEmptyStr
    arguments: JsonObject
```

```json
{
  "kind": "tool_call",
  "call_id": "call-1",
  "name": "mcp__database__list_tables",
  "arguments": {}
}
```

```python
class ToolReferencePart(DataModel):
    """Ссылка на найденный инструмент и использованную запись каталога."""

    kind: Literal["tool_reference"] = "tool_reference"
    name: NonEmptyStr
    catalog_revision: NonEmptyStr
```

```json
{
  "kind": "tool_reference",
  "name": "mcp__database__list_tables",
  "catalog_revision": "tool-add-1"
}
```

```python
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
```

```python
class ToolCatalogItem(DataModel):
    """Имя и описание доступного инструмента без полной схемы."""

    description: str
```

```json
{
  "description": "Получить список таблиц."
}
```

```python
class ToolDefinition(DataModel):
    """Полное определение инструмента с уникальным именем в каталоге агента."""

    description: str
    parameters: JsonObject
    deferred: bool
```

```json
{
  "description": "Получить список таблиц.",
  "parameters": {
    "type": "object",
    "properties": {},
    "additionalProperties": false
  },
  "deferred": false
}
```

```python
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
```

```json
{
  "name": "review",
  "description": "Проверка изменений проекта.",
  "source_uri": "file:///workspace/skills/review/SKILL.md",
  "instructions": null
}
```

```python
class AgentCatalogItem(DataModel):
    """Описание доступного типа агента; конфигурация запуска фиксируется в дочерней истории."""

    name: NonEmptyStr
    description: str
    source_uri: NonEmptyStr | None = None
```

```json
{
  "name": "explore",
  "description": "Исследование проекта без изменения файлов.",
  "source_uri": "file:///workspace/agents/explore.md"
}
```

```python
TaskExecutionMode = Literal["foreground", "background"]
TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TaskTerminalStatus = Literal["completed", "failed", "cancelled"]
```

`ToolCallPart` един для foreground/background. Сабагент вызывается инструментом `agent`.
Состояния задачи ссылаются на сохранённый вызов по `related_message_id` и `call_id`;
имя и аргументы остаются в исходном сообщении.

```python
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
```

```json
{
  "execution_mode": "background",
  "description": "Проверка проекта",
  "status": "running",
  "execution_id": "workspace-job-42",
  "child_session_id": null,
  "output_uri": "file:///workspace/tasks/tests-1.log",
  "result": null,
  "error": null
}
```

```python
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
```

```json
{
  "working_directory": "/workspace/boba/packages",
  "workspace_roots": [
    "/workspace/boba"
  ],
  "platform": "linux",
  "os_version": "Ubuntu 24.04",
  "shell": "/bin/bash",
  "temporary_directory": "/workspace/boba/.tmp"
}
```

## 2 Проприетарная история агента

Каждое полное сообщение сначала сохраняется через `HistoryStore.append` с `record_id`
и `seq`, затем обрабатывается агентом. Сборщик читает исключительно персистентную историю.
Чанки потокового показа не являются полными сообщениями и не записываются отдельно.

`kind` выбирает модель; `category` — правило сборки. `key` — свободный непустой ключ
состояния; у остальных записей он равен `null`. LAST заменяет запись целиком по
`(category, key)`, даже между разными `kind`. Префиксы ключей — соглашения производителей.

В `setting`, `prompt`, `context` значение `request_id=null` означает постоянное состояние;
идентификатор — версию для одного запроса с приоритетом над постоянной. В остальных
категориях это ссылка происхождения. `operation=remove` явно отменяет выбранное состояние.

Производитель сохраняет подготовленный текст, структурные данные и источник (`file:` URI
или URL). Клиент собирает своё окружение; ресурс получают через клиент, MCP или HTTP.
Сборщик не читает файлы, URI, часы или настройки процесса и не рендерит текст повторно.

```python
class RecordBase(DataModel):
    """Одно сообщение = одна строка records; session_id и seq назначает хранилище."""

    class Payload(DataModel):
        role: Literal["system", "user", "assistant", "tool"] | None = None

    session_id: NonEmptyStr | None = None
    seq: PositiveInt | None = None
    record_id: NonEmptyStr
    created_at: AwareDatetime
    version: NonEmptyStr  # Версия boba-cli, создавшего сообщение.
    kind: NonEmptyStr
    category: HistoryCategory = "operational"
    key: NonEmptyStr | None = None
    operation: Literal["set", "remove"] = "set"
    request_id: NonEmptyStr | None = None
    payload: Payload

    @model_validator(mode="after")
    def address_is_complete(self) -> Self:
        if (self.session_id is None) != (self.seq is None):
            raise ValueError("session_id and seq must be assigned together")
        return self
```

JSON базового класса; самостоятельной записью журнала не является:

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "record-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "record_example",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

`payload.content` — упорядоченный массив типизированных частей сообщения.
`created_at` и `version` принадлежат сообщению; части не дублируют его метаданные.
JSONB сохраняет порядок элементов массивов; отдельный position в частях не требуется.

```python
class SystemPromptAddedRecord(RecordBase):
    """Инструкция указанного типа: постоянная либо только для request_id."""

    class Payload(RecordBase.Payload):
        role: Literal["system"] = "system"
        source_uri: NonEmptyStr | None = (
            None  # Источник инструкции; LLM-сборщик не читает его.
        )
        content: NonEmptyTuple[TextPart]

    kind: Literal["system_prompt_added"] = "system_prompt_added"
    category: Literal["prompt"] = "prompt"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr  # Свободный ключ, например agent.role или project.rules.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "prompt-add-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "system_prompt_added",
  "category": "prompt",
  "key": "agent.role",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "system",
    "source_uri": null,
    "content": [
      {
        "kind": "text",
        "text": "Отвечай кратко."
      }
    ]
  }
}
```

```python
class SystemPromptRemovedRecord(RecordBase):
    """Удалить промпт постоянно либо исключить его только из request_id."""

    class Payload(RecordBase.Payload):
        role: Literal["system"] = "system"

    kind: Literal["system_prompt_removed"] = "system_prompt_removed"
    category: Literal["prompt"] = "prompt"
    operation: Literal["remove"] = "remove"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "prompt-remove-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "system_prompt_removed",
  "category": "prompt",
  "key": "agent.role",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": "system"
  }
}
```

AGENTS.md, инструкции MCP, язык и стиль ответа используют эти модели. `key` обозначает
заменяемую секцию, `source_uri` — источник. Назначение инструкции задаёт производитель.

```python
class SkillAddedRecord(RecordBase):
    """Полный снимок одного доступного навыка и готовое объявление для модели."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        skill: SkillDefinition
        content: NonEmptyTuple[TextPart]

    kind: Literal["skill_added"] = "skill_added"
    category: Literal["context"] = "context"
    operation: Literal["set"] = "set"
    request_id: None = None
    key: NonEmptyStr  # Устойчивая идентичность навыка, например skill:workspace:review.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-add-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "skill_added",
  "category": "context",
  "key": "skill:workspace:review",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "skill": {
      "name": "review",
      "description": "Проверка изменений проекта.",
      "source_uri": "file:///workspace/skills/review/SKILL.md",
      "instructions": null
    },
    "content": [
      {
        "kind": "text",
        "text": "Доступен навык review: проверка изменений проекта. Источник: file:///workspace/skills/review/SKILL.md."
      }
    ]
  }
}
```

```python
class SkillRemovedRecord(RecordBase):
    """Снять доступность навыка; ранее активированные инструкции сохраняются."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"

    kind: Literal["skill_removed"] = "skill_removed"
    category: Literal["context"] = "context"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-remove-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "skill_removed",
  "category": "context",
  "key": "skill:workspace:review",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": "user"
  }
}
```

```python
class SkillActivatedRecord(RecordBase):
    """Полученные инструкции навыка с уже подставленными аргументами вызова."""

    class Payload(RecordBase.Payload):
        role: Literal["system"] = "system"
        name: NonEmptyStr
        source_uri: NonEmptyStr | None = None
        content: NonEmptyTuple[TextPart]

    kind: Literal["skill_activated"] = "skill_activated"
    category: Literal["prompt"] = "prompt"
    operation: Literal["set"] = "set"
    key: NonEmptyStr  # Та же идентичность, что в каталоге, но категория prompt.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-activate-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "skill_activated",
  "category": "prompt",
  "key": "skill:workspace:review",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "system",
    "name": "review",
    "source_uri": "file:///workspace/skills/review/SKILL.md",
    "content": [
      {
        "kind": "text",
        "text": "Навык review. Проверяй изменения на ошибки. Указывай файл и строку для каждого замечания."
      }
    ]
  }
}
```

`SkillAddedRecord` объявляет навык; `SkillActivatedRecord` сохраняет его полные инструкции.
Активацию инициирует пользователь или модель. Инструкции получают из сохранённого текста
либо источника и записывают до использования в LLM. При вызове через tool результат
и активация сохраняются одной пачкой. Снятие объявления не снимает активированную инструкцию:
для неё используется `SystemPromptRemovedRecord` с ключом активации.

```python
class MemoryAddedRecord(RecordBase):
    """Полный снимок выбранной памяти, действующий до замены или отмены."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        source_uri: NonEmptyStr | None = None
        content: NonEmptyTuple[TextPart]

    kind: Literal["memory_added"] = "memory_added"
    category: Literal["context"] = "context"
    operation: Literal["set"] = "set"
    key: NonEmptyStr  # Устойчивая идентичность фрагмента памяти, например memory:workspace:project.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "memory-add-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "memory_added",
  "category": "context",
  "key": "memory:workspace:project",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "source_uri": "https://example.org/workspaces/boba/memory/project",
    "content": [
      {
        "kind": "text",
        "text": "В проекте используется PostgreSQL 17."
      }
    ]
  }
}
```

```python
class MemoryRemovedRecord(RecordBase):
    """Прекратить включение выбранной памяти в запросы; исходные записи остаются."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"

    kind: Literal["memory_removed"] = "memory_removed"
    category: Literal["context"] = "context"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "memory-remove-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "memory_removed",
  "category": "context",
  "key": "memory:workspace:project",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": "user"
  }
}
```

Память выбирает клиент или производитель памяти; add сохраняет полный текст выбранного
контекста, remove снимает его. Источник может быть файлом, URL или внутренним ресурсом.
Прочитанный файл не становится памятью автоматически. Разовый материал относится к
`ResourceMessageRecord`, фактический результат tool — к `ToolResultRecord`.
Постоянная память переносится в снимок компактизации.

```python
class AgentAddedRecord(RecordBase):
    """Полный снимок одного доступного типа агента и готовое объявление для модели."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        agent: AgentCatalogItem
        content: NonEmptyTuple[TextPart]

    kind: Literal["agent_added"] = "agent_added"
    category: Literal["context"] = "context"
    operation: Literal["set"] = "set"
    request_id: None = None
    key: NonEmptyStr  # Например agent:explore; не идентификатор запущенной задачи.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "agent-add-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "agent_added",
  "category": "context",
  "key": "agent:explore",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "agent": {
      "name": "explore",
      "description": "Исследование проекта без изменения файлов.",
      "source_uri": "file:///workspace/agents/explore.md"
    },
    "content": [
      {
        "kind": "text",
        "text": "Доступен агент explore: исследование проекта без изменения файлов."
      }
    ]
  }
}
```

```python
class AgentRemovedRecord(RecordBase):
    """Снять тип агента из каталога; уже запущенные задачи продолжают работу."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"

    kind: Literal["agent_removed"] = "agent_removed"
    category: Literal["context"] = "context"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "agent-remove-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "agent_removed",
  "category": "context",
  "key": "agent:explore",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": "user"
  }
}
```

Каталог агентов — LAST по `(context, key)`. Параметры, инструменты, инструкции и разрешения
запуска сохраняются отдельными записями дочерней сессии.

```python
class EnvironmentRecord(RecordBase):
    """Полный снимок одной среды; state=None явно снимает её объявление."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        state: EnvironmentState | None
        content: tuple[TextPart, ...] = ()

    kind: Literal["environment"] = "environment"
    category: Literal["context"] = "context"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "environment-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "environment",
  "category": "context",
  "key": "environment.executor.shell",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "state": {
      "working_directory": "/workspace/boba/packages",
      "workspace_roots": [
        "/workspace/boba"
      ],
      "platform": "linux",
      "os_version": "Ubuntu 24.04",
      "shell": "/bin/bash",
      "temporary_directory": "/workspace/boba/.tmp"
    },
    "content": [
      {
        "kind": "text",
        "text": "Среда executor.shell: Linux, Ubuntu 24.04. Оболочка: /bin/bash. Текущий каталог: /workspace/boba/packages. Корень проекта: /workspace/boba. Временные файлы: /workspace/boba/.tmp."
      }
    ]
  }
}
```

`working_directory` — cwd; `workspace_roots` — корни открытых проектов, при неизвестных
корнях список пуст. Пути не задают разрешений или cwd процесса сервера.
Ключ среды свободный; снимок и подготовленное описание всегда полные.

```python
class DateRecord(RecordBase):
    """Наблюдённая календарная дата в выбранной зоне; отличается от времени записи."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        date: CalendarDate
        timezone: NonEmptyStr
        content: NonEmptyTuple[TextPart]

    kind: Literal["date"] = "date"
    category: Literal["context"] = "context"
    key: NonEmptyStr = "calendar.current"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr | None = None
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "date-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "date",
  "category": "context",
  "key": "calendar.current",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "date": "2026-11-13",
    "timezone": "Europe/Moscow",
    "content": [
      {
        "kind": "text",
        "text": "Текущая дата: 2026-11-13. Часовой пояс: Europe/Moscow."
      }
    ]
  }
}
```

Дату и часовой пояс фиксирует производитель при изменении. `calendar.current` — ключ
по умолчанию; разрешены другие ключи. SQL использует сохранённую дату, а не текущие часы.

```python
class ToolAddedRecord(RecordBase):
    """Инструмент доступен агенту по полному имени."""

    class Payload(RecordBase.Payload):
        tool: ToolCatalogItem

    kind: Literal["tool_added"] = "tool_added"
    category: Literal["tool_catalog"] = "tool_catalog"
    operation: Literal["set"] = "set"
    key: NonEmptyStr  # Полное имя инструмента.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool-add-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "tool_added",
  "category": "tool_catalog",
  "key": "mcp__database__list_tables",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "tool": {
      "description": "Получить список таблиц."
    }
  }
}
```

```python
class ToolRemovedRecord(RecordBase):
    """Инструмент стал недоступен агенту."""

    kind: Literal["tool_removed"] = "tool_removed"
    category: Literal["tool_catalog"] = "tool_catalog"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool-remove-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "tool_removed",
  "category": "tool_catalog",
  "key": "mcp__database__list_tables",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

```python
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
    key: NonEmptyStr  # Полное имя инструмента.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool-schema-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "tool_schema",
  "category": "tool_schema",
  "key": "mcp__database__list_tables",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "catalog_revision": "tool-add-1",
    "tool": {
      "description": "Получить список таблиц.",
      "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": false
      },
      "deferred": false
    }
  }
}
```

```python
class ParameterRecord(RecordBase):
    """Атомарный набор готовых полей запроса; ключ задаёт логическую категорию."""

    class Payload(RecordBase.Payload):
        value: Annotated[JsonObject, Field(min_length=1)]

    category: Literal["setting"] = "setting"
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
```

JSON базового класса; самостоятельной записью журнала не является:

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "parameters-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "parameter_example",
  "category": "setting",
  "key": "sampling",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "value": {
      "temperature": 0.2
    }
  }
}
```

`value` — непустой атомарный набор полей выбранного инференса. Производитель проверяет
их допустимость; SQL переносит буквально. LAST не наследует поля предыдущего набора.
Отмена — отдельная remove-запись без `value`; `{}` запрещён. Вложенные `{}`, `null`, `false`
и `0` остаются обычными значениями параметров. Конфликт выходных полей — `duplicate_parameter`.
Выбор модели заменяется новым `ModelRecord`.

```python
class ParameterRemovedRecord(RecordBase):
    """Отмена целого набора параметров; payload содержит только метаданные записи."""

    category: Literal["setting"] = "setting"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

JSON базового класса; самостоятельной записью журнала не является:

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "parameter_removed_example-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "parameter_removed_example",
  "category": "setting",
  "key": "sampling",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

```python
class ModelRecord(ParameterRecord):
    """Выбранный инференс и готовые поля выбора модели."""

    class Payload(ParameterRecord.Payload):
        provider: NonEmptyStr  # Маршрутизация агентом; не поле тела запроса.

    kind: Literal["model"] = "model"
    key: Literal["model"] = "model"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "model-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "model",
  "category": "setting",
  "key": "model",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "value": {
      "model": "agent-model"
    },
    "provider": "openai"
  }
}
```

```python
class SamplingRecord(ParameterRecord):
    """Параметры выборки в форме выбранного инференса."""

    kind: Literal["sampling"] = "sampling"
    key: Literal["sampling"] = "sampling"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "sampling-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "sampling",
  "category": "setting",
  "key": "sampling",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "value": {
      "temperature": 0.2,
      "top_p": 0.9
    }
  }
}
```

```python
class SamplingRemovedRecord(ParameterRemovedRecord):
    """Параметры выборки: отмена постоянно либо для request_id."""

    kind: Literal["sampling_removed"] = "sampling_removed"
    key: Literal["sampling"] = "sampling"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "sampling_removed-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "sampling_removed",
  "category": "setting",
  "key": "sampling",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

```python
class InferenceRecord(ParameterRecord):
    """Лимиты вывода и параметры reasoning в форме выбранного инференса."""

    kind: Literal["inference"] = "inference"
    key: Literal["inference"] = "inference"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "inference-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "inference",
  "category": "setting",
  "key": "inference",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "value": {
      "max_completion_tokens": 1024,
      "reasoning_effort": "medium"
    }
  }
}
```

```python
class InferenceRemovedRecord(ParameterRemovedRecord):
    """Параметры инференса: отмена постоянно либо для request_id."""

    kind: Literal["inference_removed"] = "inference_removed"
    key: Literal["inference"] = "inference"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "inference_removed-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "inference_removed",
  "category": "setting",
  "key": "inference",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

```python
class ProviderParametersRecord(ParameterRecord):
    """Прочие поля запроса выбранного инференса."""

    kind: Literal["provider_parameters"] = "provider_parameters"
    key: Literal["provider_parameters"] = "provider_parameters"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "provider_parameters-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "provider_parameters",
  "category": "setting",
  "key": "provider_parameters",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "value": {
      "parallel_tool_calls": true
    }
  }
}
```

```python
class ProviderParametersRemovedRecord(ParameterRemovedRecord):
    """Параметры провайдера: отмена постоянно либо для request_id."""

    kind: Literal["provider_parameters_removed"] = "provider_parameters_removed"
    key: Literal["provider_parameters"] = "provider_parameters"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "provider_parameters_removed-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "provider_parameters_removed",
  "category": "setting",
  "key": "provider_parameters",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

```python
class ResponseFormatRecord(ParameterRecord):
    """Формат генерируемого ответа одного запроса."""

    kind: Literal["response_format"] = "response_format"
    key: Literal["response_format"] = "response_format"
    request_id: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "response_format-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "response_format",
  "category": "setting",
  "key": "response_format",
  "operation": "set",
  "request_id": "request-1",
  "payload": {
    "role": null,
    "value": {
      "response_format": {
        "type": "json_schema",
        "json_schema": {
          "name": "answer",
          "schema": {
            "type": "object",
            "properties": {
              "answer": {
                "type": "string"
              }
            },
            "required": [
              "answer"
            ],
            "additionalProperties": false
          },
          "strict": true
        }
      }
    }
  }
}
```

```python
class ResponseFormatRemovedRecord(ParameterRemovedRecord):
    """Формат ответа: отмена для одного запроса."""

    kind: Literal["response_format_removed"] = "response_format_removed"
    key: Literal["response_format"] = "response_format"
    request_id: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "response_format_removed-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "response_format_removed",
  "category": "setting",
  "key": "response_format",
  "operation": "remove",
  "request_id": "request-1",
  "payload": {
    "role": null
  }
}
```

```python
class ToolChoiceRecord(ParameterRecord):
    """Политика выбора tools для одного запроса в форме инференса."""

    kind: Literal["tool_choice"] = "tool_choice"
    key: Literal["tool_choice"] = "tool_choice"
    request_id: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool_choice-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "tool_choice",
  "category": "setting",
  "key": "tool_choice",
  "operation": "set",
  "request_id": "request-1",
  "payload": {
    "role": null,
    "value": {
      "tool_choice": "auto"
    }
  }
}
```

```python
class ToolChoiceRemovedRecord(ParameterRemovedRecord):
    """Выбор tools: отмена для одного запроса."""

    kind: Literal["tool_choice_removed"] = "tool_choice_removed"
    key: Literal["tool_choice"] = "tool_choice"
    request_id: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool_choice_removed-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "tool_choice_removed",
  "category": "setting",
  "key": "tool_choice",
  "operation": "remove",
  "request_id": "request-1",
  "payload": {
    "role": null
  }
}
```

```python
class StreamingRecord(ParameterRecord):
    """Потоковая выдача одного запроса в форме инференса."""

    kind: Literal["streaming"] = "streaming"
    key: Literal["streaming"] = "streaming"
    request_id: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "streaming-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "streaming",
  "category": "setting",
  "key": "streaming",
  "operation": "set",
  "request_id": "request-1",
  "payload": {
    "role": null,
    "value": {
      "stream": true
    }
  }
}
```

```python
class StreamingRemovedRecord(ParameterRemovedRecord):
    """Потоковая выдача: отмена для одного запроса."""

    kind: Literal["streaming_removed"] = "streaming_removed"
    key: Literal["streaming"] = "streaming"
    request_id: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "streaming_removed-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "streaming_removed",
  "category": "setting",
  "key": "streaming",
  "operation": "remove",
  "request_id": "request-1",
  "payload": {
    "role": null
  }
}
```

Для разовых настроек и их удаления `request_id` обязателен. Remove постоянного набора
снимает его до следующего set; remove с `request_id` исключает набор только из этого запроса.
`ModelRecord.payload.provider` выбирает подключение и адаптер, в тело запроса не попадает.
При смене инференса производитель обновляет затронутые параметры до границы запроса.

```python
class LlmRequestBoundaryRecord(RecordBase):
    """Граница входной истории одного логического LLM-запроса.

    record_id связывает параметры, попытки и ответ; вход — записи перед границей.
    Фиксация границы предшествует сборке и отправке. Повтор использует тот же вход.
    """

    kind: Literal["llm_request_boundary"] = "llm_request_boundary"
    category: Literal["request"] = "request"
    operation: Literal["set"] = "set"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "request-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "llm_request_boundary",
  "category": "request",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null
  }
}
```

`record_id` границы — идентификатор запроса. Все его входные записи сохраняются раньше;
начало диапазона — последняя `CompactMessageRecord`, конец — `seq` границы исключительно.

```python
class UserMessageRecord(RecordBase):
    """Сообщение пользователя в журнале истории."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        content: NonEmptyTuple[UserPart]

    kind: Literal["user_message"] = "user_message"
    category: Literal["message"] = "message"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "user-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "user_message",
  "category": "message",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "content": [
      {
        "kind": "text",
        "text": "Привет"
      }
    ]
  }
}
```

```python
class ResourceMessageRecord(RecordBase):
    """Материалы, добавленные клиентом или агентом: выбранный фрагмент, файл, ресурс.

    related_message_id связывает материал с предыдущей записью, не ограничивая его одним запросом.
    """

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        source: (
            NonEmptyStr  # Например client.editor, resource.prefetch или file.watcher.
        )
        related_message_id: NonEmptyStr | None = None
        content: NonEmptyTuple[
            Annotated[TextPart | ResourcePart, Field(discriminator="kind")]
        ]

    kind: Literal["resource_message"] = "resource_message"
    category: Literal["message"] = "message"
    operation: Literal["set"] = "set"
    payload: Payload

    @model_validator(mode="after")
    def has_resource(self) -> Self:
        if not any(isinstance(part, ResourcePart) for part in self.payload.content):
            raise ValueError("A resource message must include a resource")
        return self
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "resource-message-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "resource_message",
  "category": "message",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "source": "client.editor",
    "related_message_id": "user-1",
    "content": [
      {
        "kind": "text",
        "text": "К сообщению приложено выделение app.py, строки 10–11:\n"
      },
      {
        "kind": "resource",
        "name": "app.py",
        "uri": "file:///workspace/boba/app.py",
        "media_type": "text/x-python",
        "content": [
          {
            "kind": "text",
            "text": "def greet():\n    return \"Привет\"\n"
          }
        ],
        "line_start": 10,
        "line_end": 11,
        "truncated": false
      }
    ]
  }
}
```

`ResourcePart` одинаков для user, tool result и самостоятельного материала.
`ResourcePart.content=null` означает незагруженную ссылку; `[]` — загруженный пустой ресурс.
Непустой массив содержит сохранённые текстовые блоки и изображения. Незагруженная ссылка не выдаётся за прочитанный ресурс.
`related_message_id` связывает материал с предыдущей записью, не ограничивая одним запросом.

```python
class CompactMessageRecord(RecordBase):
    """Результат успешной компактизации и первое сообщение нового контекста.

    reason — причина сжатия, например manual или context_limit.
    """

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        reason: NonEmptyStr
        content: NonEmptyTuple[TextPart]

    kind: Literal["compact_message"] = "compact_message"
    category: Literal["compact"] = "compact"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "compact-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "compact_message",
  "category": "compact",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "reason": "context_limit",
    "content": [
      {
        "kind": "text",
        "text": "Обсудили структуру базы данных."
      }
    ]
  }
}
```

Компактизация атомарно сохраняет: пересказ → действующие постоянные setting/prompt/context →
каталог tools и сохраняемые схемы → выбранный завершённый хвост диалога.
Разовые настройки и снятые состояния не копируются; пропущенная схема не восстанавливается.

Копии получают новые `record_id`, время и версию создавшего их агента; содержимое сохраняется
буквально. Ссылки схем на каталог и ссылки на копируемые сообщения переводятся на копии;
остальные ссылки и исходный `request_id` ответов сохраняются. Старые записи остаются в журнале.

`expected_seq` защищает снимок от конкурентной записи. Незакрытые пары tools и разовые
настройки без границы запроса — ошибка подготовки. Команда `/compact` и ошибка сжатия
не меняют активную границу; сборщик не достраивает неполный снимок.

```python
class AssistantMessageRecord(RecordBase):
    """Итог одного сообщения LLM: текст, reasoning и/или вызовы tools в исходном порядке.

    request_id указывает источник ответа; сообщение остаётся в контексте следующих запросов.
    completion отличает полный ответ от текста, сохранённого при отмене или лимите.
    """

    class Payload(RecordBase.Payload):
        role: Literal["assistant"] = "assistant"
        content: NonEmptyTuple[AssistantPart]
        completion: Literal["complete", "cancelled", "output_limit"] = "complete"

    kind: Literal["assistant_message"] = "assistant_message"
    category: Literal["message"] = "message"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr
    payload: Payload

    @model_validator(mode="after")
    def completed_tool_calls(self) -> Self:
        if self.payload.completion != "complete" and any(
            isinstance(part, ToolCallPart) for part in self.payload.content
        ):
            raise ValueError("an incomplete message cannot contain tool calls")
        return self
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "assistant-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "assistant_message",
  "category": "message",
  "key": null,
  "operation": "set",
  "request_id": "request-1",
  "payload": {
    "role": "assistant",
    "content": [
      {
        "kind": "text",
        "text": "Проверю доступные таблицы."
      },
      {
        "kind": "tool_call",
        "call_id": "call-1",
        "name": "mcp__database__list_tables",
        "arguments": {}
      }
    ],
    "completion": "complete"
  }
}
```

Полное сообщение LLM сохраняется до исполнения его tools. Чанки текста/reasoning передаются
подписчикам временно; после завершения одного сообщения сохраняется весь накопленный результат.
`record_id` выделяется до первого чанка и связывает показ с записью; новая попытка получает новый id.
При отмене/лимите сохраняется полученный текст без незавершённых calls; пустой буфер не пишется.
Ошибка попытки фиксируется в `LlmRequestAttemptRecord`, её частичный ответ не сохраняется.
`boba-aid` получает историю и чанки только от агента через ACP.

```python
class ToolResultRecord(RecordBase):
    """Один окончательный результат; отказ и отмена имеют is_error=True."""

    class Payload(RecordBase.Payload):
        role: Literal["tool"] = "tool"
        call_id: NonEmptyStr
        content: tuple[ToolResultPart, ...] = ()
        is_error: bool

    kind: Literal["tool_result"] = "tool_result"
    category: Literal["message"] = "message"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool-result-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "tool_result",
  "category": "message",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "tool",
    "call_id": "call-1",
    "content": [
      {
        "kind": "text",
        "text": "users, orders"
      }
    ],
    "is_error": false
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "elicit-form-1",
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "elicitation_request",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "source": "agent.preferences",
    "message": "Какие проверки запускать?",
    "mode": "form",
    "related_message_id": null,
    "call_id": null,
    "requested_schema": {
      "type": "object",
      "properties": {
        "checks": {
          "type": "array",
          "items": {
            "type": "string",
            "enum": [
              "lint",
              "tests"
            ]
          },
          "minItems": 1
        }
      },
      "required": [
        "checks"
      ]
    },
    "url": null
  }
}
```

Тот же тип для внешнего действия:

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "elicit-url-1",
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "elicitation_request",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "source": "agent.credentials.github",
    "message": "Подключите доступ к GitHub.",
    "mode": "url",
    "related_message_id": null,
    "call_id": null,
    "requested_schema": null,
    "url": "https://agent.example/connect/github"
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 2,
  "record_id": "elicit-response-1",
  "created_at": "2026-11-14T09:01:00Z",
  "version": "0.0.33.dev4",
  "kind": "elicitation_response",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "elicitation_id": "elicit-form-1",
    "action": "accept",
    "content": {
      "checks": [
        "lint",
        "tests"
      ]
    }
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 3,
  "record_id": "elicit-completed-1",
  "created_at": "2026-11-14T09:02:00Z",
  "version": "0.0.33.dev4",
  "kind": "elicitation_completed",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "elicitation_id": "elicit-url-1",
    "outcome": "completed",
    "error": null
  }
}
```

Все три записи — `operational`. Ответ для LLM производитель отдельно сохраняет обычным
`ToolResultRecord` или `UserMessageRecord`. Ошибка доставки — `NoticeRecord`, не отказ пользователя.
Формы содержат несекретные данные; credentials обрабатываются внешним URL-процессом.
Восстановление истории само по себе не отправляет вопрос повторно и не запускает действие.

```python
class AgentCommandOwner(DataModel):
    """Команду объявляет и исполняет агент; например /compact."""

    kind: Literal["agent"] = "agent"
```

```json
{
  "kind": "agent"
}
```

```python
class ClientCommandOwner(DataModel):
    """Команду объявляет клиент и исполняет его обработчик."""

    kind: Literal["client"] = "client"
```

```json
{
  "kind": "client"
}
```

```python
class McpPromptCommandOwner(DataModel):
    """MCP-промпт; имя команды mcp__<server>__<prompt> определяет адрес prompts/get."""

    kind: Literal["mcp_prompt"] = "mcp_prompt"
```

```json
{
  "kind": "mcp_prompt"
}
```

```python
class McpAppCommandOwner(DataModel):
    """Команда MCP App; объявивший её MCP задаёт инструмент и ресурс интерфейса."""

    kind: Literal["mcp_app"] = "mcp_app"
    tool_name: NonEmptyStr  # Полное имя mcp__<server>__<tool>.
    resource_uri: NonEmptyStr
```

```json
{
  "kind": "mcp_app",
  "tool_name": "mcp__database__show_dashboard",
  "resource_uri": "ui://database/dashboard"
}
```

```python
SlashCommandOwner = Annotated[
    AgentCommandOwner | ClientCommandOwner | McpPromptCommandOwner | McpAppCommandOwner,
    Field(discriminator="kind"),
]
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "command-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "slash_command",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "name": "compact",
    "text": "/compact",
    "arguments": {},
    "owner": {
      "kind": "agent"
    }
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "command-result-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "slash_command_result",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "command_id": "command-1",
    "outcome": "succeeded",
    "content": [
      {
        "kind": "text",
        "text": "Контекст сжат."
      }
    ],
    "structured_content": null,
    "error": null
  }
}
```

```python
class PermissionRule(DataModel):
    """Правило сессии для одного инструмента, указанного полным именем."""

    tool: NonEmptyStr
    decision: Literal["allow", "deny", "ask"]
```

```json
{
  "tool": "mcp__database__list_tables",
  "decision": "allow"
}
```

```python
class PermissionStateRecord(RecordBase):
    """Полный снимок режима и правил разрешений текущей сессии; действует последний."""

    class Payload(RecordBase.Payload):
        mode: Literal[
            "default",
            "accept_edits",
            "plan",
            "dont_ask",
            "bypass_permissions",
            "auto",
        ]
        rules: tuple[PermissionRule, ...]

    kind: Literal["permission_state"] = "permission_state"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "permission-state-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "permission_state",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "mode": "default",
    "rules": [
      {
        "tool": "mcp__database__list_tables",
        "decision": "allow"
      }
    ]
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "permission-decision-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "permission_decision",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "call_id": "call-1",
    "decision": "allow",
    "reason": "Инструмент разрешён правилами агента."
  }
}
```

Исполнитель использует последнюю `PermissionStateRecord`; `deny` запрещает инструмент,
не удаляя его из каталога. Решение сохраняется в `PermissionDecisionRecord`, отказ —
в обычном `ToolResultRecord`. Эти правила действуют только в своей сессии.

```python
class NoticeRecord(RecordBase):
    """Общие поля уведомлений агента; в историю входят только конкретные наследники.

    payload.level — важность события; payload.content — подготовленное пояснение.
    """

    class Payload(RecordBase.Payload):
        level: Literal["info", "warning", "error"]
        content: NonEmptyTuple[TextPart]

    payload: Payload
```

JSON базового класса; самостоятельной записью журнала не является:

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "notice-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "notice",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "level": "info",
    "content": [
      {
        "kind": "text",
        "text": "Соединение восстановлено."
      }
    ]
  }
}
```

```python
class TextNoticeRecord(NoticeRecord):
    """Самостоятельное уведомление без дополнительных структурированных данных."""

    kind: Literal["notice"] = "notice"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "notice-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "notice",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "level": "info",
    "content": [
      {
        "kind": "text",
        "text": "Соединение восстановлено."
      }
    ]
  }
}
```

```python
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
    request_id: NonEmptyStr
    payload: Payload

    @model_validator(mode="after")
    def attempt_has_outcome(self) -> Self:
        if self.payload.status_code is None and self.payload.error is None:
            raise ValueError("an attempt must have a response or an error")
        return self
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "request-attempt-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "llm_request_attempt",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": "request-1",
  "payload": {
    "role": null,
    "level": "warning",
    "content": [
      {
        "kind": "text",
        "text": "Модель временно недоступна. Повтор через 2 секунды."
      }
    ],
    "attempt": 1,
    "method": "POST",
    "url": "https://inference.example/request",
    "status_code": 429,
    "duration_ms": 125,
    "error": "rate_limit_exceeded",
    "retry_after_ms": 2000
  }
}
```

`NoticeRecord.payload.level` и `payload.content` задают уведомление; конкретный `kind` —
структурные сведения. Производитель выбирает info/warning/error. Попытка LLM сама содержит
уведомление, отдельный дубликат не создаётся. Участие в LLM определяется категорией,
завершение хода — отдельной `TurnEndRecord`.

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "task-state-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "task_state",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "level": "info",
    "content": [
      {
        "kind": "text",
        "text": "Автоматическое событие: задача task:tests-1 выполняет проверку проекта. Повторный запуск не требуется."
      }
    ],
    "task_id": "task:tests-1",
    "state": {
      "execution_mode": "background",
      "description": "Проверка проекта",
      "status": "running",
      "execution_id": "workspace-job-42",
      "child_session_id": null,
      "output_uri": "file:///workspace/tasks/tests-1.log",
      "result": null,
      "error": null
    },
    "related_message_id": "assistant-launch-1",
    "call_id": "call-tests-1"
  }
}
```

```python
class TaskContextRecord(RecordBase):
    """Снимок фоновой задачи или её необработанного итога, подготовленный для LLM."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        related_message_id: NonEmptyStr  # Исходная TaskStateRecord в этой сессии.
        state: TaskSnapshot
        content: NonEmptyTuple[TextPart]

    kind: Literal["task_context"] = "task_context"
    category: Literal["context"] = "context"
    operation: Literal["set"] = "set"
    request_id: None = None
    key: NonEmptyStr  # task_id из TaskStateRecord.
    payload: Payload

    @model_validator(mode="after")
    def background_task(self) -> Self:
        if self.payload.state.execution_mode != "background":
            raise ValueError("Only background tasks have a separate context snapshot")
        return self
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "task-context-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "task_context",
  "category": "context",
  "key": "task:tests-1",
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "related_message_id": "task-state-1",
    "state": {
      "execution_mode": "background",
      "description": "Проверка проекта",
      "status": "running",
      "execution_id": "workspace-job-42",
      "child_session_id": null,
      "output_uri": "file:///workspace/tasks/tests-1.log",
      "result": null,
      "error": null
    },
    "content": [
      {
        "kind": "text",
        "text": "Автоматическое событие: задача task:tests-1 выполняет проверку проекта. Повторный запуск не требуется."
      }
    ]
  }
}
```

```python
class TaskNotificationRecord(NoticeRecord):
    """Итог фоновой задачи в диалоге; автоматическое событие, не ответ пользователя."""

    class Payload(NoticeRecord.Payload):
        role: Literal["user"] = "user"
        task_id: NonEmptyStr
        status: TaskTerminalStatus
        related_message_id: NonEmptyStr  # Терминальная TaskStateRecord в этой сессии.

    kind: Literal["task_notification"] = "task_notification"
    category: Literal["message"] = "message"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "task-notification-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "task_notification",
  "category": "message",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": "user",
    "level": "info",
    "content": [
      {
        "kind": "text",
        "text": "Автоматическое событие: задача task:tests-1 завершена. Прошли 123 проверки. Это не сообщение и не подтверждение пользователя."
      }
    ],
    "task_id": "task:tests-1",
    "status": "completed",
    "related_message_id": "task-state-2"
  }
}
```

```python
class TaskAcknowledgedRecord(RecordBase):
    """Итог обработан: снять задачу с учёта в активном контексте, сохранив её историю."""

    class Payload(RecordBase.Payload):
        role: Literal["user"] = "user"
        notification_id: NonEmptyStr
        related_message_id: (
            NonEmptyStr  # Ответ агента или явная команда, обработавшая итог.
        )

    kind: Literal["task_acknowledged"] = "task_acknowledged"
    category: Literal["context"] = "context"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr  # task_id принятого результата.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "task-ack-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "task_acknowledged",
  "category": "context",
  "key": "task:tests-1",
  "operation": "remove",
  "request_id": null,
  "payload": {
    "role": "user",
    "notification_id": "task-notification-1",
    "related_message_id": "assistant-report-1"
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "child-origin-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "subagent_session",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "parent_session_id": "parent-chat-1",
    "parent_task_id": "task:review-1",
    "parent_request_id": "parent-request-1"
  }
}
```

Координатор сверяет вызов с историей и сохраняет `pending` до запуска. Далее допустимы
`running` и один терминальный исход; из pending возможен прямой отказ/отмена.
Режим и ссылка на вызов неизменны, повторный запуск получает новый `task_id`.

Foreground возвращает полный `ToolResultRecord`; background — результат с идентификатором
принятой задачи. Вызывающий цикл сохраняет результат, закрывая call. Поздний итог фона —
уведомление, а не второй tool result. Чтение вывода и отмена — новые вызовы tools;
отмену подтверждает событие исполнителя. `output_uri` не подменяет сохранённый результат.

События задачи сохраняются сразу. После закрытия группы tools `deliver` копирует последнее
фоновое состояние в `TaskContextRecord`, терминальный итог — также в `TaskNotificationRecord`.
Доставка не повторяется и ищет события по всей истории, включая записи до компактизации.
После обработки итога `TaskAcknowledgedRecord` снимает снимок. До этого он переносится
при компактизации. Прогресс и уведомления не являются согласием пользователя; продолжение
инициирует цикл агента через новую границу запроса.

Инструмент `agent` создаёт/продолжает дочернюю сессию. Её входы сохраняются до первого
запроса; наследуется явно выбранный завершённый контекст родителя. Дочерний сборщик читает
только свою сессию, родитель получает состояние и итог. Продолжение использует прежнюю
дочернюю сессию и новый task_id. После перезапуска координатор сверяет задачи с исполнителем;
сама запись не запускает их повторно.

```python
class TokenUsage(DataModel):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    reasoning_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0
```

```json
{
  "input_tokens": 120,
  "output_tokens": 35,
  "reasoning_tokens": 0,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0
}
```

```python
class TokenUsageRecord(RecordBase):
    """Итоговые счётчики токенов одного запроса."""

    class Payload(RecordBase.Payload):
        usage: TokenUsage

    kind: Literal["token_usage"] = "token_usage"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    request_id: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "token-usage-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "token_usage",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": "request-1",
  "payload": {
    "role": null,
    "usage": {
      "input_tokens": 120,
      "output_tokens": 35,
      "reasoning_tokens": 0,
      "cache_read_tokens": 0,
      "cache_write_tokens": 0
    }
  }
}
```

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "turn-end-1",
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "kind": "turn_end",
  "category": "operational",
  "key": null,
  "operation": "set",
  "request_id": null,
  "payload": {
    "role": null,
    "turn_id": "turn-1",
    "outcome": "completed",
    "duration_ms": 1800,
    "error": null
  }
}
```

```python
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
    | LlmRequestBoundaryRecord
    | UserMessageRecord
    | ResourceMessageRecord
    | CompactMessageRecord
    | AssistantMessageRecord
    | ToolResultRecord
    | ElicitationRequestRecord
    | ElicitationResponseRecord
    | ElicitationCompletedRecord
    | SlashCommandRecord
    | SlashCommandResultRecord
    | PermissionStateRecord
    | PermissionDecisionRecord
    | LlmRequestAttemptRecord
    | TaskStateRecord
    | TaskContextRecord
    | TaskNotificationRecord
    | TaskAcknowledgedRecord
    | SubagentSessionRecord
    | TokenUsageRecord
    | TextNoticeRecord
    | TurnEndRecord,
    Field(discriminator="kind"),
]
```

```python
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
```

```json
{
  "format_version": 2,
  "session_id": "session-1",
  "rows": [
    {
      "session_id": "session-1",
      "seq": 1,
      "record_id": "user-1",
      "created_at": "2026-11-13T09:00:00Z",
      "version": "0.0.33.dev4",
      "kind": "user_message",
      "category": "message",
      "key": null,
      "operation": "set",
      "request_id": null,
      "payload": {
        "role": "user",
        "content": [
          {
            "kind": "text",
            "text": "Привет"
          }
        ]
      }
    }
  ]
}
```

```python
HISTORY_RECORD_ADAPTER = TypeAdapter(HistoryRecord)
# Неизвестный kind и неверная форма любой записи — ValidationError.
```

```python
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


class HistoryStore(Protocol):
    async def append(
        self,
        session_id: NonEmptyStr,
        records: NonEmptyTuple[HistoryRecord],
        *,
        expected_seq: NonNegativeInt | None = None,
    ) -> tuple[HistoryRecord, ...]:
        """Первым действием сохранить сообщения с record_id и seq; затем разрешить обработку.

        expected_seq проверяется под блокировкой сессии до вставки пачки.
        При сбое — HistoryWriteError с причиной и идентификаторами записей.
        """
        ...

    async def read(
        self,
        session_id: NonEmptyStr,
        *,
        through_seq: PositiveInt | None = None,
    ) -> History:
        """Прочитать записи с начала до through_seq включительно; None — весь текущий журнал."""
        ...
```

`append` получает сообщения с `session_id=None`, `seq=None`; сохраняет их по порядку и возвращает
с назначенными адресами. `record_id` и `version` задаёт производитель до записи.
При сбое возвращается `HistoryWriteError`: storage_unavailable, record_conflict,
invalid_reference, stale_history или commit_unknown. При неизвестном исходе проверяется журнал.

`HistoryCompactor.prepare` — чистая подготовка по истории и готовому пересказу.
Вызывающий код сохраняет результат с `expected_seq`; сборщик компактор не вызывает.

```python
class CompactionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HistoryCompactor(Protocol):
    def prepare(
        self,
        history: History,
        summary: CompactMessageRecord,
        *,
        keep_from_seq: PositiveInt | None = None,
        keep_schema_names: frozenset[str] | None = None,
    ) -> NonEmptyTuple[HistoryRecord]:
        """Подготовить полный снимок; несовместимый хвост или ссылки — CompactionError.

        Копии получают version из summary.
        keep_from_seq=None исключает старый диалог; иначе сохраняется его полный суффикс.
        keep_schema_names=None сохраняет все загруженные схемы; пустое множество — ни одной.
        """
        ...
```

`TaskCoordinator` проверяет переходы, происхождение, обработку уведомлений и отсутствие
повторной доставки. Пачки доставки фиксируются с `expected_seq`.

```python
class TaskCoordinator(Protocol):
    """Запуск, фиксация событий и доставка в контекст; не участвует в сборке LLM-запроса."""

    async def execute(
        self,
        session_id: NonEmptyStr,
        call: ToolCallPart,
        *,
        source_record_id: NonEmptyStr,
        execution_mode: TaskExecutionMode,
    ) -> ToolResultRecord:
        """Сохранить pending до запуска; вернуть итог или подтверждение фонового запуска.

        Вызывающий цикл сохраняет ToolResultRecord через HistoryStore.append.
        """
        ...

    async def record_state(
        self,
        session_id: NonEmptyStr,
        record: TaskStateRecord,
    ) -> TaskStateRecord:
        """Проверить переход и сохранить наблюдённое событие исполнителя."""
        ...

    async def deliver(
        self, session_id: NonEmptyStr, *, through_seq: PositiveInt
    ) -> tuple[HistoryRecord, ...]:
        """Доставить фоновые события подтверждённого префикса после закрытия группы tools."""
        ...

    async def acknowledge(
        self,
        session_id: NonEmptyStr,
        record: TaskAcknowledgedRecord,
    ) -> TaskAcknowledgedRecord:
        """Проверить обработку терминального уведомления и снять снимок задачи."""
        ...
```

```python
class ElicitationCoordinator(Protocol):
    """Производитель событий elicitation; отправка клиенту выполняется после записи."""

    async def request(
        self,
        session_id: NonEmptyStr,
        record: ElicitationRequestRecord,
    ) -> ElicitationRequestRecord:
        """Сохранить запрос и отправить его поддерживающему этот режим клиенту."""
        ...

    async def respond(
        self,
        session_id: NonEmptyStr,
        record: ElicitationResponseRecord,
    ) -> ElicitationResponseRecord:
        """Проверить запрос той же сессии, единственность ответа и схему формы.

        Для URL content запрещён. Сохранить ответ до продолжения обработки.
        """
        ...

    async def complete(
        self,
        session_id: NonEmptyStr,
        record: ElicitationCompletedRecord,
    ) -> ElicitationCompletedRecord:
        """Проверить исходный URL-запрос и единственность завершения, затем сохранить итог.

        Поздний итог не возобновляет отменённую работу. Секреты не передаются в журнал.
        """
        ...
```

## 3 Хранение и SQL-адаптер

Одна таблица [`records`](../experiments/history-sql/schema.sql), ключ `(session_id, seq)`.
`model_dump(mode="json")` совпадает со столбцами; одно сообщение занимает одну строку.
`HISTORY_RECORD_ADAPTER` выбирает модель по kind. Части находятся в `payload.content`;
тип массива в каждой модели определяет допустимые части и их количество.

[`append.sql`](../experiments/history-sql/append.sql) назначает seq под блокировкой сессии.
[`history_integrity.sql`](../experiments/history-sql/history_integrity.sql) проверяет ссылки
на предыдущие записи, включая ссылки через границу компактизации. Ошибка откатывает пачку.
Форму данных проверяют Pydantic-модели; SQL задаёт ключи и уникальность record_id.

[`fixtures.sql`](../experiments/history-sql/fixtures.sql) заполняет тестовые сессии `sql-*`;
доступные `session_id` и `request_id` перечислены в начале файла.

[`openai.sql`](../experiments/history-sql/openai.sql) принимает параметры только в CTE `params`:
`session_id` и `request_id` (null — последняя сохранённая граница). Отсутствие границы — ошибка.
После неё история не читается; все категории собираются от последней компактизации.
`active_records` читает метаданные диапазона без `operational` через индекс `records_llm_seq`.
LAST/SUM работают с метаданными; `selected_records` один раз получает payload выбранных сообщений.
`top_parts` раскрывает content только выбранных сообщений; `parts` добавляет содержимое ресурсов.
`position` в данных валидатора вычисляется из порядка элементов массива и в таблице не хранится.

- `message`, `compact`: SUM в порядке seq и элементов content.
- `prompt`, `context`, `setting`: LAST по `(category, key)` с приоритетом версии запроса;
  выбранный remove исключает ключ. Удаление неактивного prompt — ошибка.
- `tool_catalog`: LAST доступности; удаление неизвестного имени — ошибка.
- `tool_schema`: LAST после последнего remove каталога. Повторный add сохраняет схему;
  add после remove не возвращает её. `catalog_revision` ссылается на предыдущий add того же
  имени внутри непрерывной доступности и текущего диапазона.
- `request`: граница; `operational`: сохраняется, но в LLM не входит.

Все выбранные setting раскрываются одним `jsonb_each(payload.value)`; перечня параметров
и ветвлений по kind нет. Prompt идёт первым разделом, остальное — по seq.
Текст сохраняется буквально, изображения преобразуются в image_url. Непустое содержимое —
массив блоков; только tool calls дают content=null, пустые user/tool — пустую строку.

SQL возвращает кандидат `request` и сведения `validation_data` одним снимком БД.
[`OpenAiRequestValidator`](../experiments/history-sql/openai_validator.py) проверяет порядок
и пары tools, поддерживаемые части, области запроса, зависимости схем и конфликты параметров.
`OpenAiHistoryAdapter` возвращает запрос только после `require_valid`; ошибки — `AssemblyIssue`.
Валидатор чистый, значения инференса не проверяет. Новые kind существующих категорий
не меняют SQL; новый формат содержимого требует поддержки адаптером.

```python
class AssemblyIssue(DataModel):
    code: str
    seq: PositiveInt | None = None
    record_id: str | None = None
    details: JsonObject = Field(default_factory=dict)
```

```json
{
  "code": "missing_tool_result",
  "seq": 2,
  "record_id": "assistant-1",
  "details": {
    "call_id": "call-1"
  }
}
```

```python
class RequestValidationError(ValueError):
    def __init__(self, issues: tuple[AssemblyIssue, ...]) -> None:
        self.issues = issues
        super().__init__("LLM request validation failed")


class OpenAiRequestValidator(Protocol):
    def validate(self, draft: OpenAiRequestDraft) -> tuple[AssemblyIssue, ...]: ...

    def require_valid(self, draft: OpenAiRequestDraft) -> JsonObject:
        """Вернуть request либо поднять RequestValidationError с найденными нарушениями."""
        ...


class OpenAiHistoryAdapter(Protocol):
    """Выполнить SQL, проверить кандидат чистым валидатором и вернуть запрос провайдера."""

    async def build(
        self, session_id: str, request_id: str | None = None
    ) -> JsonObject: ...
```
