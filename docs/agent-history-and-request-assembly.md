# История агента и сборка LLM-запроса

Каждая модель сообщения соответствует одной строке `records`; рядом показан её JSON.
`Payload` описывает JSON-столбец. Общие модели значений показаны как вложенные фрагменты.
Примеры независимы; `session_id` и `seq` в них уже назначены хранилищем.
ACP-проекция описана отдельно: [модели и SQL](agent-history-to-acp.md).

## 1 Общие значения

---

### Импорты и общие типы

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

---

### `DataModel`

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

---

### `HistoryCategory`

Категории и общие поля строк; конкретные модели ниже уточняют `kind` и `payload`.

```python
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
```

---

### `TextPart`

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

---

### `ImagePart`

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

---

### `ResourcePart`

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

---

### `ReasoningPart`

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

---

### `ToolCallPart`

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

---

### `ToolReferencePart`

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

---

### `UserPart`, `AssistantPart`, `ToolResultPart`

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

---

### `ToolCatalogItem`

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

---

### `ToolDefinition`

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

---

### `SkillDefinition`

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

---

### `AgentCatalogItem`

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

---

### `TaskExecutionMode` и `TaskStatus`

```python
TaskExecutionMode = Literal["foreground", "background"]
TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TaskTerminalStatus = Literal["completed", "failed", "cancelled"]
```

`ToolCallPart` един для foreground/background. Сабагент вызывается инструментом `agent`.
Состояния задачи ссылаются на сохранённый вызов по `related_message_id` и `call_id`;
имя и аргументы остаются в исходном сообщении.

---

### `TaskSnapshot`

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

---

### `EnvironmentState`

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

---

### `RecordBase`

```python
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
    version: NonEmptyStr  # Версия boba-cli, создавшего сообщение.
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
  "kind": "record_example",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

`role` — роль записи в диалоге; у записей без роли значение `null`.
`payload.content` — упорядоченный массив типизированных частей сообщения.
`created_at` и `version` принадлежат сообщению; части не дублируют его метаданные.
JSONB сохраняет порядок элементов массивов; отдельный position в частях не требуется.

---

### `SystemPromptAddedRecord`

```python
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
    key: NonEmptyStr  # Свободный ключ, например agent.role или project.rules.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "prompt-add-1",
  "kind": "system_prompt_added",
  "category": "prompt",
  "role": "system",
  "key": "agent.role",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `SystemPromptRemovedRecord`

```python
class SystemPromptRemovedRecord(RecordBase):
    """Удалить промпт постоянно либо исключить его только из request_id."""

    kind: Literal["system_prompt_removed"] = "system_prompt_removed"
    category: Literal["prompt"] = "prompt"
    role: Literal["system"] = "system"
    operation: Literal["remove"] = "remove"
    request_id: NonEmptyStr | None = None
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "prompt-remove-1",
  "kind": "system_prompt_removed",
  "category": "prompt",
  "role": "system",
  "key": "agent.role",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

AGENTS.md, инструкции MCP, язык и стиль ответа используют эти модели. `key` обозначает
заменяемую секцию, `source_uri` — источник. Назначение инструкции задаёт производитель.

---

### `SkillAddedRecord`

```python
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
    key: NonEmptyStr  # Устойчивая идентичность навыка, например skill:workspace:review.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-add-1",
  "kind": "skill_added",
  "category": "context",
  "role": "user",
  "key": "skill:workspace:review",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `SkillRemovedRecord`

```python
class SkillRemovedRecord(RecordBase):
    """Снять доступность навыка; ранее активированные инструкции сохраняются."""

    kind: Literal["skill_removed"] = "skill_removed"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-remove-1",
  "kind": "skill_removed",
  "category": "context",
  "role": "user",
  "key": "skill:workspace:review",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `SkillActivatedRecord`

```python
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
    key: NonEmptyStr  # Та же идентичность, что в каталоге, но категория prompt.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-activate-1",
  "kind": "skill_activated",
  "category": "prompt",
  "role": "system",
  "key": "skill:workspace:review",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `MemoryAddedRecord`

