# История агента и сборка LLM-запроса

Документ задаёт целевой формат одной таблицы `boba.records`, модели записей и правила
сборки из истории. Python-модель строки и соседний JSON имеют одинаковые поля;
`payload` содержит предметные данные. Примеры отдельных моделей независимы.

## 1 Общие принципы

### История и её производители

Все полные сообщения и изменения состояния сначала сохраняются в PostgreSQL.
Производитель подготавливает текст, структурные данные и идентификаторы; хранилище
назначает `seq`. Ошибка записи возвращается вызывающему коду. Сохранённые строки
не изменяются: новое состояние записывается новой строкой.

Сборщик читает только выбранный срез истории. Файлы, URI, часы, конфигурация процесса
и таблица исполняемых задач не являются источниками данных сборщика. Текст сохраняется
производителем и при сборке переносится буквально; адаптер преобразует структуру
сообщения в формат провайдера. Чанки передаются клиентам по ACP и не пишутся в историю.
Решение начать генерацию принимает runtime; сборщик не запускает действия по записям.

### Идентификаторы и порядок

- `session_id` определяет историю одного диалога.
- `record_id` уникален среди строк сессии и адресует одну запись.
- `seq` — возрастающий номер сохранения строки в сессии. Он задаёт доступность записи
  в историческом срезе и основной порядок диалога.
- `operation_id` объединяет составляющие одной логической операции: пользовательское
  сообщение с настройками, ответ LLM с вызовами tools, прочитанный статус с результатом
  опроса или одно изменение конфигурации. Поле обязательно у каждой записи, включая
  операции из одной строки. Производитель выделяет ID один раз на операцию.
- `operation_position` — позиция составляющей внутри операции, начиная с `0`.
  Пара `(operation_id, operation_position)` уникальна в сессии. Номер попытки HTTP
  и смена состояния задачи не являются позициями частей прежней операции.
- `parent_operation_id` — необязательная ссылка на другую операцию той же сессии.
  У частей одной операции одинаковый родитель. Например, операция ответа ссылается
  на попытку инференса, а повторная попытка — на предыдущую попытку.
  Родитель не определяет состав LLM-контекста и не передаёт настройки по наследству.
- `copied_from_id` адресует исходную строку, скопированную при компактизации.
  Связи с конкретным вызовом, задачей и результатом задают `call_id`, `task_id`
  и соответствующие поля `*_record_id`; группировка эти связи не заменяет.

Составляющие одного подготовленного сообщения сохраняются атомарно в порядке
`operation_position`. `seq` упорядочивает записи разных операций. Порядок частей
внутри `payload.content` задаётся массивом: JSONB порядок элементов массива сохраняет.
`created_at` — время создания записи, `version` — версия записавшего её агента.

### Категория, тип и состояние

`kind` выбирает конкретную Pydantic-модель. `category` задаёт контракт сборки:
обязательные общие поля, SUM или LAST, допустимый `scope`, обработку `set/remove`
и место результата в запросе. Новый `kind` с тем же контрактом и поддерживаемыми
частями `content` не требует ветвления в сборщике. Новая категория или формат части
требует поддержки адаптера; неизвестные варианты дают явную ошибку.
Поддержку части для конкретной роли в протоколе проверяет адаптер: неподдерживаемое
содержимое даёт ошибку, а не удаляется из сообщения.

`role` задаёт роль сообщения; у записей без собственной роли значение `null`.
`key` — непустой ключ заменяемого состояния, имени инструмента или вызова
согласно категории; для остальных записей — `null`. Префиксы ключей задают производители.
`operation` имеет только значения `set` и `remove`; это действие над состоянием,
а не идентификатор операции. LAST выбирает целую запись, а SUM сохраняет последовательность.
Пустой объект параметров не заменяет отдельную запись удаления.

### Область действия и параметры сборки

`scope` применяется к категориям `message`, `prompt`, `context`, `setting` и `assembly`:

- `session` — сообщение или состояние участвует в последующих сборках сессии.
- `request` — запись участвует только при выборе её `operation_id` для сборки.
- У остальных категорий поле `scope` отсутствует в модели и JSON; отбор определяется
  их связями и контрактами. В столбце таблицы для таких записей хранится `NULL`.

`operation_id` заполнен при любом `scope`. Настройки `scope=request` не расходуются
и не истекают после обращения к LLM. Их повторная сборка подчиняется тем же правилам.
Для LAST-категорий запись выбранной операции имеет приоритет над записью сессии
с тем же `(category, key)`, независимо от взаимного порядка этих двух областей.
В каждой области выбирается последний `seq`. Выбранный `remove` исключает весь ключ:
разовое удаление не изменяет постоянное состояние и не возвращает его в текущий запрос.

Сборка принимает `session_id`, `operation_id` и `through_seq`. `through_seq` включителен;
при отсутствии выбирается последний сохранённый `seq` одним снимком БД.
`operation_id` выбирает разовые входы и переопределения, но не ограничивает основной
диалог сообщениями этой операции. При `operation_id=null` используются только входы
сессии. Полный диалог собирается от последней компактизации в срезе либо с начала чата.
Разовые входы выбранной операции ищутся во всём срезе до through_seq; граница
компактизации ограничивает входы сессии, но не заменяет выбор operation_id.
Условия служебной операции задаёт `AssemblyContextRecord`; его диапазон переписки
не ограничивает постоянные настройки или собственные входы выбранной операции.

Если накопилось несколько пользовательских сообщений, runtime запускает одну генерацию
для всей ожидающей пачки. Для сборки выбирается `operation_id` последнего сообщения
по `seq`. Используются только разовые настройки выбранной операции и настройки сессии;
разовые настройки предыдущих операций не объединяются. Все сообщения пачки остаются
в диалоге. Выбор операции выполняет runtime, а не SQL-сборщик.

Отдельная запись о начале LLM-запроса для сборки не нужна. Сборка ничего не добавляет
в историю и не ищет событие, запустившее runtime. Для одного среза, выбранной операции
и версии адаптера результат детерминирован. Несогласованные данные дают ошибку;
сборщик не выдумывает результаты, настройки или текст.

### Вызовы инструментов и наблюдения

`AssistantMessageRecord`, определения всех вызовов и начальные `ToolResultRecord`
сохраняются одной транзакцией с регистрацией задач. Для поставленного на исполнение
вызова результат имеет status=pending; быстрый вызов может сразу иметь завершённый результат.
Эти записи имеют один `operation_id` ответа. Для каждого включённого вызова SQL выбирает
LAST `ToolResultRecord` по key=call_id в выбранном срезе, независимо от стадии исполнения.
В запросе все результаты стоят сразу после assistant с вызовами, в порядке ссылок
в `content`. Группа допустима только целиком: каждый вызов должен иметь результат.
Пользовательские сообщения и изменения контекста можно сохранять во время исполнения tools.

Worker меняет состояние задачи в API задач. Координатор находит исходный вызов по
ToolCallRecord.payload.task_id и сохраняет каждое чтение новым ToolResultRecord этого вызова,
включая повторную revision. Новое чтение — новая операция; LAST определяется seq наблюдения.
Терминальный снимок уже является результатом: дополнительная строка о завершении не нужна.

Вызов `task_status` получает собственный ToolResultRecord с key своего call_id и
status=completed при успешном чтении. source_record_id закрепляет точную запись наблюдения;
последующие изменения задачи не меняют ответ этому опросу. Статус опроса не наследуется
из наблюдения: проверяемая задача может быть running или failed. При отсутствии исходного
вызова в сессии снимок внешней задачи хранится непосредственно в результате опроса.

Каталог доступности tool_catalog и полученные схемы tool_schema ведутся независимыми
дельтами. Получение схемы само по себе не добавляет инструмент в каталог.

### Попытка инференса, ответ и компактизация

