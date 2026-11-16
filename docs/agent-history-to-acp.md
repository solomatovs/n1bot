# SQL-адаптер истории в ACP v1 и v2

Источник — `boba.records`. Модели хранения: [история агента](agent-history-and-request-assembly.md).
Исполняемые модели и проверки: [acp_validator.py](../packages/apps/boba-agent/src/boba/agent/acp_validator.py).
SQL создаёт данные протокола; отправка, capabilities и координация исполнения принадлежат транспорту.

Контракт закреплён на [ACP v2, revision f49d27a9](https://github.com/agentclientprotocol/agent-client-protocol/blob/f49d27a9eab068cd9a48901237f256da34fe0183/schema/v2/schema.json).
Это draft. Установленный `agent-client-protocol==1.0.0rc3` ещё не содержит
`replay_complete` и `subagent_update`: их используемые поля описаны локальными моделями.
Подключение SQL к серверу требует согласовать эту ревизию с SDK и клиентом.

Версия согласуется через `initialize` для каждого подключения. `/acp` поддерживает
v1 и v2; `/acp/v1` ограничивает выбор версией 1 для проверки клиентов с автоматическим
согласованием. История и исполнитель сессии общие для обеих версий.
В v1 сохранённый текст передаётся через `*_message_chunk`, вызов tool начинается
с `tool_call`, а `session/prompt` завершается ответом `stopReason` после обработки ввода.
В v2 используются итоговые сообщения с ID и `state_update`.
Текст, уже отправленный живыми чанками, SQL повторно в v1 не публикует.

`AcpV1` и `AcpV2` реализуют API соответствующей версии SDK; `AgentProtocolRouter`
согласует версию один раз на подключении. SQL независимы: `sql/acp/v1/` и `sql/acp/v2/`.
Далее описаны запросы v2. В `v1/history.sql` пятый параметр `streamed_record_ids` —
массив ID ответов, уже переданных чанками; для replay передаётся пустой массив.
`v1/options.sql` возвращает `id`; `v2/options.sql` — `configId`.

`session/delete` сохраняет `SessionDeletedRecord`; история остаётся в `records`,
а список и открытие чата исключают удалённую сессию. `session/fork` копирует
завершённую историю SQL-запросом и добавляет `SessionForkedRecord` в новую сессию.
Вызовы tools и фоновые задачи из копии не запускаются повторно.

`sql/acp/v1/session.sql` собирает ответы new/load/resume/fork и состояние `modes`;
`sql/acp/v2/session.sql` — ответы new/resume/fork. Параметры: ID сессии,
JSON `update` из options.sql и, для v2, JSON `update` из commands.sql.
Поле `mode` изменяет `PermissionStateRecord`: v1 принимает `session/set_mode`,
обе версии принимают `session/set_config_option`. Перечень режимов и текущий выбор
берутся из истории; v1 дополнительно публикует `current_mode_update`.

`sql/acp/v1/providers.sql` и `sql/acp/v2/providers.sql` принимают только ID владельца.
Таблица `boba.providers` хранит личные подключения: обязательный `owner_id`
и произвольный `provider_id` уникальны в паре. Новый пользователь получает пустой список
и добавляет подключение через `providers/set` (`providerId`, `apiType`, `baseUrl`,
`headers`). Ответ `providers/list` содержит тип API, адрес и доступность подключения
без credentials. Настройки сохраняются между подключениями.

Параметр чата `provider` выбирает личное подключение независимо от параметра `model`.
Первое добавленное подключение выбирается в чатах без провайдера. Добавление остальных
подключений не меняет уже сделанный выбор; отключённое подключение возвращает ошибку
при отправке LLM-запроса. До добавления провайдера чат можно создать и восстановить.

Служба получает ключ шифрования через общий `ValueCipherConfig`:

```toml
[agent.providers]
cipher = "${keys.encryption}"
```

`keys.encryption` задаёт 32 байта в base64. `SecretCipher` шифрует поля `SecretStr`
в JSONB-конфигурации. Настройка хранится отдельно от истории диалога;
выбор модели и параметры LLM-запроса по-прежнему фиксируются в `records`.

## Как использовать запросы

Тестовая база — **`aid_ss_agent_operations`**, схема **`boba`**. Сначала выполнить
миграции агента командой `boba migrate`, затем [fixtures.sql](../experiments/history-sql/fixtures.sql)
и [openai_fixtures.sql](../experiments/history-sql/openai_fixtures.sql).
В рабочей базе тестовые данные не устанавливаются.

Параметры задаются только в начальном CTE `params`. `after_seq` исключителен,
`through_seq` включителен; `null` означает начало или текущий конец истории.
`record_id` выбирает конкретную запись. `operation_id` группирует части операции,
но ACP-запросы не принимают `operation_id`: переписка восстанавливается по порядку `seq`.

### История

[`v2/history.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v2/history.sql)
возвращает `notifications` с готовыми `session/update`, границами и атрибутами проверки.
Используется для resume и публикации новых сохранённых записей. Копии с `copied_from_id`
не показываются повторно. `ToolCallReferencePart` разворачивается через `ToolCallRecord`;
каждый `ToolResultRecord` обновляет тот же `toolCallId`. `source_record_id` выбирает
содержимое конкретного снимка, а не последнее состояние исходного вызова.

Параметры для ручного запуска:

- `'sql-acp-demo', null, null, true` — вся переписка с сохранёнными предупреждениями.
- `'sql-acp-demo', 5, 8, true` — assistant с вызовом и начальный pending.
- `'sql-acp-demo', 10, 11, true` — завершение того же вызова.
- `'sql-acp-demo', 17, 19, true` — два прошлых предупреждения.
- `'sql-acp-demo', 17, 19, false` — предупреждения исключены из общего потока;
  для живой публикации вызывается `notice.sql`.
- `'openai-tasks', null, null, true` — несколько вызовов, сообщения пользователя
  во время исполнения и закреплённый результат опроса статуса.
- `'openai-task-compact', null, null, true` — история с копиями при компактизации.
- `'openai-media', null, null, true` — смешанное содержимое.

[`v1/history.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v1/history.sql)
использует те же первые четыре параметра и пятый `streamed_record_ids::text[]`.
Для resume передаётся `'{}'::text[]`; при живой публикации — ID ответов,
текст которых уже отправлен чанками. Реализация и JSON-модели v1 независимы от v2.

### Настройки, команды и статистика

[`v2/state.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v2/state.sql)
собирает постоянные настройки, разрешения и расход токенов. Настройки `scope=request`
не входят в постоянное состояние. Usage группируется по операции ответа.
Параметры: `'sql-acp-demo', null`; срез `'sql-acp-demo', 12` уже содержит usage.

[`v2/options.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v2/options.sql)
и [`v1/options.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v1/options.sql)
собирают варианты изменения параметров и текущие значения из истории.
Параметры `'sql-acp-demo', null` показывают выбор `temperature=0.2`.

[`v2/commands.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v2/commands.sql)
и [`v1/commands.sql`](../packages/apps/boba-agent/src/boba/agent/sql/acp/v1/commands.sql)
собирают действующие slash-команды. Параметры `'sql-acp-demo', 14` дают пустой список,
`'sql-acp-demo', 15` — команду `compact`.

### Уведомления, вопросы и разрешения

`notice.sql` принимает `session_id`, `record_id` и возвращает готовое уведомление
соответствующей версии ACP. `'sql-acp-demo', 'warning'` и `'sql-acp-demo', 'retry'`
дают warning; `'sql-acp-demo', 'attempt'` — успешная попытка без предупреждения.
SQL не отправляет уведомление самостоятельно.

`permission.sql` принимает `session_id` и `record_id` вопроса.
`'sql-acp-actions', 'permission'` собирает запрос разрешения для сохранённого
незавершённого tool call. `'sql-acp-demo', 'permission'` должен быть отклонён
валидатором: в истории уже есть ответ и завершение вызова. Начальный pending-результат
не считается завершением вызова.

`elicitation.sql` также принимает только `session_id` и `record_id` вопроса.
`'sql-acp-actions', 'form'` собирает форму; `'sql-acp-actions', 'url'` — запрос
внешнего действия. `'sql-acp-demo', 'question'` уже закрыт ответом и отклоняется
валидатором. Восстановление истории не запускает вопросы заново.
Credentials в историю не записываются.

Для обеих операций используются отдельные файлы в `sql/acp/v1/` и `sql/acp/v2/`.
Транспорт проверяет возможности клиента и передаёт валидированный JSON из SQL в SDK.

### Ответы управления сессией

`v1/prompt.sql` принимает `session_id`, `record_id` сохранённого пользовательского
сообщения. После завершения runtime возвращает `stopReason` по `TurnEndRecord`
и завершению ответа. В v2 результат `session/prompt` содержит ID принятого сообщения;
результаты исполнения приходят отдельными уведомлениями.

`session.sql` каждой версии формирует ответы создания и восстановления сессии;
принимает ID сессии и результаты SQL-сборки options, а v2 также commands.
`providers.sql` принимает ID владельца и возвращает его подключения к инференсу.
Эти запросы не принимают входной текст диалога и не запускают генерацию.

При resume транспорт сначала подключает подписчика к сессии и фиксирует текущий `seq`,
затем передаёт историю до этого `seq` и текущие options/commands. Последующие изменения
публикуются после выбранной границы. Потоковые дельты передаются Python-моделями без
записи в PostgreSQL.

## Содержимое и сообщения

### Сохранённые части

Используются стандартные блоки SDK. Данные ресурсов берутся из истории; SQL не открывает URI.
Порядок — порядок массива `payload.content`, включая дочерние части ресурсов.

```python
from typing import Annotated, Literal
from acp.experimental.v2 import schema
from pydantic import Field, JsonValue

ContentBlock = Annotated[
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock,
    Field(discriminator="type"),
]
```

```json
[
  {"type": "text", "text": "Привет"},
  {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="},
  {"type": "resource_link", "name": "skill.md", "uri": "https://example.com/skill.md"},
  {"type": "resource", "resource": {
    "uri": "file:///workspace/app.py", "mimeType": "text/x-python", "text": "print(1)\n"
  }}
]
```

`ResourcePart.content=null` даёт `resource_link`; пустой массив — ресурс с пустым текстом.
Загруженные части становятся `resource` с `text` или `blob`; base64 преобразуется в стандартный
алфавит. Поля выделения `line_start/line_end/truncated` остаются в истории: у ACP resource
нет равнозначных полей. `tool_reference` отображается текстом имени инструмента.

### Поток ответа и несколько подключений

Чанки обязательно передаются через ACP: `agent_message_chunk` для ответа,
`agent_thought_chunk` для показываемых рассуждений. Это стандартные варианты
[ACP v2](https://agentclientprotocol.com/protocol/v2/prompt-lifecycle).
SQL преобразует сохранённые записи; потоковый адаптер преобразует ещё не сохранённые
дельты провайдера. SQL на каждый чанк не выполняется.

```python
class MessageChunk(DataModel):
    """Временное событие ACP; не модель строки records."""

    session_update: Literal["agent_message_chunk", "agent_thought_chunk"] = Field(
        alias="sessionUpdate"
    )
    message_id: str = Field(alias="messageId", min_length=1)
    content: ContentBlock
```

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "session-1",
    "update": {
      "sessionUpdate": "agent_message_chunk",
      "messageId": "[\"assistant-1\", [1]]",
      "content": {"type": "text", "text": "Проверяю"}
    }
  }
}
```

`record_id` ответа назначается до первого чанка. `messageId` части совпадает с ID,
который `v2/history.sql` получает из пары `[record_id, position]`; нумерация частей
начинается с 1 и не меняется при сохранении итогового сообщения. Дельты одной текстовой
части накапливаются в одной части `content`, не создавая отдельный элемент на каждый токен.
После commit итоговое `agent_message` с тем же `messageId` заменяет накопленный текст.
Неудачная попытка генерации получает отдельный ID; её текст не смешивается с повтором.

Одна исполняющая сессия агента обслуживает несколько авторизованных ACP-подключений.
Каждый подписчик имеет ограниченный буфер отправки; медленный подписчик не останавливает
LLM и других клиентов. При переполнении клиент переподключается и восстанавливает состояние.
Отключение одного подписчика не означает отмену работы сессии.

При resume агент согласует подписку с границей сохранённой истории: сначала replay до
фиксированного seq, затем `replay_complete`, снимок незавершённых сообщений из памяти
и последующие дельты. Во время синхронизации исходящие события для этого клиента
буферизуются; уже отражённые снимком дельты повторно не отправляются.
Снимок незавершённого сообщения — обычный `agent_message`/`agent_thought` с полным content,
а не повтор всех старых чанков. После перезапуска процесса доступны только сохранённые записи.

В PostgreSQL хранятся итоговые сообщения, принятый при отмене частичный ответ, usage
и результаты выполнения. Таблиц live-токенов нет. `boba-aid` получает историю и поток
исключительно через ACP. При нескольких процессах агент направляет подписчиков к
владельцу исполняющей сессии; второй исполнитель для той же сессии не запускается.
Вопросы permission/elicitation направляются одному выбранному клиенту, а не всем
подписчикам как независимые действия.

```python
class MessageUpdate(DataModel):
    """Complete persisted content; replacing the same ID is idempotent."""

    session_update: Literal["user_message", "agent_message", "agent_thought"] = Field(
        alias="sessionUpdate"
    )
    message_id: str = Field(alias="messageId")
    content: tuple[ContentBlock, ...]