```python
class MemoryAddedRecord(RecordBase):
    """Полный снимок выбранной памяти, действующий до замены или отмены."""

    class Payload(RecordBase.Payload):
        source_uri: NonEmptyStr | None = None
        content: NonEmptyTuple[TextPart]

    kind: Literal["memory_added"] = "memory_added"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    key: NonEmptyStr  # Устойчивая идентичность фрагмента памяти, например memory:workspace:project.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "memory-add-1",
  "kind": "memory_added",
  "category": "context",
  "role": "user",
  "key": "memory:workspace:project",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `MemoryRemovedRecord`

```python
class MemoryRemovedRecord(RecordBase):
    """Прекратить включение выбранной памяти в запросы; исходные записи остаются."""

    kind: Literal["memory_removed"] = "memory_removed"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "memory-remove-1",
  "kind": "memory_removed",
  "category": "context",
  "role": "user",
  "key": "memory:workspace:project",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

Память выбирает клиент или производитель памяти; add сохраняет полный текст выбранного
контекста, remove снимает его. Источник может быть файлом, URL или внутренним ресурсом.
Прочитанный файл не становится памятью автоматически. Разовый материал относится к
`ResourceMessageRecord`, фактический результат tool — к `ToolResultRecord`.
Постоянная память переносится в снимок компактизации.

---

### `AgentAddedRecord`

```python
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
    key: NonEmptyStr  # Например agent:explore; не идентификатор запущенной задачи.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "agent-add-1",
  "kind": "agent_added",
  "category": "context",
  "role": "user",
  "key": "agent:explore",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `AgentRemovedRecord`

```python
class AgentRemovedRecord(RecordBase):
    """Снять тип агента из каталога; уже запущенные задачи продолжают работу."""

    kind: Literal["agent_removed"] = "agent_removed"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
    operation: Literal["remove"] = "remove"
    request_id: None = None
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "agent-remove-1",
  "kind": "agent_removed",
  "category": "context",
  "role": "user",
  "key": "agent:explore",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

Каталог агентов — LAST по `(context, key)`. Параметры, инструменты, инструкции и разрешения
запуска сохраняются отдельными записями дочерней сессии.

---

### `EnvironmentRecord`

```python
class EnvironmentRecord(RecordBase):
    """Полный снимок одной среды; state=None явно снимает её объявление."""

    class Payload(RecordBase.Payload):
        state: EnvironmentState | None
        content: tuple[TextPart, ...] = ()

    kind: Literal["environment"] = "environment"
    category: Literal["context"] = "context"
    role: Literal["user"] = "user"
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
  "kind": "environment",
  "category": "context",
  "role": "user",
  "key": "environment.executor.shell",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `DateRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "date-1",
  "kind": "date",
  "category": "context",
  "role": "user",
  "key": "calendar.current",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `ToolAddedRecord`

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
  "kind": "tool_added",
  "category": "tool_catalog",
  "role": null,
  "key": "mcp__database__list_tables",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "tool": {
      "description": "Получить список таблиц."
    }
  }
}
```

---

### `ToolRemovedRecord`

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
  "kind": "tool_removed",
  "category": "tool_catalog",
  "role": null,
  "key": "mcp__database__list_tables",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `ToolSchemaRecord`

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
  "kind": "tool_schema",
  "category": "tool_schema",
  "role": null,
  "key": "mcp__database__list_tables",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `ParameterChoice`

```python
class ParameterChoice(DataModel):
    """Допустимое значение параметра агента; value_id адресует выбор клиента."""

    value_id: NonEmptyStr
    name: NonEmptyStr
    value: JsonValue