Одна завершённая попытка инференса — одна строка `LlmRequestAttemptRecord` и отдельная
операция с `operation_position=0`. Запись создаётся после ответа, ошибки или отмены;
незавершённая при аварии процесса попытка может отсутствовать в журнале. Каждая повторная
попытка получает собственный `operation_id`; поле `payload.attempt` хранит номер повтора.
Технические данные и необязательный debug-payload этой записи не влияют на сборку.

Ответ имеет свой `operation_id`; `parent_operation_id` указывает на операцию конкретной
попытки. `AssistantMessageRecord`, его `ToolCallRecord`, начальные `ToolResultRecord`
и `TokenUsageRecord` принадлежат операции ответа. Группа может содержать разные категории:
счётчики токенов имеют `category=operational` и не становятся содержимым LLM-запроса.
Тело фактической отправки можно сохранить для debug; без тела либо сохранённых координат
сборки нельзя гарантировать восстановление входа именно этой попытки по одному ответу.

`CompactMessageRecord` — успешный пересказ и начало нового активного контекста.
Команда `/compact`, служебное задание и неудачная попытка пересказа границу не меняют.
Пересказ и переносимые данные сохраняются атомарно; сборщик не выполняет компактизацию.

### Пример группировки

```text
operation_id=user-1, parent_operation_id=null
├── position=0: UserMessageRecord, scope=session
└── position=1: SamplingRecord, scope=request

operation_id=attempt-1, parent_operation_id=null
└── position=0: LlmRequestAttemptRecord, attempt=1, status=failed

operation_id=attempt-2, parent_operation_id=attempt-1
└── position=0: LlmRequestAttemptRecord, attempt=2, status=failed

operation_id=attempt-3, parent_operation_id=attempt-2
└── position=0: LlmRequestAttemptRecord, attempt=3, status=failed

operation_id=attempt-4, parent_operation_id=attempt-3
└── position=0: LlmRequestAttemptRecord, attempt=4, status=completed

operation_id=answer-1, parent_operation_id=attempt-4
├── position=0: AssistantMessageRecord
├── position=1: ToolCallRecord, key=call-1, task_id=t1
├── position=2: ToolResultRecord, key=call-1, status=pending
└── position=3: TokenUsageRecord
```

`position` в этой схеме обозначает `operation_position`. Сборка для user-1 использует
его SamplingRecord; технические операции attempt-1…attempt-4 не задают область настроек.
Причина запуска попытки не кодируется обязательной родительской ссылкой на ввод.

## 2 Общие модели и категории

---

### Импорты и общие типы

```python
from __future__ import annotations

from datetime import date as CalendarDate
from enum import StrEnum
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

Категория задаёт поля и правило сборки независимо от конкретного `kind`.
`operational` исключается из LLM-сборки; `assembly` задаёт входные условия, но не входит
в тело запроса. Наличие роли или текста само по себе не включает запись в LLM.

```python
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
```

`MessageRecord`, `PromptRecord`, `ContextRecord` и `ParameterRecord` закрепляют общие
контракты категорий. Например, для `context` сборщик читает `key`, `operation`, `scope`,
`operation_id`, `role` и `payload.content`; поля `payload.skill`, `payload.agent` и
`payload.state` содержат предметные данные, но не требуют новых ветвлений в сборщике.

---

### Дерево использования моделей

Ветви показывают поля, содержащие другие модели; `[]` обозначает элемент массива.
Несколько типов под одним полем — допустимые варианты по `kind`.
Общая модель повторяется в каждом месте использования; скалярные поля опущены.
Колонка `category` слева относится к записи; вложенные модели собственной категории не имеют.
Все записи категории `message` наследуют `payload.content: NonEmptyTuple[MessagePart]`
от `MessageRecord`. В дереве показаны допустимые части с учётом роли: `ReasoningPart`
и `ToolCallReferencePart` разрешены только для `assistant`.

```text
category        │ модель / поле
────────────────┼───────────────────────────────────────────────────────
                │ History
                │ └── rows[]: HistoryRecord
assembly        │     ├── AssemblyContextRecord
message         │     ├── RequestMessageRecord
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       └── ResourcePart
                │     │           └── content[]
                │     │               ├── TextPart
                │     │               └── ImagePart
message         │     ├── UserMessageRecord
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       └── ResourcePart
                │     │           └── content[]
                │     │               ├── TextPart
                │     │               └── ImagePart
message         │     ├── ResourceMessageRecord
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       └── ResourcePart
                │     │           └── content[]
                │     │               ├── TextPart
                │     │               └── ImagePart
message         │     ├── AssistantMessageRecord
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       ├── ResourcePart
                │     │       │   └── content[]
                │     │       │       ├── TextPart
                │     │       │       └── ImagePart
                │     │       ├── ReasoningPart
                │     │       └── ToolCallReferencePart
tool_call       │     ├── ToolCallRecord
tool_result     │     ├── ToolResultRecord
                │     │   ├── payload.state: TaskSnapshot | None
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       ├── ResourcePart
                │     │       │   └── content[]
                │     │       │       ├── TextPart
                │     │       │       └── ImagePart
                │     │       └── ToolReferencePart
compact         │     ├── CompactMessageRecord
                │     │   └── payload.content[]: TextPart
prompt          │     ├── SystemPromptAddedRecord
                │     │   └── payload.content[]: TextPart
prompt          │     ├── SystemPromptRemovedRecord
                │     │   └── payload.content[]: TextPart
prompt          │     ├── SkillActivatedRecord
                │     │   └── payload.content[]: TextPart
context         │     ├── AgentAddedRecord
                │     │   ├── payload.content[]: TextPart
                │     │   └── payload.agent: AgentCatalogItem
context         │     ├── AgentRemovedRecord
                │     │   └── payload.content[]: TextPart
context         │     ├── SkillAddedRecord
                │     │   ├── payload.content[]: TextPart
                │     │   └── payload.skill: SkillDefinition
context         │     ├── SkillRemovedRecord
                │     │   └── payload.content[]: TextPart
context         │     ├── MemoryAddedRecord
                │     │   └── payload.content[]: TextPart
context         │     ├── MemoryRemovedRecord
                │     │   └── payload.content[]: TextPart
context         │     ├── EnvironmentRecord
                │     │   ├── payload.content[]: TextPart
                │     │   └── payload.state: EnvironmentState | None
context         │     ├── DateRecord
                │     │   └── payload.content[]: TextPart
setting         │     ├── ModelRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── SamplingRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── SamplingRemovedRecord
setting         │     ├── InferenceRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── InferenceRemovedRecord
setting         │     ├── ProviderParametersRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── ProviderParametersRemovedRecord
setting         │     ├── ResponseFormatRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── ResponseFormatRemovedRecord
setting         │     ├── ToolChoiceRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── ToolChoiceRemovedRecord
setting         │     ├── StreamingRecord
                │     │   └── payload.value: JsonObject
setting         │     ├── StreamingRemovedRecord
tool_catalog    │     ├── ToolAddedRecord
                │     │   └── payload.tool: ToolCatalogItem
tool_catalog    │     ├── ToolRemovedRecord
tool_schema     │     ├── ToolSchemaRecord
                │     │   └── payload.tool: ToolDefinition
operational     │     ├── ParameterOptionRecord
                │     │   └── payload.choices[]: ParameterChoice
operational     │     ├── ParameterOptionRemovedRecord
operational     │     ├── RequestOutputRecord
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       ├── ResourcePart
                │     │       │   └── content[]
                │     │       │       ├── TextPart
                │     │       │       └── ImagePart
                │     │       ├── ReasoningPart
                │     │       └── ToolCallReferencePart
operational     │     ├── ElicitationRequestRecord
operational     │     ├── ElicitationResponseRecord
operational     │     ├── ElicitationCompletedRecord
operational     │     ├── SlashCommandRecord
                │     │   └── payload.owner
                │     │       ├── AgentCommandOwner
                │     │       ├── ClientCommandOwner
                │     │       ├── McpPromptCommandOwner
                │     │       └── McpAppCommandOwner