```

```json
{
  "sessionUpdate": "agent_message",
  "messageId": "[\"assistant\", [2]]",
  "content": [
    {
      "type": "text",
      "text": "Читаю."
    }
  ]
}
```

`user_message` и `resource_message` истории → `user_message`; текст ассистента →
`agent_message`, reasoning → `agent_thought`. Подпись reasoning в ACP не отправляется.
Последовательность `text → tool_call → text` сохраняется; соседние части одного канала
собираются в одну группу. ID сообщения агента — JSON `[record_id, position]` как строка.
`position` вычисляется из индексов массива и в истории не хранится.

Slash-команда отображается как `user_message` с сохранённым текстом, её результат —
`agent_message` с сохранённым content. Повторной маршрутизации или исполнения при replay нет.
Пустой результат команды даёт пустой content.

```python
class ToolCallUpdate(DataModel):
    """The result patches the original call using the same toolCallId."""

    session_update: Literal["tool_call_update"] = Field(
        default="tool_call_update", alias="sessionUpdate"
    )
    tool_call_id: str = Field(alias="toolCallId")
    name: str | None = None
    title: str | None = None
    kind: Literal["other"] | None = None
    status: Literal["pending", "in_progress", "completed", "failed"]
    raw_input: dict[str, JsonValue] | None = Field(default=None, alias="rawInput")
    content: tuple[schema.ContentToolCallContent, ...] | None = None