```

```json
{
  "value_id": "precise",
  "name": "Точный",
  "value": 0.2
}
```

---

### `ParameterOptionRecord`

```python
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

        @model_validator(mode="after")
        def unique_choices(self) -> Self:
            if len({c.value_id for c in self.choices}) != len(self.choices):
                raise ValueError("Parameter choice IDs must be unique")
            return self

    kind: Literal["parameter_option"] = "parameter_option"
    key: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "parameter_option-1",
  "kind": "parameter_option",
  "category": "operational",
  "role": null,
  "key": "temperature",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "name": "Температура",
    "description": null,
    "source_kinds": ["sampling", "sampling_removed"],
    "source_key": "sampling",
    "value_path": [
      "value",
      "temperature"
    ],
    "choices": [
      {
        "value_id": "precise",
        "name": "Точный",
        "value": 0.2
      },
      {
        "value_id": "creative",
        "name": "Творческий",
        "value": 0.8
      }
    ]
  }
}
```

---

### `ParameterOptionRemovedRecord`

```python
class ParameterOptionRemovedRecord(RecordBase):
    """Убирает возможность менять параметр; сохранённое значение настройки не меняет."""

    kind: Literal["parameter_option_removed"] = "parameter_option_removed"
    key: NonEmptyStr
    operation: Literal["remove"] = "remove"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "parameter_option_removed-1",
  "kind": "parameter_option_removed",
  "category": "operational",
  "role": null,
  "key": "temperature",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `ParameterRecord`

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
  "kind": "parameter_example",
  "category": "setting",
  "role": null,
  "key": "sampling",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

В настройках `request_id=null` задаёт постоянное действие, заполненный `request_id` —
действие только для указанного LLM-запроса. `operation=remove` отменяет настройки с данным
`key`: постоянно до следующего set либо только для указанного request_id.

---

### `ParameterRemovedRecord`

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
  "kind": "parameter_removed_example",
  "category": "setting",
  "role": null,
  "key": "sampling",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `ModelRecord`

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
  "kind": "model",
  "category": "setting",
  "role": null,
  "key": "model",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "model": "agent-model"
    },
    "provider": "openai"
  }
}
```

`ModelRecord.payload.provider` выбирает подключение и адаптер; поля payload.value
переносятся в тело запроса. Новая модель и совместимые настройки сохраняются до LlmRequestBoundaryRecord.

---

### `SamplingRecord`

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
  "kind": "sampling",
  "category": "setting",
  "role": null,
  "key": "sampling",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "temperature": 0.2,
      "top_p": 0.9
    }
  }
}
```

---

### `SamplingRemovedRecord`

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
  "kind": "sampling_removed",
  "category": "setting",
  "role": null,
  "key": "sampling",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `InferenceRecord`

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
  "kind": "inference",
  "category": "setting",
  "role": null,
  "key": "inference",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "max_completion_tokens": 1024,
      "reasoning_effort": "medium"
    }
  }
}
```

---

### `InferenceRemovedRecord`

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
  "kind": "inference_removed",
  "category": "setting",
  "role": null,
  "key": "inference",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `ProviderParametersRecord`

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
  "kind": "provider_parameters",
  "category": "setting",
  "role": null,
  "key": "provider_parameters",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "parallel_tool_calls": true
    }
  }
}
```

---

### `ProviderParametersRemovedRecord`

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
  "kind": "provider_parameters_removed",
  "category": "setting",
  "role": null,
  "key": "provider_parameters",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `ResponseFormatRecord`

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
  "kind": "response_format",
  "category": "setting",
  "role": null,
  "key": "response_format",
  "operation": "set",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `ResponseFormatRemovedRecord`

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
  "kind": "response_format_removed",
  "category": "setting",
  "role": null,
  "key": "response_format",
  "operation": "remove",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `ToolChoiceRecord`

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
  "kind": "tool_choice",
  "category": "setting",
  "role": null,
  "key": "tool_choice",
  "operation": "set",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "tool_choice": "auto"
    }
  }
}
```

---

### `ToolChoiceRemovedRecord`

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
  "kind": "tool_choice_removed",
  "category": "setting",
  "role": null,
  "key": "tool_choice",
  "operation": "remove",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `StreamingRecord`

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
  "kind": "streaming",
  "category": "setting",
  "role": null,
  "key": "streaming",
  "operation": "set",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "stream": true
    }
  }
}
```