operational     │     ├── CommandAddedRecord
                │     │   └── payload.owner
                │     │       ├── AgentCommandOwner
                │     │       ├── ClientCommandOwner
                │     │       ├── McpPromptCommandOwner
                │     │       └── McpAppCommandOwner
operational     │     ├── CommandRemovedRecord
operational     │     ├── SlashCommandResultRecord
                │     │   └── payload.content[]
                │     │       ├── TextPart
                │     │       ├── ImagePart
                │     │       └── ResourcePart
                │     │           └── content[]
                │     │               ├── TextPart
                │     │               └── ImagePart
operational     │     ├── PermissionStateRecord
                │     │   └── payload.rules[]: PermissionRule
operational     │     ├── PermissionRequestRecord
                │     │   └── payload.options[]: PermissionOption
operational     │     ├── PermissionResponseRecord
operational     │     ├── PermissionDecisionRecord
operational     │     ├── LlmRequestAttemptRecord
                │     │   └── payload.content[]: TextPart
operational     │     ├── SubagentSessionRecord
operational     │     ├── TokenUsageRecord
                │     │   └── payload.usage: TokenUsage
operational     │     ├── NoticeRecord
                │     │   └── payload.content[]: TextPart
operational     │     ├── TurnEndRecord
operational     │     ├── SessionDeletedRecord
operational     │     └── SessionForkedRecord
```

Связи tools между отдельными записями задаются идентификаторами:

```text
ToolCallReferencePart.call_id                → ToolCallRecord.key
ToolCallRecord.payload.assistant_record_id   → AssistantMessageRecord.record_id
ToolResultRecord.key                         → ToolCallRecord.key  (общий call_id)
ToolResultRecord.payload.call_record_id      → ToolCallRecord.record_id
ToolResultRecord.payload.source_record_id    → ToolResultRecord.record_id  (точный источник content/state)
ToolResultRecord.payload.state.task_id       → задача из API; у исходного вызова совпадает с payload.task_id
ToolReferencePart.catalog_revision           → ToolAddedRecord.record_id
ToolSchemaRecord.payload.catalog_revision    → ToolAddedRecord.record_id
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

### `ToolCallReferencePart`

```python
class ToolCallReferencePart(DataModel):
    """Позиция вызова внутри assistant-сообщения; определение хранится в ToolCallRecord."""

    kind: Literal["tool_call_reference"] = "tool_call_reference"
    call_id: NonEmptyStr
```

```json
{
  "kind": "tool_call_reference",
  "call_id": "call-1"
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

### `ContentPart`, `MessagePart`, `ToolResultPart`

```python
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
```

---

### `ToolCatalogItem`

```python
class ToolCatalogItem(DataModel):
    """Краткое описание инструмента; полное имя хранится в ToolAddedRecord.key."""

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
    """Полная схема инструмента; имя хранится в ToolSchemaRecord.key."""

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

### `ExecutionStatus`

```python
# Общие значения стадий; поле status относится к исполнению своего вызова или задачи.
ExecutionStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled", "interrupted"
]
```

---

### `TaskSnapshot`

```python
class TaskSnapshot(DataModel):
    """Наблюдение задачи task_id из API; revision меняется worker'ом, status относится к задаче."""

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
```

```json
{
  "task_id": "task:tests-1",
  "revision": 2,
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

---

## 3 Модели записей истории

### `RecordBase`

```python
class RecordBase(DataModel):
    """Одна строка истории; производитель задаёт операцию, хранилище — session_id и seq."""

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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "record-1",
  "operation_id": "operation-record-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "record_example",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `MessageRecord`

```python
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
                raise ValueError(f"message role {self.role!r} cannot contain {part.kind!r}")

        return self
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "category-example",
  "operation_id": "operation-category-example",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "category_example",
  "category": "message",
  "role": "user",
  "scope": "session",
  "key": null,
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-15T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Prepared content."
      }
    ]
  }
}
```

---

### `PromptRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "category-example",
  "operation_id": "operation-category-example",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "category_example",
  "category": "prompt",
  "role": "system",
  "scope": "session",
  "key": "prompt.example",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-15T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Prepared content."
      }
    ]
  }
}
```

---

### `ContextRecord`

```python
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "category-example",
  "operation_id": "operation-category-example",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "category_example",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "context.example",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-15T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Prepared content."
      }
    ]
  }
}
```

---

### `SystemPromptAddedRecord`

```python
class SystemPromptAddedRecord(PromptRecord):
    """Сохранённая секция системных инструкций по key; scope определяет область действия."""

    class Payload(PromptRecord.Payload):
        source_uri: NonEmptyStr | None = (
            None  # Источник инструкции; LLM-сборщик не читает его.
        )

    kind: Literal["system_prompt_added"] = "system_prompt_added"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "prompt-add-1",
  "operation_id": "operation-prompt-add-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "system_prompt_added",
  "category": "prompt",
  "role": "system",
  "scope": "session",
  "key": "agent.role",
  "operation": "set",
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
class SystemPromptRemovedRecord(PromptRecord):
    """Снять секцию по key в области scope; для request отмена действует в выбранной операции."""

    kind: Literal["system_prompt_removed"] = "system_prompt_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "prompt-remove-1",
  "operation_id": "operation-prompt-remove-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "system_prompt_removed",
  "category": "prompt",
  "role": "system",
  "scope": "session",
  "key": "agent.role",
  "operation": "remove",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": []
  }
}
```

---

### `SkillAddedRecord`

```python
class SkillAddedRecord(ContextRecord):
    """Каталог навыка с готовым объявлением; полные инструкции сохраняет SkillActivatedRecord."""

    class Payload(ContextRecord.Payload):
        skill: SkillDefinition

    kind: Literal["skill_added"] = "skill_added"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-add-1",
  "operation_id": "operation-skill-add-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "skill_added",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "skill:workspace:review",
  "operation": "set",
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
class SkillRemovedRecord(ContextRecord):
    """Снять доступность навыка; ранее активированные инструкции сохраняются."""

    kind: Literal["skill_removed"] = "skill_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-remove-1",
  "operation_id": "operation-skill-remove-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "skill_removed",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "skill:workspace:review",
  "operation": "remove",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": []
  }
}
```

---

### `SkillActivatedRecord`

```python
class SkillActivatedRecord(PromptRecord):
    """Полученные инструкции навыка с подставленными аргументами; отменяются SystemPromptRemovedRecord по key."""

    class Payload(PromptRecord.Payload):
        name: NonEmptyStr
        source_uri: NonEmptyStr | None = None

    kind: Literal["skill_activated"] = "skill_activated"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "skill-activate-1",
  "operation_id": "operation-skill-activate-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "skill_activated",
  "category": "prompt",
  "role": "system",
  "scope": "session",
  "key": "skill:workspace:review",
  "operation": "set",
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

---

### `MemoryAddedRecord`

```python
class MemoryAddedRecord(ContextRecord):
    """Готовый текст выбранной памяти до замены или отмены; source_uri хранит источник без чтения при сборке."""

    class Payload(ContextRecord.Payload):
        source_uri: NonEmptyStr | None = None

    kind: Literal["memory_added"] = "memory_added"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "memory-add-1",
  "operation_id": "operation-memory-add-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "memory_added",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "memory:workspace:project",
  "operation": "set",
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
class MemoryRemovedRecord(ContextRecord):
    """Прекратить включение выбранной памяти в запросы; исходные записи остаются."""

    kind: Literal["memory_removed"] = "memory_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "memory-remove-1",
  "operation_id": "operation-memory-remove-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "memory_removed",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "memory:workspace:project",
  "operation": "remove",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": []
  }
}
```

---

### `AgentAddedRecord`

