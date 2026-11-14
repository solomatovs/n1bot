# SQL-адаптер истории в ACP v2

Источник — `history_sql.records`. Модели хранения: [история агента](agent-history-and-request-assembly.md).
Исполняемые модели и проверки: [acp_validator.py](../experiments/history-sql/acp_validator.py).
SQL создаёт данные протокола; отправка, capabilities и координация исполнения принадлежат транспорту.

Контракт закреплён на [ACP v2, revision f49d27a9](https://github.com/agentclientprotocol/agent-client-protocol/blob/f49d27a9eab068cd9a48901237f256da34fe0183/schema/v2/schema.json).
Это draft. Установленный `agent-client-protocol==1.0.0rc3` ещё не содержит
`replay_complete` и `subagent_update`: их используемые поля описаны локальными моделями.
Подключение SQL к серверу требует согласовать эту ревизию с SDK и клиентом.

## Как использовать запросы

Это четыре операции адаптера с разными поводами для запуска. При восстановлении чата
используются `acp_history.sql` и `acp_state.sql`. Когда агенту нужно сейчас показать
предупреждение или задать вопрос, используются соответственно `acp_notice.sql` и
`acp_elicitation.sql`. Запускать все четыре подряд для восстановления не нужно.

Все запросы только читают уже сохранённую историю и возвращают данные. Выполнение SQL
в редакторе ничего не отправляет клиенту, не запускает tools и не вызывает LLM.
Отправлять проверенный результат должен ACP-адаптер агента. Эти файлы пока являются
прототипом, а не подключёнными обработчиками работающего сервера.

### Где запускать и что означают параметры

Подключение — настроенный PostgreSQL 17, база **`aid_ss_history_sql_boba`**,
таблица **`history_sql.records`**. Данные для примеров сохранены в
[fixtures.sql](../experiments/history-sql/fixtures.sql).

- `session_id` — идентификатор чата из `records.session_id`. В примерах: `'sql-acp-demo'`.
- `seq` — порядковый номер сохранённой записи внутри этого чата. В тестовом чате: 1–25.
- `record_id` — идентификатор одной записи из `records.record_id`, например `'warning'`.
  Он выбирает конкретное предупреждение или вопрос; это не номер `seq`.
- `request_id` — связь с LLM-запросом. **Ни один из этих четырёх ACP-запросов его не принимает.**

`$1`, `$2`, `$3` — места для параметров при вызове из программы. Для ручного запуска
открой нужный SQL-файл и замени значения **только в первом CTE `params`**, как показано ниже.
Затем выполни весь файл. SQL `null` пишется без кавычек.

### `acp_history.sql` — восстановить содержимое чата

[Запрос](../experiments/history-sql/acp_history.sql) превращает записи истории в сообщения
ACP для показа переписки: ввод пользователя, ответы и рассуждения агента, вызовы tools,
их результаты, компактизацию и сохранённые служебные сообщения. Читает историю с начала,
включая диалог до компактизации; копии с `copied_from_id` повторно не показывает.

Используется при открытии сохранённого чата с историей (`session/resume` с replay),
при чтении истории порциями и для ручного просмотра того, что клиент увидел бы на выбранном seq.

Для всего тестового чата блок параметров выглядит так:

```sql
params as (
    select
        'sql-acp-demo'::text as session_id,
        null::bigint as after_seq,
        null::bigint as through_seq
),
```

`after_seq` исключает сам указанный номер и всё перед ним; `null` равнозначен 0.
`through_seq` включает указанный номер; `null` выбирает последний seq на момент чтения.
Поэтому параметры `(15, 25)` означают записи **16–25**, а не количество записей или OFFSET.

Готовые случаи из тестовых данных:

| Случай | Параметры | Ожидаемый результат |
| --- | --- | --- |
| Весь чат | `'sql-acp-demo', null, null` | 15 ACP-уведомлений, bounds.through_seq=25 |
| Только настройки | `'sql-acp-demo', null, 3` | Пустой notifications: настройки не становятся репликами чата |
| Ввод с вложениями | `'sql-acp-demo', 3, 4` | Одно user_message: текст, изображение, ресурс с текстом, пустой ресурс и ссылка на незагруженный ресурс |
| Ответ с вызовом tool | `'sql-acp-demo', 5, 6` | Строго agent_thought → agent_message → tool_call_update → agent_message |
| Только результат tool | `'sql-acp-demo', 6, 7` | Одно tool_call_update со status=completed и toolCallId=call-read; вызов найден в предыдущей части истории |
| Предупреждение в истории | `'sql-acp-demo', 7, 9` | Два agent_message с сохранёнными предупреждениями; активные notice не создаются |
| До компактизации | `'sql-acp-demo', null, 15` | 13 уведомлений; compact ещё отсутствует |
| После компактизации | `'sql-acp-demo', 15, 25` | compaction_update и финальный agent_message; копии диалога не дублируются |
| Только копии и настройки | `'sql-acp-demo', 16, 24` | Пустой notifications; bounds.through_seq=24 остаётся позицией прочитанной порции |
| Пустой диапазон | `'sql-acp-demo', 25, 25` | Пустой notifications, без ошибки |
| Background и сабагент | `'sql-background-demo', null, null` | Вызовы и результаты tools, сообщения задач, subagent_update для дочерней сессии |
| Slash-команды | `'sql-demo', null, null` | Команды как user_message, результаты как agent_message; повторного исполнения нет |

Результат — **одна строка SQL** с тремя столбцами:

- `bounds` — фактически использованный диапазон и последний seq в истории.
- `notifications` — упорядоченный массив. В каждом элементе `notification` уже содержит
  полное JSON-RPC уведомление `session/update`; именно его отправляют клиенту.
- `validation_data` — данные для `AcpHistoryValidator.require_valid`, клиенту их не отправляют.

Одна запись может дать несколько ACP-уведомлений; некоторые записи не дают ни одного.
После успешной обработки порции позиция чтения продвигается до **`bounds.through_seq`**,
даже если `notifications` пуст. Последний seq отображённого сообщения для этого не подходит.
`after_seq` служит внутреннему чтению из БД и не объявляет новый вид ACP replay cursor.

### `acp_state.sql` — показать сохранённые настройки и статистику

[Запрос](../experiments/history-sql/acp_state.sql) собирает снимок на выбранный seq:
действующие постоянные model/sampling/inference и другие setting, последний снимок
разрешений, сумму сохранённых счётчиков токенов. Разовые настройки с `request_id`
в этот снимок не включаются, снятые через remove категории тоже исключаются.

Используется вместе с восстановлением чата, чтобы пользователь мог прочитать его настройки,
либо отдельно для просмотра настроек в прошлом. Это сохранённые значения; состояние
выполнения «агент сейчас работает или ожидает» данный запрос не определяет.

Для снимка в конце тестового чата:

```sql
params as (
    select
        'sql-acp-demo'::text as session_id,
        25::bigint as through_seq
),
```

`through_seq=15` покажет настройки до компактизации, `25` — в конце примера,
`null` — на последнем сохранённом seq. На seq=25 постоянный sampling снят;
sampling для одного LLM-запроса его не заменяет. При восстановлении передавай сюда
**тот же верхний seq, до которого прочитана история**, чтобы получить согласованный снимок.

| Параметры | Ожидаемые settings |
| --- | --- |
| `'sql-acp-demo', 3` | model и sampling с temperature=0.2, top_p=0.9 |
| `'sql-acp-demo', 17` | model: после компактизации перенесена только модель |
| `'sql-acp-demo', 21` | model и sampling с temperature=0.7 |
| `'sql-acp-demo', 22` | model: sampling явно снят |
| `'sql-acp-demo', 23` | model: разовый sampling с request_id=r1 не становится постоянным |
| `'sql-acp-demo', null` | Тот же снимок settings, что на seq=25; permissions содержит правило ask; usage.input_tokens=12 |

Результат — одна строка с `bounds`, `update` и `replay_complete`.
`update` — стандартный `agent_message` с вложенным JSON-ресурсом настроек. Его содержимое
находится в `update.content[0].resource.text`. После `require_state` адаптер помещает
`update` в `params.update` уведомления `session/update`, указав `params.sessionId` этого чата.
Это представление для чтения; редактируемые элементы `configOptions` запрос не создаёт.

`replay_complete` — отдельно подготовленный маркер окончания восстановления.
Он возвращается рядом со снимком, но сам запуск `acp_state.sql` не завершает replay:
адаптер отправляет маркер **один раз после всей истории**, а не после каждой порции.
При отдельном просмотре настроек этот столбец не отправляется.

### `acp_notice.sql` — показать новое предупреждение сейчас

[Запрос](../experiments/history-sql/acp_notice.sql) выбирает **одну конкретную запись**
`notice`, `llm_request_attempt`, `task_state` или `task_notification` и готовит стандартный
ACP `notice` с её уровнем и текстом.

Повод для запуска: агент сохранил новое предупреждение, получил его `record_id`
и должен уведомить подключённого клиента. Например, только что произошла ошибка LLM
и агент сообщает о повторной попытке. Прочитанная при восстановлении старая ошибка
уже будет отображена через `acp_history.sql` как историческое сообщение.

Для ручного просмотра предупреждения из примера:

```sql
params as (
    select
        'sql-acp-demo'::text as session_id,
        'warning'::text as record_id
),
```

| Параметры | Ожидаемый update |
| --- | --- |
| `'sql-acp-demo', 'warning'` | notice, severity=warning, title=«Временная задержка.» |
| `'sql-acp-demo', 'retry'` | notice с текстом повторной попытки LLM |
| `'sql-acp-actions-demo', 'info'` | notice, severity=info |
| `'sql-acp-actions-demo', 'error'` | notice, severity=error |

Результат — одна строка с `session_id`, `record_id`, `seq`, `update`.
После `AcpNoticeDraft.require_valid()` адаптер отправляет `update` внутри `session/update`.
Если подходящая запись не найдена, `seq=null` и валидатор возвращает ошибку.

SQL не определяет, доставлялось ли предупреждение раньше, и не помечает его отправленным.
Решение о показе нового уведомления принимает вызывающий код; при replay этот запрос не вызывается.

### `acp_elicitation.sql` — задать пользователю конкретный вопрос

[Запрос](../experiments/history-sql/acp_elicitation.sql) читает одну сохранённую
`elicitation_request` и готовит ACP-вызов `elicitation/create`: форму с вариантами ответа
либо предложение открыть URL для внешнего действия, например подключения доступа.

Повод для запуска: обработчик агента сохранил вопрос и должен запросить действие у клиента.
Участие LLM для этого не требуется. `record_id` берётся из результата сохранения вопроса.

Для ещё не отвеченной формы из примера:

```sql
params as (
    select
        'sql-acp-demo'::text as session_id,
        'pending-form'::text as record_id
),
```

В той же сессии `'url'` подготовит запрос внешнего действия, а `'form'` — уже отвеченную
форму, которую валидатор отклонит. Режим form/url берётся из самой записи; отдельного
параметра режима у SQL нет.

| Параметры | Результат проверки |
| --- | --- |
| `'sql-acp-demo', 'pending-form'` | Можно подготовить form с выбором short/full |
| `'sql-acp-demo', 'url'` | Можно подготовить url с elicitationId=url |
| `'sql-acp-actions-demo', 'pending'` | Можно подготовить form с boolean-полем confirm |
| `'sql-acp-actions-demo', 'pending-url'` | Можно подготовить url |
| `'sql-acp-demo', 'form'` | elicitation_already_answered: сохранён ответ accept |
| `'sql-acp-actions-demo', 'cancelled'` | elicitation_already_answered: сохранён cancel |
| `'sql-acp-actions-demo', 'declined'` | elicitation_already_answered: сохранён decline |
| `'sql-acp-actions-demo', 'url-completed'` | elicitation_already_answered: внешнее действие завершено |
| `'sql-acp-actions-demo', 'url-failed'` | elicitation_already_answered: внешнее действие завершилось ошибкой |
| `'sql-acp-demo', 'missing'` | elicitation_not_found |

Результат — одна строка с `method='elicitation/create'`, `params`, `required_mode`,
источником (`session_id`, `record_id`, `seq`) и счётчиками `response_count`, `completion_count`.
`require_elicitation` отклоняет отсутствующий вопрос, уже сохранённый ответ или завершённое действие.
После проверки режима, поддержанного клиентом, транспорт назначает JSON-RPC `id`
и отправляет запрос с полученными `method` и `params`; это вызов, ожидающий ответ клиента.

Отсутствие ответа в истории ещё не означает, что вопрос не отправлен и не ожидает ответа сейчас.
Это проверяет координатор. Восстановление переписки показывает старые вопросы через
`acp_history.sql` и само по себе не запускает elicitation повторно.

### Какие запросы вызываются при восстановлении чата

Для тестового чата последовательность такая:

1. Выполнить `acp_history.sql` с `('sql-acp-demo', null, null)`. Прочитать верхнюю границу
   из `bounds.through_seq`; в подготовленных данных это 25. Проверить и отправить `notifications`.
2. Выполнить `acp_state.sql` с `('sql-acp-demo', 25)`. Проверить и отправить `update` настроек,
   затем один `replay_complete`.
3. Транспорт сообщает текущее состояние исполнения и отвечает на `session/resume`.
   Текущее состояние берётся у работающего агента.

При подключении к агенту используется общий ограниченный пул PostgreSQL; число клиентов
не определяет число соединений с БД. Для большого журнала перед чтением фиксируется
последний seq через хранилище, а диапазон до него читается порциями. Например, при верхней границе 25: сначала
`(null, 15)`, затем `(15, 25)`, после чего снимок настроек на 25 и один маркер завершения.
Новые записи за зафиксированной границей обрабатываются после replay.
`acp_notice.sql` и `acp_elicitation.sql` в эту последовательность не входят.

В проверенном нагрузочном профиле каждый запрос истории ограничен 1000 записями —
как replay, так и первое чтение изменений с after_seq=0. После порции соединение
возвращается в пул, клиенту отправляются полученные уведомления. Порции продолжаются
до зафиксированного верхнего seq, затем отправляется один replay_complete.
Ожидающие соединения обслуживаются по очереди; таймаут ожидания пула — 5 секунд.
Чтение всей большой истории одним запросом остаётся доступным для ручной проверки,
но его одновременный запуск многими клиентами перегружает пул.

## Дополнительные демо

Для повторного заполнения используй [fixtures.sql](../experiments/history-sql/fixtures.sql).
Большие истории создаёт [acp_load.sql](../experiments/history-sql/acp_load.sql).
В текущей экспериментальной базе данные уже загружены.

| Сессия | Число записей | Содержимое |
| --- | ---: | --- |
| sql-acp-load-1000 | 1 002 | 100 циклов user / assistant / tool / notice / usage / elicitation / settings |
| sql-acp-load-10000 | 10 002 | 1 000 таких циклов |
| sql-acp-load-50000 | 50 002 | 5 000 таких циклов |
| sql-acp-load-commands | 2 000 | 1 000 slash-команд и их результатов |
| sql-acp-stress-0001 … sql-acp-stress-0512 | 961 024 суммарно | 512 сессий: 480 по 1 002 записи, 28 по 10 002 и 4 по 50 002 |

| Запрос | Параметры | Что измерять |
| --- | --- | --- |
| acp_history.sql | `'sql-acp-load-1000', null, null` | Полная небольшая история |
| acp_history.sql | `'sql-acp-load-10000', null, null` | Полная история, результат около 7,2 МБ |
| acp_history.sql | `'sql-acp-load-50000', null, null` | Полная большая история, результат около 36 МБ |
| acp_history.sql | `'sql-acp-load-50000', 0, 100` | Первые 100 записей |
| acp_history.sql | `'sql-acp-load-50000', 49902, 50002` | Последние 100 записей с проверкой предшествующих вызовов |
| acp_history.sql | `'sql-acp-load-commands', null, null` | Большое число результатов команд |
| acp_state.sql | `'sql-acp-load-50000', null` | Снимок настроек и сумма 5 000 usage-записей |
| acp_notice.sql | `'sql-acp-load-50000', 'notice-1'` | Поиск одной записи по идентификатору |
| acp_elicitation.sql | `'sql-acp-load-50000', 'question-1'` | Поиск вопроса и уже сохранённого ответа |

Намеренно ошибочные случаи: SQL возвращает проект результата, ошибку выдаёт
Python-валидатор перед отправкой клиенту.

| Запрос | Параметры | Ошибка валидатора |
| --- | --- | --- |
| acp_history.sql | `'sql-acp-orphan-demo', null, null` | orphan_tool_result: у результата tool нет предшествующего вызова |
| acp_history.sql | `'sql-acp-duplicate-demo', null, null` | duplicate_tool_call: два вызова с одним call_id |
| acp_history.sql | `'sql-acp-copy-demo', null, null` | invalid_copy_reference: copied_from_id ведёт на отсутствующую запись |
| acp_history.sql | `'sql-acp-demo', 20, 15` | invalid_history_range: начало позже конца |
| acp_history.sql | `'sql-acp-demo', null, 100` | invalid_history_range: верхняя граница за концом истории |
| acp_notice.sql | `'sql-acp-demo', 'missing'` | notice_not_found |

## Содержимое и сообщения

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
    status: Literal["pending", "completed", "failed"]
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

`ToolCallPart` создаёт вызов; `ToolResultRecord` обновляет его по тому же `toolCallId`.
`is_error=false/true` преобразуется в `completed/failed`. Исполнение в фоне само по себе
не меняет статус первоначального вызова: принятие задачи уже может завершить этот вызов.

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

Источник — `TaskStateRecord.payload.state.child_session_id` в родительской сессии.
Ассоциация передаётся перед текстом состояния задачи; повторное объявление идемпотентно.
История ребёнка читается отдельно и отправляется с его `sessionId` только после объявления
ассоциации. Транспорт проверяет владение сессией; SQL не даёт прав на чужую историю.
Текущие состояния ребёнка и разрешённые клиентские операции из прошлых записей не выводятся.

## Уведомления и вопросы

Исторические `notice`, `llm_request_attempt`, `task_state`, `task_notification` дают обычный
`agent_message`: сохранённый текст с уровнем и вложенный JSON-ресурс со структурой события.
`turn_end`, результаты разрешений и elicitation также отображаются как записи истории,
без запуска действий. Это стандартные сообщения ACP; специальной типизированной карточки
warning или формы обычному клиенту такой replay не обещает.

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

`acp_state.sql` берёт постоянные setting по LAST внутри текущего контекста; remove снимает
категорию целиком. Настройки с request_id исключаются. Разрешения — последний сохранённый
снимок; token usage — последние счётчики каждого запроса, суммированные по именам.
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
    update: dict[str, JsonValue]
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
  "update": {
    "sessionUpdate": "agent_message",
    "messageId": "[\"session-state\", \"sql-acp-demo\", 25]",
    "content": [
      {
        "type": "resource",
        "resource": {
          "uri": "urn:boba:session-state",
          "mimeType": "application/json",
          "text": "{\"throughSeq\":25,\"settings\":[],\"permissions\":[],\"usage\":{}}"
        }
      }
    ]
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
проверяет результат, отправляет историю и снимок сохранённых настроек, затем ровно один
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


## Нагрузка: 14 ноября 2026

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