---

### `StreamingRemovedRecord`

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
  "kind": "streaming_removed",
  "category": "setting",
  "role": null,
  "key": "streaming",
  "operation": "remove",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `RequestContextRecord`

```python
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
            if self.mode == "isolated" and (self.from_seq is not None or self.through_seq is not None):
                raise ValueError("An isolated request has no conversation range")
            return self

    kind: Literal["request_context"] = "request_context"
    category: Literal["request_context"] = "request_context"
    request_id: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "request_context-1",
  "kind": "request_context",
  "category": "request_context",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": "summary-1",
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "mode": "conversation",
    "from_seq": null,
    "through_seq": 7
  }
}
```

---

### `RequestMessageRecord`

```python
class RequestMessageRecord(RecordBase):
    """Готовый ввод только для request_id; не становится репликой основного диалога."""

    class Payload(RecordBase.Payload):
        content: NonEmptyTuple[UserPart]

    kind: Literal["request_message"] = "request_message"
    category: Literal["request_message"] = "request_message"
    role: Literal["user"] = "user"
    request_id: NonEmptyStr
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "request_message-1",
  "kind": "request_message",
  "category": "request_message",
  "role": "user",
  "key": null,
  "operation": "set",
  "request_id": "summary-1",
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Сохрани в пересказе решения, ограничения и незавершённые задачи."
      }
    ]
  }
}
```

---

### `LlmRequestBoundaryRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 10,
  "record_id": "request-1",
  "kind": "llm_request_boundary",
  "category": "request",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `UserMessageRecord`

```python
class UserMessageRecord(RecordBase):
    """Сохранённый ввод пользователя; MessageAcceptedRecord включает текст в контекст LLM."""

    class Payload(RecordBase.Payload):
        content: NonEmptyTuple[UserPart]

    kind: Literal["user_message"] = "user_message"
    category: Literal["input"] = "input"
    role: Literal["user"] = "user"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "user-1",
  "kind": "user_message",
  "category": "input",
  "role": "user",
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Привет"
      }
    ]
  }
}
```

---

### `ResourceMessageRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "resource-message-1",
  "kind": "resource_message",
  "category": "input",
  "role": "user",
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `MessageAcceptedRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 4,
  "record_id": "accept-user-1",
  "kind": "message_accepted",
  "category": "admission",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "source_record_id": "user-1"
  }
}
```

Порядок записи: assistant/tool call → UserMessageRecord → ToolResultRecord → MessageAcceptedRecord.
Порядок LLM: assistant/tool call → tool result → текст UserMessageRecord.
До принятия ввод доступен в истории и ACP, но не входит в LLM-запрос.
В свободной сессии ввод и принятие сохраняются одной пачкой, каждый со своим seq.
Если во время tools поступило несколько сообщений, после всех результатов tools
производитель принимает весь ожидающий ввод в порядке исходных seq и добавляет одну
LlmRequestBoundaryRecord. Один LLM-запрос включает все принятые сообщения, каждое со своим текстом.
При компактизации сохраняемый ввод и его принятие копируются вместе; ссылка ведёт на копию ввода.
Непринятый ввод остаётся доступен по record_id, даже если поступил до последней компактизации.

---

### `MessageRejectedRecord`

```python
class MessageRejectedRecord(MessageAcceptedRecord):
    """Закрывает ещё не принятый ввод при отмене; текст не поступает в LLM."""

    class Payload(MessageAcceptedRecord.Payload):
        reason: NonEmptyStr

    kind: Literal["message_rejected"] = "message_rejected"
    operation: Literal["remove"] = "remove"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 4,
  "record_id": "reject-user-1",
  "kind": "message_rejected",
  "category": "admission",
  "role": null,
  "key": null,
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {"source_record_id": "user-1", "reason": "Пользователь отменил ожидающий ввод."}
}
```