```

```json
{
  "sessionUpdate": "tool_call_update",
  "toolCallId": "call-read",
  "name": "mcp__workspace__read",
  "title": "mcp__workspace__read",
  "kind": "other",
  "status": "pending",
  "rawInput": {
    "path": "app.py",
    "optional": null
  }
}
```

SQL раскрывает ToolCallReferencePart через ToolCallRecord в одно событие вызова; `ToolResultRecord.key` задаёт тот же `toolCallId` для ответа.
`ToolCallRecord.payload.task_id` связывает вызов с задачей; `ToolStatusRecord` обновляет
состояние этого вызова. SQL использует сохранённое наблюдение и не читает таблицу задач.
Ответ отдельного `task_status` использует собственный toolCallId и точную status_record_id.
У обычного ответа `is_error=false/true` преобразуется в `completed/failed`.
У исходного запуска наблюдение pending/running даёт pending, completed — completed,
failed/cancelled/interrupted — failed с сохранённым пояснением. Успешный опрос task_status
даёт completed для самого опроса, даже когда опрашиваемая задача ещё выполняется.

```json
{
  "sessionUpdate": "tool_call_update", "toolCallId": "call-read", "status": "completed",
  "content": [{"type": "content", "content": {"type": "text", "text": "print(1)\n"}}]
}
```

```python
class CompactionUpdate(DataModel):
    session_update: Literal["compaction_update"] = Field(
        default="compaction_update", alias="sessionUpdate"
    )
    compaction_id: str = Field(alias="compactionId")
    status: Literal["completed"] = "completed"
    summary: tuple[ContentBlock, ...]