```python
class AgentAddedRecord(ContextRecord):
    """Полный снимок одного доступного типа агента и готовое объявление для модели."""

    class Payload(ContextRecord.Payload):
        agent: AgentCatalogItem

    kind: Literal["agent_added"] = "agent_added"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "agent-add-1",
  "operation_id": "operation-agent-add-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "agent_added",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "agent:explore",
  "operation": "set",
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
class AgentRemovedRecord(ContextRecord):
    """Снять тип агента из каталога; уже запущенные задачи продолжают работу."""

    kind: Literal["agent_removed"] = "agent_removed"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "agent-remove-1",
  "operation_id": "operation-agent-remove-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "agent_removed",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "agent:explore",
  "operation": "remove",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": []
  }
}
```

---

### `EnvironmentRecord`

```python
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

```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "environment-1",
  "operation_id": "operation-environment-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "environment",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "environment.executor.shell",
  "operation": "set",
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

---

### `DateRecord`

```python
class DateRecord(ContextRecord):
    """Наблюдённая календарная дата в выбранной зоне; отличается от времени записи."""

    class Payload(ContextRecord.Payload):
        date: CalendarDate
        timezone: NonEmptyStr

    kind: Literal["date"] = "date"
    key: NonEmptyStr = "calendar.current"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "date-1",
  "operation_id": "operation-date-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "date",
  "category": "context",
  "role": "user",
  "scope": "session",
  "key": "calendar.current",
  "operation": "set",
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
  "operation_id": "operation-tool-add-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_added",
  "category": "tool_catalog",
  "role": null,
  "key": "mcp__database__list_tables",
  "operation": "set",
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
    """Инструмент отсутствует в актуальном каталоге; remove также исключает сохранённую схему."""

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
  "operation_id": "operation-tool-remove-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_removed",
  "category": "tool_catalog",
  "role": null,
  "key": "mcp__database__list_tables",
  "operation": "remove",
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
    """Схема, полученная через tool_search; LAST по имени после последнего удаления из каталога."""

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
  "operation_id": "operation-tool-schema-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_schema",
  "category": "tool_schema",
  "role": null,
  "key": "mcp__database__list_tables",
  "operation": "set",
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
  "operation_id": "operation-parameter_option-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "parameter_option",
  "category": "operational",
  "role": null,
  "key": "temperature",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "name": "Температура",
    "description": null,
    "source_kinds": [
      "sampling",
      "sampling_removed"
    ],
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
  "operation_id": "operation-parameter_option_removed-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "parameter_option_removed",
  "category": "operational",
  "role": null,
  "key": "temperature",
  "operation": "remove",
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
    """Атомарный набор полей инференса по key; производитель проверяет значения, сборщик переносит их буквально."""

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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "parameters-1",
  "operation_id": "operation-parameters-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "parameter_example",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "sampling",
  "operation": "set",
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

---

### `ParameterRemovedRecord`

```python
class ParameterRemovedRecord(RecordBase):
    """Отмена набора по key в области scope; пустой payload не содержит новых значений."""

    category: Literal["setting"] = "setting"
    scope: Literal["session", "request"] = "session"
    operation: Literal["remove"] = "remove"
    key: NonEmptyStr
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "parameter_removed_example-1",
  "operation_id": "operation-parameter_removed_example-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "parameter_removed_example",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "sampling",
  "operation": "remove",
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
    """Выбранный инференс и готовые поля выбора модели.

    В категории setting ключ model зарезервирован для выбора модели и подключения.
    payload.value входит в тело запроса; payload.provider адресует личное подключение.
    При provider=None сборка возвращает missing_provider до выбора подключения.
    """

    class Payload(ParameterRecord.Payload):
        provider: NonEmptyStr | None  # Личное подключение; None до выбора пользователем.
        profile_id: NonEmptyStr | None = None  # Выбранный пресет настроек модели.

    kind: Literal["model"] = "model"
    key: Literal["model"] = "model"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "model-1",
  "operation_id": "operation-model-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "model",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "model",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "value": {
      "model": "agent-model"
    },
    "provider": "openai",
    "profile_id": "general"
  }
}
```

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
  "seq": 2,
  "record_id": "sampling-1",
  "operation_id": "user-1",
  "operation_position": 1,
  "parent_operation_id": null,
  "kind": "sampling",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "sampling",
  "operation": "set",
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
    """Параметры выборки: отмена постоянно либо для выбранного operation_id."""

    kind: Literal["sampling_removed"] = "sampling_removed"
    key: Literal["sampling"] = "sampling"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "sampling_removed-1",
  "operation_id": "operation-sampling_removed-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "sampling_removed",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "sampling",
  "operation": "remove",
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
  "operation_id": "operation-inference-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "inference",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "inference",
  "operation": "set",
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
    """Параметры инференса: отмена постоянно либо для выбранного operation_id."""

    kind: Literal["inference_removed"] = "inference_removed"
    key: Literal["inference"] = "inference"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "inference_removed-1",
  "operation_id": "operation-inference_removed-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "inference_removed",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "inference",
  "operation": "remove",
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
  "operation_id": "operation-provider_parameters-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "provider_parameters",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "provider_parameters",
  "operation": "set",
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
    """Параметры провайдера: отмена постоянно либо для выбранного operation_id."""

    kind: Literal["provider_parameters_removed"] = "provider_parameters_removed"
    key: Literal["provider_parameters"] = "provider_parameters"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "provider_parameters_removed-1",
  "operation_id": "operation-provider_parameters_removed-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "provider_parameters_removed",
  "category": "setting",
  "role": null,
  "scope": "session",
  "key": "provider_parameters",
  "operation": "remove",
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
    scope: Literal["request"] = "request"
    key: Literal["response_format"] = "response_format"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "response_format-1",
  "operation_id": "request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "response_format",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "response_format",
  "operation": "set",
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
    scope: Literal["request"] = "request"
    key: Literal["response_format"] = "response_format"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "response_format_removed-1",
  "operation_id": "request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "response_format_removed",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "response_format",
  "operation": "remove",
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
    scope: Literal["request"] = "request"
    key: Literal["tool_choice"] = "tool_choice"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool_choice-1",
  "operation_id": "request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_choice",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "tool_choice",
  "operation": "set",
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
    scope: Literal["request"] = "request"
    key: Literal["tool_choice"] = "tool_choice"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "tool_choice_removed-1",
  "operation_id": "request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_choice_removed",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "tool_choice",
  "operation": "remove",
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
    scope: Literal["request"] = "request"
    key: Literal["streaming"] = "streaming"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "streaming-1",
  "operation_id": "request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "streaming",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "streaming",
  "operation": "set",
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
    scope: Literal["request"] = "request"
    key: Literal["streaming"] = "streaming"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "streaming_removed-1",
  "operation_id": "request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "streaming_removed",
  "category": "setting",
  "role": null,
  "scope": "request",
  "key": "streaming",
  "operation": "remove",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `AssemblyContextRecord`

```python
class AssemblyContextRecord(RecordBase):
    """AssemblyContextRecord указывает сборщику, какую часть существующей переписки приложить к заданию для LLM на компактизацию.
    Само задание находится в RequestMessageRecord, а параметры модели — в записях настроек с тем же operation_id.

    Например, runtime решил выполнить компактизацию. Вся переписка слишком большая, поэтому сначала нужно пересказать только участок с seq от 100 до 200.

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
    │       {"kind": "text", "text": "Перескажи переписку. Сохрани решения и незавершённые задачи."}
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
    В messages попадут выбранная переписка и задание «Перескажи переписку…». Поля mode, from_seq, through_seq в инференс не отправляются: они нужны SQL-сборщику для выбора строк.
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
```

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "assembly-context-1",
  "operation_id": "compact-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "assembly_context",
  "category": "assembly",
  "role": null,
  "scope": "request",
  "key": "context",
  "operation": "set",
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
class RequestMessageRecord(MessageRecord):
    """Служебное задание LLM, например пересказ или классификация разрешения.

    Входит в messages только при выборе своего operation_id; текст готовит производитель.
    """

    kind: Literal["request_message"] = "request_message"
    scope: Literal["request"] = "request"
    role: Literal["user"] = "user"
```

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "request_message-1",
  "operation_id": "compact-1",
  "operation_position": 1,
  "parent_operation_id": null,
  "kind": "request_message",
  "category": "message",
  "role": "user",
  "scope": "request",
  "key": null,
  "operation": "set",
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

### `UserMessageRecord`

```python
class UserMessageRecord(MessageRecord):
    """Ввод пользователя; operation_id общий с приложенными настройками, текст остаётся в диалоге."""

    kind: Literal["user_message"] = "user_message"
    scope: Literal["session"] = "session"
    role: Literal["user"] = "user"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "user-1",
  "operation_id": "user-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "user_message",
  "category": "message",
  "role": "user",
  "scope": "session",
  "key": null,
  "operation": "set",
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
class ResourceMessageRecord(MessageRecord):
    """Материалы, добавленные клиентом или агентом: выбранный фрагмент, файл, ресурс.

    related_message_id связывает материал с предыдущей записью, не ограничивая его одним запросом.
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "resource-message-1",
  "operation_id": "operation-resource-message-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "resource_message",
  "category": "message",
  "role": "user",
  "scope": "session",
  "key": null,
  "operation": "set",
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