Принятие и отклонение взаимоисключающие: индекс records_admission допускает одно
решение для source_record_id. Прочитать ещё не решённый ввод позволяет
[`pending_inputs.sql`](../experiments/history-sql/pending_inputs.sql), включая ввод до компактизации.

---

### `CompactMessageRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "compact-1",
  "kind": "compact_message",
  "category": "compact",
  "role": "user",
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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
`copied_from_id` указывает на непосредственно скопированную запись того же kind/category
в этой сессии. ACP показывает оригиналы, LLM-сборщик использует копии в новом контексте.

`expected_seq` защищает снимок от конкурентной записи. Незакрытые пары tools и разовые
настройки без границы запроса — ошибка подготовки. Команда `/compact` и ошибка сжатия
не меняют активную границу; сборщик не достраивает неполный снимок.

---

### `AssistantMessageRecord`

```python
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
  "kind": "assistant_message",
  "category": "message",
  "role": "assistant",
  "key": null,
  "operation": "set",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `RequestOutputRecord`

```python
class RequestOutputRecord(AssistantMessageRecord):
    """Ответ служебного LLM-вызова; связан с request_id, в основной диалог не включается.

    Успешный пересказ затем сохраняется как CompactMessageRecord; ответ классификатора
    обрабатывает политика разрешений. Получение ответа само по себе контекст не меняет.
    """

    kind: Literal["request_output"] = "request_output"
    category: Literal["operational"] = "operational"
```

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "request_output-1",
  "kind": "request_output",
  "category": "operational",
  "role": "assistant",
  "key": null,
  "operation": "set",
  "request_id": "summary-1",
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Согласованы модели истории; следующий шаг — проверка SQL."
      }
    ],
    "completion": "complete"
  }
}
```

---

### `ToolResultRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool-result-1",
  "kind": "tool_result",
  "category": "message",
  "role": "tool",
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `ElicitationRequestRecord`

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
  "kind": "elicitation_request",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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
  "kind": "elicitation_request",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `ElicitationResponseRecord`

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
  "kind": "elicitation_response",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:01:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `ElicitationCompletedRecord`

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
  "kind": "elicitation_completed",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:02:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `AgentCommandOwner`

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

---

### `ClientCommandOwner`

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

---

### `McpPromptCommandOwner`

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

---

### `McpAppCommandOwner`

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

---

### `SlashCommandOwner`

```python
SlashCommandOwner = Annotated[
    AgentCommandOwner | ClientCommandOwner | McpPromptCommandOwner | McpAppCommandOwner,
    Field(discriminator="kind"),
]
```

---

### `CommandAddedRecord`

```python
class CommandAddedRecord(RecordBase):
    """Объявляет доступную команду; LAST по key обновляет описание и владельца."""

    class Payload(RecordBase.Payload):
        description: str
        owner: SlashCommandOwner
        input_hint: str | None = None

    kind: Literal["command_added"] = "command_added"
    key: NonEmptyStr  # Публичное имя команды без / и пробелов.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "compact-command",
  "kind": "command_added",
  "category": "operational",
  "role": null,
  "key": "compact",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "description": "Сжать контекст разговора",
    "owner": {"kind": "agent"},
    "input_hint": "Что сохранить в пересказе"
  }
}
```

---

### `CommandRemovedRecord`

```python
class CommandRemovedRecord(RecordBase):
    """Убирает команду из доступного каталога; прежние вызовы остаются в истории."""

    kind: Literal["command_removed"] = "command_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 2,
  "record_id": "compact-command-removed",
  "kind": "command_removed",
  "category": "operational",
  "role": null,
  "key": "compact",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `SlashCommandRecord`

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
  "kind": "slash_command",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "name": "compact",
    "text": "/compact",
    "arguments": {},
    "owner": {
      "kind": "agent"
    }
  }
}
```