```

```json
{
  "sessionUpdate": "compaction_update",
  "compactionId": "compact",
  "status": "completed",
  "summary": [
    {
      "type": "text",
      "text": "Файл прочитан."
    }
  ]
}
```

ACP читает весь сохранённый диалог, включая сообщения до компактизации.
Строки с `copied_from_id` исключаются: копирование для LLM не создаёт второй элемент чата.
Ссылка должна вести на предыдущую запись того же kind/category в этой сессии.

```python
class SubagentUpdate(DataModel):
    session_update: Literal["subagent_update"] = Field(
        default="subagent_update", alias="sessionUpdate"
    )
    session_id: str = Field(alias="sessionId")
```

```json
{
  "sessionUpdate": "subagent_update",
  "sessionId": "sql-subagent-demo"
}
```

Источник — `ToolStatusRecord.payload.state.child_session_id` в родительской сессии.
Ассоциация передаётся перед текстом состояния задачи; повторное объявление идемпотентно.
История ребёнка читается отдельно и отправляется с его `sessionId` только после объявления
ассоциации. Транспорт проверяет владение сессией; SQL не даёт прав на чужую историю.
Текущие состояния ребёнка и разрешённые клиентские операции из прошлых записей не выводятся.

## Уведомления и вопросы

Исторические `notice` и неуспешные `llm_request_attempt`
дают обычный `agent_message` с сохранённым текстом и уровнем. `turn_end` и записи
`permission_request`, `permission_response`, `permission_decision` не создают реплик чата.
Разрешение запрашивается через `session/request_permission`; результат исполнения tool
отображается через `tool_call_update`. Записи elicitation показываются без повторного
запуска действий. Replay не создаёт новых предупреждений или вопросов.
Наблюдения задач отображаются как обновления соответствующих tools; отдельные
реплики пользователя и notice для каждого прочитанного снимка не создаются.

```json
{
  "sessionUpdate": "agent_message", "messageId": "[\"warning\", [1]]",
  "content": [
    {"type": "text", "text": "[warning] Временная задержка."},
    {"type": "resource", "resource": {
      "uri": "urn:boba:history:7761726e696e67", "mimeType": "application/json",
      "text": "{\"level\": \"warning\", \"role\": null}"
    }}
  ]
}
```

Новые уведомления используют `schema.SessionNotice`:

```python
class LiveNotice(DataModel):
    session_update: Literal["notice"] = Field(default="notice", alias="sessionUpdate")
    severity: Literal["info", "warning", "error"]
    title: str