---

### `CompactMessageRecord`

```python
class CompactMessageRecord(RecordBase):
    """Успешный пересказ и начало активного контекста; сохраняется атомарно с переносимыми данными.

    reason фиксирует причину сжатия; команда или неудачная попытка пересказа границу не создаёт.
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
  "operation_id": "operation-compact-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "compact_message",
  "category": "compact",
  "role": "user",
  "key": null,
  "operation": "set",
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

---

### `AssistantMessageRecord`

```python
class AssistantMessageRecord(MessageRecord):
    """Ответ LLM; operation_id объединяет текст, определения calls, начальные статусы и usage.

    parent_operation_id адресует попытку. completion отличает полный ответ от частичного.
    record_id выделяется до первого чанка; сохранение полного сообщения выполняется после стрима.
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "assistant-1",
  "operation_id": "answer-1",
  "operation_position": 0,
  "parent_operation_id": "attempt-4",
  "kind": "assistant_message",
  "category": "message",
  "role": "assistant",
  "scope": "session",
  "key": null,
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "content": [
      {
        "kind": "text",
        "text": "Attached a preview and report. I will check the available tables."
      },
      {
        "kind": "image",
        "media_type": "image/png",
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk-A8AAQUBAScY42YAAAAASUVORK5CYII="
      },
      {
        "kind": "resource",
        "name": "report.txt",
        "uri": "file:///workspace/boba/report.txt",
        "media_type": "text/plain",
        "content": [
          {
            "kind": "text",
            "text": "Workspace inspection completed."
          }
        ],
        "line_start": null,
        "line_end": null,
        "truncated": false
      },
      {
        "kind": "tool_call_reference",
        "call_id": "call-1"
      }
    ],
    "completion": "complete"
  }
}
```

---

### `RequestOutputRecord`

```python
class RequestOutputRecord(RecordBase):
    """Ответ служебного LLM-вызова; принадлежит операции ответа, в основной диалог не включается.

    Успешный пересказ затем сохраняется как CompactMessageRecord; ответ классификатора
    обрабатывает политика разрешений. Получение ответа само по себе контекст не меняет.
    """

    kind: Literal["request_output"] = "request_output"
    parent_operation_id: NonEmptyStr = Field(...)
    category: Literal["operational"] = "operational"
    role: Literal["assistant"] = "assistant"
    payload: AssistantMessageRecord.Payload
```

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "request_output-1",
  "operation_id": "summary-answer-1",
  "operation_position": 0,
  "parent_operation_id": "summary-attempt-1",
  "kind": "request_output",
  "category": "operational",
  "role": "assistant",
  "key": null,
  "operation": "set",
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

### `ToolCallRecord`

```python
class ToolCallRecord(RecordBase):
    """Определение вызова из AssistantMessageRecord; входит в ту же операцию ответа и связывает call_id с task_id."""

    class Payload(RecordBase.Payload):
        assistant_record_id: NonEmptyStr  # Сообщение с ToolCallReferencePart для этого call_id.
        name: NonEmptyStr
        arguments: JsonObject
        task_id: NonEmptyStr | None = None  # Исполняющая задача; None для непосредственного ответа.

    kind: Literal["tool_call"] = "tool_call"
    category: Literal["tool_call"] = "tool_call"
    role: Literal["assistant"] = "assistant"
    key: NonEmptyStr  # call_id из ответа LLM; совпадает со ссылкой в assistant.content.
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 2,
  "record_id": "invoke-1",
  "operation_id": "answer-1",
  "operation_position": 1,
  "parent_operation_id": "attempt-4",
  "kind": "tool_call",
  "category": "tool_call",
  "role": "assistant",
  "key": "call-1",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "assistant_record_id": "assistant-1",
    "name": "mcp__database__list_tables",
    "arguments": {},
    "task_id": "task:tables-1"
  }
}
```

---

### `ToolResultRecord`

```python
class ToolResultRecord(RecordBase):
    """Состояние и содержимое ответа вызову key=call_id; сборщик выбирает LAST по seq.

    status относится к вызову; state.status — к наблюдаемой задаче.
    source_record_id закрепляет content/state конкретной записи, сохраняя собственный status.
    """

    class Payload(RecordBase.Payload):
        call_record_id: NonEmptyStr  # ToolCallRecord, которому отвечает эта запись.
        status: ExecutionStatus
        state: TaskSnapshot | None = None  # Прочитанный снимок, если ответ содержит наблюдение задачи.
        source_record_id: NonEmptyStr | None = None  # Ранее сохранённый ToolResultRecord с собственным content/state.
        content: tuple[ToolResultPart, ...] = ()

        @model_validator(mode="after")
        def single_source(self) -> Self:
            if self.source_record_id is None:
                return self

            if self.content:
                raise ValueError("a referenced result supplies the entire content")

            if self.state is not None:
                raise ValueError("a referenced result supplies the entire observed state")

            return self

    kind: Literal["tool_result"] = "tool_result"
    category: Literal["tool_result"] = "tool_result"
    role: Literal["tool"] = "tool"
    key: NonEmptyStr  # call_id; последовательные записи обновляют ответ одному вызову.
    operation: Literal["set"] = "set"
    payload: Payload
```

Наблюдение выполнения исходного вызова `call-1`:

```json
{
  "session_id": "session-1",
  "seq": 6,
  "record_id": "result-running-1",
  "operation_id": "observe-running-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_result",
  "category": "tool_result",
  "role": "tool",
  "key": "call-1",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "call_record_id": "invoke-1",
    "status": "running",
    "state": {
      "task_id": "task:tables-1",
      "revision": 2,
      "description": "List database tables",
      "status": "running",
      "execution_id": "workspace-job-42",
      "child_session_id": null,
      "output_uri": "file:///workspace/tasks/tests-1.log",
      "result": null,
      "error": null
    },
    "source_record_id": null,
    "content": [
      {
        "kind": "text",
        "text": "Task task:tables-1 is running. Check task_status(task_id=\"task:tables-1\")."
      }
    ]
  }
}
```

Ответ вызову `task_status`, у которого свой `call_id=call-status-1`:

```json
{
  "session_id": "session-1",
  "seq": 7,
  "record_id": "result-poll-1",
  "operation_id": "observe-running-1",
  "operation_position": 1,
  "parent_operation_id": null,
  "kind": "tool_result",
  "category": "tool_result",
  "role": "tool",
  "key": "call-status-1",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "call_record_id": "invoke-status-1",
    "status": "completed",
    "state": null,
    "source_record_id": "result-running-1",
    "content": []
  }
}
```

Завершение исходного `call-1`; ответ прежнему опросу остаётся привязан к `result-running-1`:

```json
{
  "session_id": "session-1",
  "seq": 8,
  "record_id": "result-completed-1",
  "operation_id": "observe-completed-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "tool_result",
  "category": "tool_result",
  "role": "tool",
  "key": "call-1",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "call_record_id": "invoke-1",
    "status": "completed",
    "state": {
      "task_id": "task:tables-1",
      "revision": 3,
      "description": "List database tables",
      "status": "completed",
      "execution_id": "workspace-job-42",
      "child_session_id": null,
      "output_uri": "file:///workspace/tasks/tests-1.log",
      "result": {
        "tables": [
          "users"
        ]
      },
      "error": null
    },
    "source_record_id": null,
    "content": [
      {
        "kind": "text",
        "text": "Task task:tables-1 completed. Tables: users."
      }
    ]
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
  "operation_id": "operation-elicit-form-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "elicitation_request",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "elicit-url-1",
  "operation_id": "operation-elicit-url-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "elicitation_request",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
        content: JsonObject | None = None  # Проверенный ответ формы без секретов; для URL — None.

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
  "operation_id": "operation-elicit-response-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "elicitation_response",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
  "operation_id": "operation-elicit-completed-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "elicitation_completed",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
  "operation_id": "operation-compact-command",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "command_added",
  "category": "operational",
  "role": null,
  "key": "compact",
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-14T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "description": "Сжать контекст разговора",
    "owner": {
      "kind": "agent"
    },
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
  "operation_id": "operation-compact-command-removed",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "command_removed",
  "category": "operational",
  "role": null,
  "key": "compact",
  "operation": "remove",
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
  "operation_id": "operation-command-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "slash_command",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "command-result-1",
  "operation_id": "operation-command-result-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "slash_command_result",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    DONT_ASK = "dont_ask"
    BYPASS = "bypass_permissions"
    AUTO = "auto"


class PermissionStateRecord(RecordBase):
    """Полный снимок режима и правил разрешений текущей сессии; действует последний."""

    class Payload(RecordBase.Payload):
        mode: PermissionMode
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
  "operation_id": "operation-permission-state-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "permission_state",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
  "operation_id": "operation-permission_request-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "permission_request",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
        permission_request_id: NonEmptyStr  # record_id конкретного PermissionRequestRecord.
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
  "operation_id": "operation-permission_response-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "permission_response",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
  "operation_id": "operation-permission-decision-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "permission_decision",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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

---

### `NoticeRecord`

```python
class NoticeRecord(RecordBase):
    """Сообщение о работе агента для клиента; уровень и готовый текст сохраняются в истории."""

    class Payload(RecordBase.Payload):
        level: Literal["info", "warning", "error"]
        content: NonEmptyTuple[TextPart]

    kind: Literal["notice"] = "notice"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "notice-1",
  "operation_id": "operation-notice-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "notice",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
class LlmRequestAttemptRecord(RecordBase):
    """Одна завершённая попытка инференса: одна неизменяемая строка отдельной операции.

    Создаётся после ответа, ошибки или отмены; повтор имеет новую операцию и свой итог.
    Ответ ссылается на попытку через parent_operation_id; debug-поля не влияют на сборку.
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
        request_body: JsonObject | None = None  # Готовое тело из сборщика, только для debug.
        response_body: JsonObject | None = None  # Полученный ответ, если сохранён для debug.

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

            if not 200 <= self.status_code < 300:
                raise ValueError("A completed attempt requires a successful HTTP status")

            return self

    kind: Literal["llm_request_attempt"] = "llm_request_attempt"
    category: Literal["operational"] = "operational"
    operation_position: Literal[0] = 0
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "request-attempt-1",
  "operation_id": "attempt-3",
  "operation_position": 0,
  "parent_operation_id": "attempt-2",
  "kind": "llm_request_attempt",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
    "attempt": 3,
    "method": "POST",
    "url": "https://inference.example/request",
    "status_code": 429,
    "duration_ms": 125,
    "error": "rate_limit_exceeded",
    "retry_after_ms": 2000,
    "status": "failed",
    "started_at": "2026-11-13T08:59:59Z",
    "request_body": null,
    "response_body": null
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
        context_through_seq: NonNegativeInt | None = None  # Срез родительской истории; None — без наследования.

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
  "operation_id": "operation-child-origin-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "subagent_session",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-13T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "parent_session_id": "parent-chat-1",
    "parent_task_id": "task:review-1",
    "context_through_seq": 50
  }
}
```

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
    """Расход токенов ответа; operation_id и parent_operation_id совпадают с операцией ответа.

    Сохраняется при наличии счётчиков, включая частичный ответ; в LLM-запрос не входит.
    """

    class Payload(RecordBase.Payload):
        usage: TokenUsage

    kind: Literal["token_usage"] = "token_usage"
    parent_operation_id: NonEmptyStr = Field(...)
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "token-usage-1",
  "operation_id": "answer-1",
  "operation_position": 2,
  "parent_operation_id": "attempt-4",
  "kind": "token_usage",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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
  "operation_id": "operation-turn-end-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "turn_end",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
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

### `SessionDeletedRecord`

```python
class SessionDeletedRecord(RecordBase):
    """Закрывает сессию для новых обращений; сохранённая история остаётся доступна администратору."""

    kind: Literal["session_deleted"] = "session_deleted"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