---

### `SlashCommandResultRecord`

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
  "kind": "slash_command_result",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `PermissionRule`

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

---

### `PermissionStateRecord`

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
  "kind": "permission_state",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `PermissionOption`

```python
class PermissionOption(DataModel):
    """Выбор пользователя: разрешение или отказ для вызова либо всей сессии."""

    option_id: NonEmptyStr
    name: NonEmptyStr
    decision: Literal["allow", "deny"]
    scope: Literal["call", "session"]
```

```json
{
  "option_id": "once",
  "name": "Разрешить один раз",
  "decision": "allow",
  "scope": "call"
}
```

---

### `PermissionRequestRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 2,
  "record_id": "permission_request-1",
  "kind": "permission_request",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "related_message_id": "assistant-1",
    "call_id": "call-1",
    "title": "Запустить проверку?",
    "description": null,
    "options": [
      {
        "option_id": "once",
        "name": "Разрешить один раз",
        "decision": "allow",
        "scope": "call"
      }
    ]
  }
}
```

---

### `PermissionResponseRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 2,
  "record_id": "permission_response-1",
  "kind": "permission_response",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "permission_request_id": "permission_request-1",
    "action": "selected",
    "option_id": "once"
  }
}
```

---

### `PermissionDecisionRecord`

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
  "kind": "permission_decision",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "call_id": "call-1",
    "decision": "allow",
    "reason": "Инструмент разрешён правилами агента."
  }
}
```

Ответ проверяется по сохранённым options и фиксируется до выполнения tool.
Выбор scope=session дополнительно создаёт PermissionStateRecord с обновлённым правилом.
Неизвестный option_id, отмена и повторный ответ не дают разрешения.

Исполнитель использует последнюю `PermissionStateRecord`; `deny` запрещает инструмент,
не удаляя его из каталога. Решение сохраняется в `PermissionDecisionRecord`, отказ —
в обычном `ToolResultRecord`. Эти правила действуют только в своей сессии.

---

### `NoticeRecord`

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
  "kind": "notice",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `TextNoticeRecord`

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
  "kind": "notice",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `LlmRequestAttemptRecord`

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
  "kind": "llm_request_attempt",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `TaskStateRecord`

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
  "kind": "task_state",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `TaskContextRecord`

```python
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
  "kind": "task_context",
  "category": "context",
  "role": "user",
  "key": "task:tests-1",
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `TaskNotificationRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "task-notification-1",
  "kind": "task_notification",
  "category": "message",
  "role": "user",
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `TaskAcknowledgedRecord`

```python
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
    key: NonEmptyStr  # task_id принятого результата.
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "task-ack-1",
  "kind": "task_acknowledged",
  "category": "context",
  "role": "user",
  "key": "task:tests-1",
  "operation": "remove",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "notification_id": "task-notification-1",
    "related_message_id": "assistant-report-1"
  }
}
```

---

### `SubagentSessionRecord`

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
  "kind": "subagent_session",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "parent_session_id": "parent-chat-1",
    "parent_task_id": "task:review-1",
    "parent_request_id": "parent-request-1"
  }
}
```

Инструмент `agent` создаёт/продолжает дочернюю сессию. Её входы сохраняются до первого
запроса; наследуется явно выбранный завершённый контекст родителя. Дочерний сборщик читает
только свою сессию, родитель получает состояние и итог. Продолжение использует прежнюю
дочернюю сессию и новый task_id. После перезапуска координатор сверяет задачи с исполнителем;
сама запись не запускает их повторно.

---

### `TokenUsage`

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

---

### `TokenUsageRecord`

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
  "kind": "token_usage",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": "request-1",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
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

---

### `TurnEndRecord`

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
  "kind": "turn_end",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "request_id": null,
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "turn_id": "turn-1",
    "outcome": "completed",
    "duration_ms": 1800,
    "error": null
  }
}
```

---