```

```json
{"sessionUpdate": "notice", "severity": "warning", "title": "Временная задержка."}
```

Уведомления live-only; протокол рекомендует не повторять прошлые notice при восстановлении.
У истории вопросов нет побочного эффекта: живой `elicitation/create` готовится отдельным
запросом и проверяется отдельно. [Replay contents](https://agentclientprotocol.com/protocol/v2/session-setup#replay-contents).

```python
class AcpElicitationDraft(DataModel):
    session_id: str
    record_id: str
    seq: PositiveInt | None
    response_count: NonNegativeInt
    completion_count: NonNegativeInt
    method: Literal["elicitation/create"]
    required_mode: Literal["form", "url"] | None
    params: dict[str, JsonValue] | None
```

```json
{
  "session_id": "sql-acp-demo",
  "record_id": "pending-form",
  "seq": 14,
  "response_count": 0,
  "completion_count": 0,
  "method": "elicitation/create",
  "required_mode": "form",
  "params": {
    "sessionId": "sql-acp-demo",
    "mode": "form",
    "message": "Выберите формат",
    "requestedSchema": {
      "type": "object",
      "properties": {
        "format": {
          "type": "string",
          "enum": [
            "short",
            "full"
          ]
        }
      },
      "required": [
        "format"
      ]
    }
  }
}
```

`params` проверяется SDK-моделью `CreateFormSessionElicitationRequest` либо
`CreateUrlSessionElicitationRequest`. Для URL: `url`, `elicitationId=record_id`; для формы:
`requestedSchema` без изменения JSON. `toolCallId` добавляется только при наличии связи.

```json
{
  "sessionId": "sql-acp-demo", "mode": "url", "message": "Подключите доступ",
  "url": "https://example.com/connect", "elicitationId": "url"
}
```

Отправитель проверяет согласованный режим, актуальность действия и отсутствие другого
отправителя; RPC ID назначает транспорт. Наличие записи без ответа не означает разрешения
автоматически отправить её снова. Credentials остаются во внешнем процессе.

## Сохранённые настройки

`v2/state.sql` берёт постоянные setting по LAST внутри текущего контекста; remove снимает
категорию целиком. Настройки scope=request исключаются. Разрешения — последний сохранённый
снимок; token usage — последние счётчики каждой операции ответа, суммированные по именам.
Это статистика журнала, не размер текущего контекстного окна ACP `usage_update`.

```python
class SavedSetting(DataModel):
    key: str
    record_id: str = Field(alias="recordId")
    value: dict[str, JsonValue]
    provider: str | None