```

```json
{
  "session_id": "session-1",
  "seq": 1,
  "record_id": "deleted-1",
  "operation_id": "operation-deleted-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "session_deleted",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-15T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {}
}
```

---

### `SessionForkedRecord`

```python
class SessionForkedRecord(RecordBase):
    """Фиксирует источник независимой копии истории; создаётся в новой сессии."""

    class Payload(RecordBase.Payload):
        source_session_id: NonEmptyStr
        through_seq: PositiveInt

    kind: Literal["session_forked"] = "session_forked"
    category: Literal["operational"] = "operational"
    operation: Literal["set"] = "set"
    payload: Payload
```

```json
{
  "session_id": "branch-1",
  "seq": 101,
  "record_id": "forked-1",
  "operation_id": "operation-forked-1",
  "operation_position": 0,
  "parent_operation_id": null,
  "kind": "session_forked",
  "category": "operational",
  "role": null,
  "key": null,
  "operation": "set",
  "copied_from_id": null,
  "created_at": "2026-11-15T09:00:00Z",
  "version": "0.0.33.dev4",
  "payload": {
    "source_session_id": "session-1",
    "through_seq": 100
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
```

---

### `History`

```python
class History(DataModel):
    """Сохранённые строки одной сессии в порядке seq; допускается чтение среза истории."""

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
                raise ValueError("operation_position must be unique within an operation")

            if row.operation_id in parents:
                if parents[row.operation_id] != row.parent_operation_id:
                    raise ValueError("All parts of an operation must have the same parent")

            previous_seq = row.seq
            record_ids.add(row.record_id)
            positions.add(position)
            parents[row.operation_id] = row.parent_operation_id

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
      "operation_id": "user-1",
      "operation_position": 0,
      "parent_operation_id": null,
      "kind": "user_message",
      "category": "message",
      "role": "user",
      "scope": "session",
      "key": null,
      "operation": "set",
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
    },
    {
      "session_id": "session-1",
      "seq": 2,
      "record_id": "sampling-1",
      "operation_id": "user-1",
      "operation_position": 1,
      "parent_operation_id": null,
      "kind": "sampling",
      "category": "setting",
      "role": null,
      "scope": "request",
      "key": "sampling",
      "operation": "set",
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

## 4 Контракты работы с историей

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
```

`append` получает сообщения с `session_id=None`, `seq=None`; сохраняет их по порядку и возвращает
с назначенными адресами. `record_id`, `operation_id`, `operation_position`, `parent_operation_id` и `version`
задаёт производитель до записи. Новые операции сохраняются целиком; части разных операций
могут входить в одну атомарную пачку. Повтор записи использует прежние идентификаторы.
Внутри сессии проверяются уникальность record_id и пары (operation_id, operation_position),
единый parent_operation_id частей и отсутствие циклов родительских связей.
Чтение среза не требует наличия в срезе всех родителей; проверка ссылок выполняется при записи.
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

        Копии получают новые record_id, operation_id и позиции внутри скопированных групп.
        version берётся из summary; copied_from_id указывает исходную запись.
        keep_from_seq=None исключает старый диалог, кроме групп ещё наблюдаемых задач.
        Иначе сохраняется полный суффикс и необходимые группы задач до его начала.
        keep_schema_names=None сохраняет все загруженные схемы; пустое множество — ни одной.
        """
        ...
```

Пересказ и все копии сохраняются одной транзакцией с expected_seq.
Копируются действующие постоянные setting/prompt/context, доступный каталог, выбранные схемы
и сохраняемый диалог. Записи scope=request, снятые состояния, попытки инференса и TokenUsageRecord
не копируются. Копирование токенов не должно повторно учитывать расход ответа.
Перенос не объединяет разные ответы в одну операцию: для каждой копируемой группы выделяется
новый operation_id, её составляющие получают operation_position заново в исходном порядке.
Каждая копия сохраняет copied_from_id; родительская ссылка переводится на копируемого родителя,
а если родитель не копируется — остаётся ссылкой на исходную операцию той же сессии.

Для незавершённой задачи переносятся исходное assistant-сообщение, определения всех
его вызовов и последние ToolResultRecord по call_id, включая результаты завершённых вызовов
этой группы. Дополнительно копируются точные источники source_record_id.
Несколько результатов одного call_id копируются в исходном порядке: старое наблюдение
для ответа task_status не должно затенить более новый результат исходного вызова.
assistant_record_id, call_record_id и source_record_id переводятся на копии соответствующих
строк; call_id и task_id сохраняются. Если исходный вызов не переносится в диалог, копия
используемого наблюдения сохраняет call_record_id исходной строки и служит только источником
по source_record_id. Самостоятельного tool-сообщения такой источник не создаёт.
Копия ToolCallRecord с прежним task_id не создаёт новую задачу. Завершённую обработанную
группу можно заменить пересказом; состояние ещё наблюдаемого вызова терять нельзя.

---

### `TaskCoordinator`

`TaskService` владеет таблицей задач и API; `TaskWorker` исполняет задачи и меняет
состояния через этот API. Оба компонента не читают историю агента. `TaskCoordinator`
связывает задачи с историей; SQL-адаптер собирает результат только из `records`.

```python
class TaskCoordinator(Protocol):
    """Связывает задачи с ToolCallRecord и сохраняет наблюдения как ToolResultRecord вызовов."""

    async def start(
        self,
        session_id: NonEmptyStr,
        message: AssistantMessageRecord,
        calls: tuple[ToolCallRecord, ...],
        *,
        usage: TokenUsageRecord | None = None,
    ) -> tuple[HistoryRecord, ...]:
        """Атомарно сохранить assistant, вызовы, задачи и начальные ToolResultRecord.

        Поставленные задачи получают status=pending; быстрые вызовы — итоговый статус.
        usage при наличии входит в ту же операцию ответа и транзакцию.
        Возвращает сохранённые записи; worker видит новые pending-задачи после commit.
        """
        ...

    async def observe(
        self, session_id: NonEmptyStr, task_id: NonEmptyStr
    ) -> ToolResultRecord:
        """Найти исходный вызов по task_id и сохранить чтение новым ToolResultRecord с key=call_id.

        status совпадает со статусом исполняющей задачи; state содержит снимок с task_id.
        Повторная revision также сохраняется. Отсутствие исходного вызова — HistoryWriteError.
        """
        ...

    async def answer_status(
        self,
        session_id: NonEmptyStr,
        task_id: NonEmptyStr,
        *,
        call_record_id: NonEmptyStr,
    ) -> ToolResultRecord:
        """Ответить конкретному вызову task_status, указанному через call_record_id.

        Если исходный запуск есть в сессии, атомарно сохранить его наблюдение и ответ опросу
        с source_record_id. Иначе снимок внешней задачи хранится прямо в ответе опросу.
        Успешное чтение даёт status=completed независимо от state.status; ошибка чтения — failed.
        """
        ...

    async def reconcile(self, session_id: NonEmptyStr) -> tuple[ToolResultRecord, ...]:
        """Сверить через API задачи незавершённых вызовов и сохранить новые ToolResultRecord."""
        ...
```

Каждый процесс чатбота может создавать задачи и работать worker'ом. Захват задания —
короткая транзакция с `for update skip locked`, токеном владельца и сроком владения.
Обновление проверяет действующий токен; внешнее выполнение не держит транзакцию.
Workers ищут pending-задачи при старте, по уведомлению и периодически: потеря NOTIFY
не должна оставлять задание без исполнителя.
Владение задачей независимо от владения диалогом. Доступ проверяется по пользователю;
MCP-задача также требует доступа worker'а к соответствующему подключению клиента.

При `task_add` координатор автоматически начинает наблюдение. На процесс выделяется
одно LISTEN-соединение; после подтверждения LISTEN координатор регистрирует task_id
и читает состояние. Worker сохраняет состояние, увеличивает revision и вызывает
`pg_notify` в одной транзакции; уведомление содержит только task_id и revision.
Получив уведомление, координатор заново читает `task_status` и сохраняет наблюдение.
После старта, обрыва соединения и периодически выполняется сверка по таблице задач.
Уведомление не подменяет чтение и не гарантирует доставку каждого промежуточного состояния.

Runtime решает, когда продолжать генерацию после терминального наблюдения.
Курсор обработки, владение диалогом и восстановление незавершённой обработки относятся
к состоянию runtime; сборщик их не читает и не выводит обработанность задачи из наличия
AssistantMessageRecord или технической записи попытки. Состояние задачи восстанавливается
через API, а каждое прочитанное наблюдение сохраняется до использования в сборке.
Наличие только pending/running не запускает бесконечный опрос LLM.
Автоматическое наблюдение прекращается после сохранения терминального ToolResultRecord;
явный вызов `task_status` по-прежнему читает таблицу и сохраняет новое наблюдение и свой ответ.

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

## 5 Хранение и SQL-адаптер

Одна таблица `boba.records`; ключ строки — `(session_id, seq)`. Поля модели и её JSON
сохраняются в одноимённые столбцы. `scope` остаётся отдельным nullable-столбцом:
для message/prompt/context/setting/assembly обязательны session или request,
для остальных категорий — NULL. При записи отсутствие поля в JSON соответствует NULL;
при чтении SQL исключает ключ scope из JSON для записей с NULL в этом столбце.
Остальные nullable-поля возвращаются согласно модели; преобразование в Python не требуется.
DDL применяется только миграциями агента.
Описанный здесь контракт — цель для отдельного обновления SQL и схемы хранения.

Запись назначает seq под блокировкой сессии. Параметры — session_id, пачка моделей,
expected_seq либо null. Проверяются форма записей, уникальность идентификаторов и позиций,
согласованность групп и ссылок. Ссылки assistant на определения tools могут разрешаться
внутри той же пачки. Ошибка откатывает всю пачку; неизвестный исход commit проверяется
по прежним record_id. Точная повторная запись возвращает существующие строки;
изменённое содержимое под тем же ID — record_conflict. Чтение не блокирует запись.

### Отбор истории

Параметры SQL задаются в одном CTE `params`: `session_id`, `operation_id`, `through_seq`.
Операция выбирает scope=request; срез выбирает доступные строки. Эти параметры независимы.
Через operation_id нельзя неявно выбирать seq, последнее пользовательское сообщение
или родительскую операцию. При operation_id=null отсутствуют разовые входы и переопределения.
При through_seq=null фиксируется последний seq в том же снимке БД.

Отсутствие технической записи о попытке не препятствует сборке. Повторный вызов сборщика
не меняет действие scope=request. Срез вне `0..last_seq` — invalid_history_range;
указанная операция без записей в срезе — unknown_operation. Неполный срез может дать
missing_tool_result или missing_model. SQL возвращает кандидат и исходные метаданные,
а чистый валидатор выдаёт конкретные ошибки без исправления истории.

### Контракты категорий

- `assembly`: LAST по key в выбранной операции. Ключ context задаёт mode и диапазон
  переписки. Строка влияет на отбор, но не входит в messages или параметры инференса.
- `message`: SUM сообщений scope=session в диапазоне и scope=request выбранной операции.
  Служебное задание использует тот же контракт сообщения, что пользовательский ввод.
- `compact`: последняя граница в срезе задаёт начало активного контекста; её content
  входит первым сообщением диалога. Сама операция пересказа не создаёт границу заранее.
- `prompt`, `context`, `setting`: LAST по `(category, key)` с приоритетом выбранной
  операции над постоянным состоянием. Remove исключает ключ; пустой объект не отменяет набор.
  Для prompt удаление неактивной секции — ошибка. Состояния после компактизации берутся
  из перенесённого снимка и последующих изменений.
- `tool_call`: определения вызовов из включённых assistant-сообщений. Каждая ссылка
  ToolCallReferencePart должна иметь ровно одно соответствующее определение.
- `tool_result`: LAST по key=call_id среди записей выбранного среза, для каждого включённого
  вызова. status не меняет правило выбора: pending/running и терминальные стадии обрабатываются
  одинаково. content/state берутся из выбранной записи либо из точного source_record_id.
  Источник читается независимо от LAST по его собственному call_id; status ответа не наследуется.
- `tool_catalog`: LAST доступности по имени. Полные MCP-имена имеют префикс
  mcp__<server>__<tool>; отдельного поля сервера не требуется. Изменение MCP сохраняет
  разницу имён: remove только для реально исчезнувших tools, add для появившихся.
- `tool_schema`: LAST схемы по имени после последнего remove каталога. Повторный add
  без удаления сохраняет схему; add после remove не возвращает старую схему.
  catalog_revision адресует add того же имени внутри непрерывной доступности.
  Изменение схемы при сохранении имени обнаруживает повторное получение определения;
  один список имён MCP не позволяет определить такое изменение.
- `operational`: учёт исполнения и действия для других потребителей истории.
  Попытки инференса, TokenUsageRecord, разрешения и уведомления не участвуют в LLM-сборке.

Новый kind внутри этих контрактов не требует перечисления в SQL. Неизвестная категория
в активном срезе — unknown_category; неизвестная часть content — unsupported_content.
LAST/SUM сначала выбирают метаданные, затем читают payload выбранных строк.
Порядок частей извлекается из массива content; operation_position используется
для составляющих одной операции, а seq — для доступности и порядка записей истории.

### Сборка тела и проверка

В setting payload.value — непустой атомарный набор готовых полей инференса.
Производитель проверяет допустимость model/sampling/inference и специфичных параметров;
сборщик переносит значения буквально. LAST не дополняет набор полями прежней версии.
Конфликт выходных полей разных наборов — duplicate_parameter. Пустые вложенные объекты,
null, false и 0 могут быть обычными значениями; удаление набора имеет отдельную модель.

Ключ model зарезервирован для ModelRecord: payload.value входит в тело запроса,
payload.provider адресует личное подключение, profile_id описывает выбранный пресет.
Отсутствие модели или подключения — missing_model или missing_provider. URL и credentials
берутся транспортом из выбранного подключения и не дополняют собранное тело.

Prompt идёт первым разделом, диалог упорядочивается по seq. SQL размещает все результаты
tools сразу после содержащего вызовы assistant-сообщения, в порядке ToolCallReferencePart.
Фактический seq результата задаёт его доступность в срезе. Пользовательские сообщения
или изменения контекста между вызовом и результатом группу не разрывают. Все calls
должны иметь результаты; никакой заглушки без сохранённого статуса сборщик не создаёт.

ToolResultRecord содержит TextPart для pending/running и сохранённый результат после
завершения. JSON состояния можно хранить как готовый текст; сборщик не сериализует
TaskSnapshot заново. Содержимое результата может включать изображения, ресурсы и ссылки
найденных tools. ToolReferencePart передаёт имя; полная схема берётся из ToolSchemaRecord.
Один снимок может обслужить исходный запуск и отдельный вызов task_status — с разными call_id.

call_record_id адресует ToolCallRecord с тем же key в этой сессии. source_record_id адресует
ранее сохранённый ToolResultRecord этой сессии с собственным content/state, без следующей ссылки.
Ссылка на себя, цепочка ссылок, отсутствующий источник или несовпадение call_id — явная ошибка.
При использовании source_record_id поля content и state текущего ответа пусты; источник
поставляет только содержимое и наблюдение, а status и принадлежность вызову остаются у ответа.
Успешный опрос задачи в состоянии failed имеет status=completed; сбой самого опроса — failed.

Текст передаётся буквально; формат изображений и структурных частей преобразует адаптер.
Незагруженный ResourcePart.content=null остаётся ссылкой: сборщик не открывает URI.
Подписанный reasoning требует поддержки провайдера; неподдерживаемая подпись — ошибка.
SQL возвращает provider, кандидат request и validation_data одним снимком БД.
OpenAiRequestValidator проверяет связи и полноту групп, части, области действия,
доступность схем и конфликты выходных параметров. Отправка разрешена после require_valid.

### Служебные операции и компактизация

Производитель сохраняет AssemblyContextRecord, RequestMessageRecord и нужные параметры
под одним operation_id. Для пересказа mode=conversation добавляет выбранную переписку;
from_seq/through_seq ограничивают только основной диалог и объявления context.
Настройки, инструкции и собственные входы операции не обрезаются этим диапазоном.
Границы диапазона должны лежать внутри активного контекста выбранного среза.
Если AssemblyContextRecord отсутствует, используется весь активный диалог.

Для mode=isolated берутся собственные message/prompt/context выбранной операции
и настройки сессии с переопределениями. Основной диалог, общие инструкции, объявления
контекста и tools исключаются. Связанные tool_call/result выбираются только для включённых
assistant-сообщений; ограничение диапазона не разрешает потерять часть группы результатов.

RequestMessageRecord имеет category=message и scope=request. Его текст, настройки
и диапазон подготавливаются до сборки; сборщик не определяет, почему операция создана.
RequestOutputRecord и TokenUsageRecord сохраняются в одной операции ответа, родитель —
конкретная завершённая попытка инференса. Служебный ответ не становится частью обычного
диалога. Успешный пересказ отдельно сохраняется как CompactMessageRecord; ответ
классификатора используется производителем PermissionDecisionRecord.

Техническая запись попытки не делает scope=request постоянным и не прекращает его действие.
Обычная сборка другой операции не включает служебные входы из-за несовпадения operation_id.
При сохранении нового снимка контекста используется expected_seq: новые сообщения
не должны теряться при конкурентной компактизации.

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

### `OpenAiRequestDraft`

```python
class OpenAiRequestDraft(DataModel):
    """Результат SQL до проверки; отсутствующий provider становится ошибкой валидации."""

    provider: NonEmptyStr | None
    request: JsonObject
    validation_data: JsonObject
```

```json
{
  "provider": "personal-openai",
  "request": {
    "model": "example-model",
    "messages": [{"role": "user", "content": "Hello"}]
  },
  "validation_data": {}
}
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
    """Выполнить SQL для выбранного среза и операции, затем проверить кандидат чистым валидатором."""

    async def build(
        self,
        session_id: str,
        *,
        operation_id: str | None = None,
        through_seq: NonNegativeInt | None = None,
    ) -> OpenAiRequestDraft:
        """operation_id выбирает разовые входы; through_seq ограничивает историю включительно.

        Отсутствие operation_id выбирает только входы сессии; through_seq=None — текущий срез.
        Сборка не требует записи попытки, ничего не сохраняет и не запускает инференс.
        """
        ...
```