### `HistoryRecord`

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
    | TurnEndRecord,
    Field(discriminator="kind"),
]
```

---

### `History`

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
      "kind": "user_message",
      "category": "input",
      "role": "user",
      "key": null,
      "operation": "set",
      "request_id": null,
      "copied_from_id": null,
      "created_at": "2026-11-13T09:00:00Z",
      "version": "0.0.33.dev4",
      "payload": {
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

---

### `HISTORY_RECORD_ADAPTER`

```python
HISTORY_RECORD_ADAPTER = TypeAdapter(HistoryRecord)
# Неизвестный kind и неверная форма любой записи — ValidationError.
```

---

### `HistoryWriteError`

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
```

---

### `HistoryStore`

```python
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
        after_seq: NonNegativeInt = 0,
        through_seq: NonNegativeInt | None = None,
    ) -> History:
        """Прочитать после after_seq до through_seq включительно; None — до конца журнала."""
        ...

    async def pending_inputs(
        self, session_id: NonEmptyStr
    ) -> tuple[UserMessageRecord | ResourceMessageRecord, ...]:
        """Непринятый и неотклонённый ввод в порядке исходных seq."""
        ...
```

`append` получает сообщения с `session_id=None`, `seq=None`; сохраняет их по порядку и возвращает
с назначенными адресами. `record_id` и `version` задаёт производитель до записи.
При сбое возвращается `HistoryWriteError`: storage_unavailable, record_conflict,
invalid_reference, stale_history или commit_unknown. При неизвестном исходе проверяется журнал.

---

### `CompactionError`

```python
class CompactionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
```

---

### `HistoryCompactor`

`HistoryCompactor.prepare` — чистая подготовка по истории и готовому пересказу.
Вызывающий код сохраняет результат с `expected_seq`; сборщик компактор не вызывает.

```python
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

        Копии получают version из summary и copied_from_id исходной записи.
        keep_from_seq=None исключает старый диалог; иначе сохраняется его полный суффикс.
        keep_schema_names=None сохраняет все загруженные схемы; пустое множество — ни одной.
        """
        ...
```

---

### `TaskCoordinator`

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

---

### `ElicitationCoordinator`

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
Параметры в `params`: session_id, JSON-массив моделей, expected_seq либо null.
SQL возвращает records с назначенными адресами, retry и error. Точная повторная пачка
возвращает прежние записи; изменённое содержимое под тем же ID — record_conflict.
Новые записи при несовпадающем expected_seq — stale_history. Непустой error запрещает commit.
Блокировка берётся отдельным действием транзакции до SQL; чтения истории блокировку не берут.
[`history_integrity.sql`](../experiments/history-sql/history_integrity.sql) проверяет ссылки
на предыдущие записи, включая ссылки через границу компактизации. Ошибка откатывает пачку.
Форму данных проверяют Pydantic-модели; SQL задаёт ключи и уникальность record_id.

[`openai.sql`](../experiments/history-sql/openai.sql) принимает параметры только в CTE `params`:
`session_id`, `request_id`, `through_seq`.

- `request_id` находит seq записи LlmRequestBoundaryRecord по её record_id.
- `through_seq` задаёт seq непосредственно, включая указанную запись.

Далее сборка одинакова: от последней компактизации в срезе либо с начала чата до выбранного seq.
Разовые настройки действуют до своей LlmRequestBoundaryRecord включительно: уже сохранённые
настройки формируемого запроса учитываются и до появления границы. После границы разовые
настройки зафиксированного запроса не применяются. Записи после выбранного seq не влияют на сборку.

При обоих `null` выбирается последняя LlmRequestBoundaryRecord; отсутствие границы — `missing_request`.
Если заданы оба параметра, seq найденной границы должен совпасть с through_seq;
иначе — `conflicting_selection`.
Срез вне диапазона `0..last_seq` — `invalid_history_range`. Произвольный срез может быть
неполным: например, отсутствие результата tool — `missing_tool_result`, модели — `missing_model`.
Разовые настройки нескольких ещё не закрытых границей запросов — `ambiguous_request_scope`.
SQL возвращает кандидат и метаданные; ошибки обнаруживает валидатор.