```

```json
{
  "key": "model",
  "recordId": "copy-model",
  "value": {
    "model": "test-model"
  },
  "provider": "openai"
}
```

```python
class SavedPermissionRule(DataModel):
    tool: str
    decision: Literal["allow", "deny", "ask"]
```

```json
{
  "tool": "mcp__workspace__read",
  "decision": "ask"
}
```

```python
class SavedPermissions(DataModel):
    key: str | None
    mode: Literal[
        "default", "accept_edits", "plan", "dont_ask", "bypass_permissions", "auto"
    ]
    record_id: str = Field(alias="recordId")
    rules: tuple[SavedPermissionRule, ...]
```

```json
{
  "key": null,
  "recordId": "permission",
  "mode": "default",
  "rules": [
    {
      "tool": "mcp__workspace__read",
      "decision": "ask"
    }
  ]
}
```

```python
class SavedSessionState(DataModel):
    through_seq: NonNegativeInt = Field(alias="throughSeq")
    settings: tuple[SavedSetting, ...]
    permissions: tuple[SavedPermissions, ...]
    usage: dict[str, NonNegativeInt]
```

```json
{
  "throughSeq": 25,
  "settings": [
    {
      "key": "model",
      "recordId": "copy-model",
      "value": {
        "model": "test-model"
      },
      "provider": "openai"
    }
  ],
  "permissions": [],
  "usage": {
    "input_tokens": 12,
    "output_tokens": 3
  }
}
```

Это содержимое стандартного ресурса `application/json` в `agent_message`, а не новая
разновидность ACP update. Произвольные значения доступны для чтения; протокол не придаёт
полям этого JSON семантику редактируемых настроек.

```json
{
  "sessionUpdate": "agent_message", "messageId": "[\"session-state\", \"sql-acp-demo\", 25]",
  "content": [{"type": "resource", "resource": {
    "uri": "urn:boba:session-state", "mimeType": "application/json",
    "text": "{\"throughSeq\":25,\"settings\":[],\"permissions\":[],\"usage\":{}}"
  }}]
}
```

Для редактирования используются стандартные `configOptions` (`select`/`boolean`), когда
агент имеет определения доступных опций и обработчик `session/set_config_option`.
История значений этого каталога не содержит. SQL не выдумывает допустимые модели, диапазоны
sampling или обработчики изменения. [Config options](https://agentclientprotocol.com/protocol/v2/session-config-options).

## Результат сборки и отправка

```python
class AcpStateDraft(DataModel):
    bounds: StateBounds
    replay_complete: dict[str, JsonValue]
    snapshot: SavedSessionState
```

```json
{
  "bounds": {
    "session_id": "sql-acp-demo",
    "through_seq": 25,
    "last_seq": 25
  },
  "replay_complete": {
    "sessionUpdate": "replay_complete"
  },
  "snapshot": {
    "throughSeq": 25,
    "settings": [],
    "permissions": [],
    "usage": {}
  }
}
```


```python
class NotificationRow(DataModel):
    seq: PositiveInt
    record_id: str
    position: tuple[NonNegativeInt, ...]
    notification: SessionNotification
```

```json
{
  "seq": 4,
  "record_id": "user",
  "position": [
    1
  ],
  "notification": {
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {
      "sessionId": "sql-acp-demo",
      "update": {
        "sessionUpdate": "user_message",
        "messageId": "user",
        "content": [
          {
            "type": "text",
            "text": "Прочитай файл."
          }
        ]
      }
    }
  }
}
```

```python
class AcpHistoryDraft(DataModel):
    bounds: HistoryBounds
    notifications: tuple[NotificationRow, ...]
    validation_data: ValidationData
```

```json
{
  "bounds": {
    "session_id": "sql-acp-demo",
    "after_seq": 0,
    "through_seq": 3,
    "last_seq": 25
  },
  "notifications": [],
  "validation_data": {
    "unmapped_records": [],
    "parts": [],
    "copies": [],
    "calls": [],
    "results": []
  }
}
```

`seq`, `record_id`, `position`, `bounds` и `validation_data` принадлежат результату SQL,
по ACP отправляется только `notification`. `position` упорядочивает несколько updates одной
строки. Новая категория без политики отображения и неизвестная часть дают явную ошибку.
Категории prompt/context/setting/tool_catalog/tool_schema/request не становятся сообщениями
чата; `skill_activated` и происхождение дочерней сессии также не выводятся отдельным текстом.

```python
class AcpHistoryValidator:
    def require_valid(self, draft: AcpHistoryDraft) -> tuple[NotificationRow, ...]: ...
    def require_state(self, draft: AcpStateDraft) -> SavedSessionState: ...
    def require_elicitation(self, draft: AcpElicitationDraft) -> BaseModel: ...
```

Валидатор работает без I/O и состояния. Проверяет форму, диапазон и порядок, ссылки копий,
неизвестные части, повторные вызовы/результаты и результат без предшествующего call.
Отсутствующий результат допустим для ещё выполняющегося вызова.

```python
class ReplayCompleteUpdate(DataModel):
    session_update: Literal["replay_complete"] = Field(
        default="replay_complete", alias="sessionUpdate"
    )