`active_records` читает метаданные диапазона без `operational` через индекс `records_llm_seq`.
LAST/SUM работают с метаданными; `selected_records` один раз получает payload выбранных сообщений.
`top_parts` раскрывает content только выбранных сообщений; `parts` добавляет содержимое ресурсов.
`position` в данных валидатора вычисляется из порядка элементов массива и в таблице не хранится.

- `message`, `compact`: SUM в порядке seq и элементов content.
- `input`: текст хранится один раз; непосредственно в LLM не входит.
- `admission`: SUM принятого ввода на позициях seq записей принятия;
  роль и content берутся по source_record_id из input той же сессии.
- `request_message`: SUM собственных входов выбранного request_id.
- `request_context`: LAST правила выбора входного диалога выбранного request_id.
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

SQL возвращает provider, кандидат request и validation_data одним снимком БД.
provider выбирает подключение; в тело инференса provider не добавляется.
Транспорт получает URL и credentials выбранного подключения, но не дополняет тело
model/sampling/stream/stream_options: все эти значения предварительно сохраняются в истории.

Пересказ и классификатор разрешений используют ту же сессию и тот же SQL.
Производитель сохраняет RequestContextRecord, RequestMessageRecord, разовые настройки
и LlmRequestBoundaryRecord. Для пересказа выбирается conversation и закрытый диапазон
диалога; для классификатора — isolated и собственная инструкция SystemPromptAddedRecord
с request_id. Если RequestContextRecord отсутствует, используется основной диалог целиком
от компактизации. Диапазон не может начинаться до текущей компактизации или заходить
в будущее относительно RequestContextRecord. Разрыв пары tools — ошибка валидатора.

Ответ служебного вызова сохраняется как RequestOutputRecord, usage — как TokenUsageRecord
с тем же request_id. Успешный пересказ отдельно становится CompactMessageRecord;
ответ классификатора используется при создании PermissionDecisionRecord.
После границы служебные входы и настройки перестают действовать; обычный запрос
не включает служебный ответ. Просмотр through_seq до появления границы использует
только уже сохранённые записи и не читает будущую границу.
[`OpenAiRequestValidator`](../experiments/history-sql/openai_validator.py) проверяет порядок
и пары tools, поддерживаемые части, области запроса, зависимости схем и конфликты параметров.
`OpenAiHistoryAdapter` возвращает запрос только после `require_valid`; ошибки — `AssemblyIssue`.
Валидатор чистый, значения инференса не проверяет. Новые kind существующих категорий
не меняют SQL; новый формат содержимого требует поддержки адаптером.

---

### `AssemblyIssue`

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

---

### `RequestValidationError`

```python
class RequestValidationError(ValueError):
    def __init__(self, issues: tuple[AssemblyIssue, ...]) -> None:
        self.issues = issues
        super().__init__("LLM request validation failed")
```

---

### `OpenAiRequestValidator`

```python
class OpenAiRequestValidator(Protocol):
    def validate(self, draft: OpenAiRequestDraft) -> tuple[AssemblyIssue, ...]: ...

    def require_valid(self, draft: OpenAiRequestDraft) -> JsonObject:
        """Вернуть request либо поднять RequestValidationError с найденными нарушениями."""
        ...
```

---

### `OpenAiHistoryAdapter`

```python
class OpenAiHistoryAdapter(Protocol):
    """Выполнить SQL, проверить кандидат чистым валидатором и вернуть запрос провайдера."""

    async def build(
        self,
        session_id: str,
        request_id: str | None = None,
        *,
        through_seq: NonNegativeInt | None = None,
    ) -> OpenAiRequestDraft:
        """Проверенный результат SQL: provider для маршрута, request для HTTP.

        request_id либо through_seq выбирают срез; JSON запроса уже готов.
        """
        ...
```