```

```json
{
  "sessionUpdate": "replay_complete"
}
```

При `session/resume` с replay транспорт фиксирует верхний seq и читает все порции до него,
проверяет результат, отправляет историю, затем ровно один
`replay_complete`. Маркер отправляется и для пустой истории; при ошибке replay — не отправляется.
Для просмотра исторического среза маркер остаётся только данными результата запроса.

Далее транспорт синхронизирует **текущее** состояние исполнения и отвечает на resume.
Исторический `turn_end` не определяет текущее состояние. Живые чанки и новые notice ждут
окончания replay. Живой поток использует те же соглашения об идентификаторах сообщений;
промежуточные чанки не требуют строк в records.
[Replay complete](https://agentclientprotocol.com/protocol/v2/session-setup#replay-complete).

Проверка существования сессии и прав доступа предшествует SQL. Пустая существующая сессия
имеет through_seq=0; по отсутствию строк SQL не отличает её от неизвестной сессии.
`boba-aid` получает историю и чанки от агента по ACP.


## Проверка расширенного набора SQL: 14 ноября 2026

После добавления принятия ввода, служебных LLM-запросов, permissions, commands и options:
512 клиентов — 128 писателей и 384 читателя — через пул из 16 соединений, 128 чатов.
Читатели выполняли OpenAI и все семь ACP-запросов, каждый результат проходил валидатор.
За 31,56 с: 290 чтений/с, 95 пачек записи/с, 26 874 записанные строки, ошибок нет.
Каждый клиент завершил минимум 23 операции. Прирост временных файлов и байтов — нулевой;
временные таблицы не используются. p95 ACP history — 1,89 с, включая ожидание пула;
p95 самого ожидания пула — 1,84 с. Это результат одного Python-генератора с валидацией,
а не измерение предельной производительности PostgreSQL.

В генераторе исправлена несправедливая выдача соединений: до исправления были три таймаута
ожидания пула, после — ни одного. Производственный пул агента не изменялся.
При удержании незавершённой записи чтение того же чата заняло 7,19 мс, запись другого —
5,78 мс. 32 параллельных писателя одного чата получили уникальные последовательные seq.
177 результатов ACP отдельно проверены по закреплённой схеме протокола.
Артефакты: `.dev/history-sql/completeness-2026-11-14/`.

## Нагрузка: 14 ноября 2026, до введения записей принятия

Ниже сохранены результаты прежней версии. Они не являются замером расширенного набора SQL.

PostgreSQL 17.10, work_mem=4MB. Восьмипроцессный генератор, 512 подготовленных сессий,
961 024 исходные записи в той же records; вместе с остальными данными — больше миллиона строк.
На каждом уровне нагрузка подавалась 30 секунд, затем завершались уже начатые операции.
Один писатель и три читателя на чат: отдельно сохраняются ввод, граница запроса, ответ
с tool call, результат tool, usage, настройки add/remove, notice, elicitation и slash-команда.
Читатели запрашивают изменения, старые срезы, replay, настройки и точечные события.
В процессе измерялись соединения, wait_event, pg_blocking_pids, ошибки, повторы seq/record_id,
временные таблицы и прирост временных файлов. Формы и инструменты реально не исполнялись.

Первая серия — прямое подключение каждого клиента к PostgreSQL, исходный SQL:

| Клиенты / подключения | Чтений/с | Пачек записи/с | Ошибки |
| ---: | ---: | ---: | ---: |
| 16 / 16 | 594 | 306 | 0 |
| 64 / 64 | 478 | 188 | 0 |
| 128 / 128 | 465 | 203 | 0 |
| 256 / 256 | 466 | 152 | 0 |
| 512 / 512 | 395 | 113 | 76 |

На 512 соединениях обнаружены таймауты SQL и сильные ожидания LockManager, WAL и I/O.
Подготовленные запросы сами по себе перегрузку не устранили. После изменения SQL
повторный прогон на 512 прямых соединениях тоже получил таймауты; такой режим не считается рабочим.

Итоговый SQL обрабатывает content локально для одного сообщения; порядок seq сохраняется
индексом, затем используется Incremental Sort для позиций частей. Так устраняются глобальные
материализации широких payload и их сортировки на диск. Запросы, модели результата и
проверки остались совместимыми: прошли 80 функциональных случаев и сравнение с прежним SQL.
51 порция большой истории дала ровно те же 45 000 уведомлений в том же порядке, что полный запрос.

Проверенные профили итогового SQL:

| Клиенты | Соединения БД | Истории и чтение | Чтений/с | Пачек записи/с | Ошибки |
| ---: | ---: | --- | ---: | ---: | ---: |
| 512 | 512 | Малые, прямые подключения | 199 | 182 | 78 |
| 512 | 16 | Малые, общий пул | 1187 | 348 | 0 |
| 128 | 16 | Смешанные до 50 002, порции 1000 | 632 | 335 | 0 |
| 512 | 16 | Смешанные до 50 002, порции 1000 | 604 | 335 | 0 |
| 512 | 16 | Смешанные, порции 1000, prepare_threshold=None | 453 | 289 | 0 |

В смешанном профиле с подготовленными запросами каждый из 512 клиентов завершил
не менее 16 операций; ожидание пула
не превысило 3 секунд при лимите 5 секунд. p95 чтения изменений — 2,17 с с учётом очереди
и разбора JSON клиентом. Повтор с prepare_threshold=None, как в используемой конфигурации,
тоже прошёл без ошибок: все 512 клиентов выполнили не менее 15 операций,
максимальное ожидание пула — 3,07 с, p95 чтения изменений — 2,37 с.
Валидатор запускался для каждого 50-го чтения; функциональные проверки и проверка
всех страниц контрольной истории выполнялись отдельно.

В итоговых смешанных прогонах: временных таблиц 0, временных файлов 0, deadlock’ов 0,
ошибок валидации и повторов идентификаторов не было. Накопленные счётчики БД не сбрасывались:
указан их прирост за конкретный прогон. До изменения SQL большие монолитные выдачи
записывали гигабайты временных файлов за прогон и задерживали остальные запросы.

В отдельном горячем чате 128 клиентов, включая 32 писателя, дали около 18 пачек записи/с:
писатели ждали advisory lock этой сессии. Читатели на ней не ждали транзакционных блокировок.
Контроль с удержанием незавершённой записи: чтение того же чата 4,9 мс, запись другого чата
4,5 мс, вторая запись того же чата ждала удерживаемую блокировку около 2 секунд.
Сериализация записи одного чата обеспечивает порядок seq; разные чаты такую блокировку не делят.

Чтение длинных историй целиком под той же смешанной нагрузкой получило таймауты ожидания
пула даже при 16 соединениях. Одного ограничения числа соединений для него недостаточно:
порциями должны идти и replay, и первое чтение изменений с after_seq=0.

Артефакты и генератор: `.dev/history-sql/acp-stress-2026-11-14/`.
Прогоны pooled16/pooled32 помечены недействительными для оценки ёмкости: в ранней версии
генератора обнаружена несправедливая выдача соединений. В итоговых профилях очередь исправлена,
проверено число завершивших операции клиентов и установлен таймаут ожидания пула.
