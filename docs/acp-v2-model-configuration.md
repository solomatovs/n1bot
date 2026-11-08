# ACP v2: провайдеры, модели, параметры генерации и авторизация

Проверено: 2026-11-06. Документ относится только к Agent Client Protocol версии 2.
Официальная документация обозначает эту версию как **v2 Draft**. Версия протокола
задаётся полем `protocolVersion: 2`; номер версии SDK — отдельная величина.

Источники проверены по ревизии
[`7ec5185c18ca928b48e457d2d8a163800be013c8`](https://github.com/agentclientprotocol/agent-client-protocol/tree/7ec5185c18ca928b48e457d2d8a163800be013c8).
Разделы 1–9 — справочник по протоколу. Раздел 10 фиксирует принятое разделение
HTTP-аутентификации и ACP. **Задание на реализацию, сообщения и приёмку см. в
[ACP V2: SDK и авторизация](acp-v2-sdk-auth-implementation.md).** Старый проект
обязательного auth/login после Bearer удалён; он не является контрактом Boba.

## 1. Кто выбирает и вызывает LLM

```text
ACP-клиент ── ACP v2 ── агент ── API провайдера ── LLM
```

ACP соединяет клиент и агента. Агент хранит настройки сессии и применяет их при
вызове своего LLM backend. Доступ к провайдеру, его endpoint, credentials и
преобразование параметров в запрос конкретного API определяет реализация агента.

В базовой схеме ACP v2 нет методов `providers/list`, `models/list` или
`connect_provider`. Выбор модели сообщается через настройки сессии:

1. `initialize` согласует версию протокола и возможности сторон.
2. `session/new` создаёт сессию и возвращает её `configOptions`.
3. `session/set_config_option` меняет одну опцию и возвращает полный список опций.
4. `session/prompt` передаёт сообщение; агент использует настройки сессии.
5. `session/update` сообщает сообщения, состояние работы и изменения конфигурации.

`mcpServers` в запросе создания сессии описывает серверы инструментов. Это не
каталог LLM-провайдеров.

Источники: [Overview](https://agentclientprotocol.com/protocol/v2/overview),
[Session Setup](https://agentclientprotocol.com/protocol/v2/session-setup),
[Schema](https://agentclientprotocol.com/protocol/v2/schema).

## 2. Инициализация: `initialize`

Следующие запрос и ответ взяты из раздела
[Initialization](https://agentclientprotocol.com/protocol/v2/initialization).

Клиент → агент:

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "method": "initialize",
  "params": {
    "protocolVersion": 2,
    "capabilities": {
      "auth": {
        "terminal": {}
      }
    },
    "info": {
      "name": "my-client",
      "title": "My Client",
      "version": "1.0.0"
    }
  }
}
```

Агент → клиент:

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "result": {
    "protocolVersion": 2,
    "capabilities": {
      "session": {
        "prompt": {
          "image": {},
          "audio": {},
          "embeddedContext": {}
        },
        "mcp": {
          "stdio": {},
          "http": {}
        }
      }
    },
    "info": {
      "name": "my-agent",
      "title": "My Agent",
      "version": "1.0.0"
    },
    "authMethods": []
  }
}
```

`info` описывает реализацию клиента или агента. `capabilities` описывает
поддерживаемые возможности; модели и провайдеры здесь не перечисляются.
Наличие `capabilities.session`, в том числе пустого объекта `{}`, объявляет
поддержку базовых методов сессий: `session/new`, `session/list`, `session/resume`,
`session/close`, `session/prompt`, `session/cancel`, `session/update`.

Агент, который объявляет `configOptions` для сессии, также обязан поддержать для
неё `session/set_config_option`; отдельный capability для этого не нужен.

Если `authMethods` отсутствует или пуст, клиент не должен вызывать `auth/login`
или `auth/logout`. При непустом списке используются объявленные агентом способы
аутентификации. Конкретную процедуру реализует агент: например, подключает аккаунт
провайдера, с которым будет работать. Это не универсальный API настройки любого
LLM backend. Для Boba с заранее настроенными credentials выбран пустой список.

Конкретный ответ `initialize` для сетевого сервера boba и полный обмен при входе
описаны в [разделе 10](#boba-auth-contract).

## 3. Получение списка моделей: ответ на `session/new`

Клиент → агент. Это минимальный пример по схеме v2; список MCP-серверов пуст:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "session/new",
  "params": {
    "cwd": "/home/user/project",
    "mcpServers": []
  }
}
```

Агент → клиент. Из примера
[Session Config Options](https://agentclientprotocol.com/protocol/v2/session-config-options)
оставлены две опции — модель и размер контекста; остальные элементы списка опущены:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "sessionId": "sess_abc123def456",
    "configOptions": [
      {
        "configId": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "model-1",
        "options": [
          {
            "value": "model-1",
            "name": "Model 1",
            "description": "The fastest model"
          },
          {
            "value": "model-2",
            "name": "Model 2",
            "description": "The most powerful model"
          }
        ]
      },
      {
        "configId": "context_size",
        "name": "Context Size",
        "category": "model_config",
        "type": "select",
        "currentValue": "200k",
        "options": [
          { "value": "200k", "name": "200K" },
          { "value": "1m", "name": "1M" }
        ]
      }
    ]
  }
}
```

`currentValue` — значение, которое агент уже использует. В данном примере это
`model-1` с размером контекста `200k`. Клиент выбирает из `options`.

`configOptions` относится к конкретной сессии. Агент возвращает её актуальный
список также при `session/resume`. Отсутствие списка в ответе создания или
возобновления сессии означает отсутствие объявленных опций; клиент заменяет свой
список, а не сохраняет предыдущий.

Схема `session/new` содержит `cwd`, `additionalDirectories`, `mcpServers`,
`systemPrompt` и `_meta`; обязательным является `cwd`. Дополнительные директории,
system prompt и транспорты MCP имеют собственные требования к capabilities.
Полей `provider`, `model` и `sampling` непосредственно в этом запросе нет.

## 4. Выбор модели: `session/set_config_option`

В документации пример этого метода меняет режим сессии. Здесь тот же стандартный
метод применён к опции `model` из предыдущего ответа: изменены `configId` и `value`.

Клиент → агент:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/set_config_option",
  "params": {
    "sessionId": "sess_abc123def456",
    "configId": "model",
    "type": "id",
    "value": "model-2"
  }
}
```

Агент → клиент. Пример для сессии ровно с двумя опциями из раздела 3:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "configOptions": [
      {
        "configId": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "model-2",
        "options": [
          {
            "value": "model-1",
            "name": "Model 1",
            "description": "The fastest model"
          },
          {
            "value": "model-2",
            "name": "Model 2",
            "description": "The most powerful model"
          }
        ]
      },
      {
        "configId": "context_size",
        "name": "Context Size",
        "category": "model_config",
        "type": "select",
        "currentValue": "200k",
        "options": [
          { "value": "200k", "name": "200K" },
          { "value": "1m", "name": "1M" }
        ]
      }
    ]
  }
}
```

Ответ содержит весь актуальный список опций, включая неизменившиеся. Смена модели
может изменить доступные параметры, их варианты и выбранные значения.

Для выбора из `select` запрос использует `type: "id"` и строковый `value`.
Для переключателя `boolean` запрос использует `type: "boolean"` и логический
`value`. У неизвестного `configId`, неподдерживаемого типа или недопустимого
значения результат — ошибка `-32602`; конфигурация остаётся прежней.

Изменение параметра модели устроено так же. Например, выбор контекста:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "session/set_config_option",
  "params": {
    "sessionId": "sess_abc123def456",
    "configId": "context_size",
    "type": "id",
    "value": "1m"
  }
}
```

В ответе снова возвращается полный `configOptions`, теперь с `currentValue: "1m"`
у `context_size`, если агент принял именно это значение. Клиент должен использовать
значения из ответа: агент может скорректировать запрошенное значение.

Изменения доступны и во время работы агента. Будет ли новая настройка применена
к текущей работе или только к следующей, определяет агент. Чтобы поведение было
однозначным для первого prompt, клиент может дождаться ответов на все настройки
до его отправки.

Источник: [Setting a config option](https://agentclientprotocol.com/protocol/v2/session-config-options#setting-a-config-option).

## 5. Как сообщать LLM-провайдеров

В ACP v2 стандартизированы механизм опций и категории для UI, а не каталог
LLM-провайдеров или формат идентификатора модели.

| Категория | Назначение |
| --- | --- |
| `model` | Выбор модели |
| `model_config` | Параметр модели, например размер контекста |
| `thought_level` | Выбор уровня рассуждения |
| `mode` | Режим работы агента |

`category` — необязательная подсказка для интерфейса. Корректность протокола не
должна зависеть от неё. Идентификаторы `configId` и `options[].value` определяет
агент; клиент не должен считать любое значение модели строкой `provider/model`.

Есть два возможных устройства агента. Оба ниже — примеры реализации поверх ACP,
а не готовые примеры каталога провайдеров из спецификации.

### 5.1. Один селектор модели, варианты сгруппированы по провайдерам

Следующий объект — элемент `configOptions`. Группировка и поля `groupId`, `name`,
`options` определены ACP v2; названия провайдеров и идентификаторы моделей выбраны
для иллюстрации и не являются актуальным каталогом доступных моделей:

```json
{
  "configId": "model",
  "name": "Model",
  "category": "model",
  "type": "select",
  "currentValue": "openai/gpt-4.1",
  "options": [
    {
      "groupId": "openai",
      "name": "OpenAI",
      "options": [
        { "value": "openai/gpt-4.1", "name": "GPT-4.1" }
      ]
    },
    {
      "groupId": "anthropic",
      "name": "Anthropic",
      "options": [
        { "value": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4" }
      ]
    }
  ]
}
```

Клиент отправляет `session/set_config_option` с `configId: "model"`,
`type: "id"`, `value: "anthropic/claude-sonnet-4"`. Агент разрешает этот ID в
конкретное подключение к провайдеру и модель. Для ACP значение остаётся
непрозрачным ID, даже если агент внутри использует маршрутизацию LiteLLM.
Группировка влияет на представление вариантов; отдельной операции выбора группы нет.

### 5.2. Отдельные селекторы провайдера и модели

Агент может объявить собственную опцию `configId: "provider"` типа `select` со
списком доступных подключений. Стандартной категории `provider` нет: её можно
опустить или использовать собственную категорию с префиксом `_`.

После выбора провайдера обработчик агента:

1. Проверяет, что провайдер доступен этой сессии.
2. Выбирает совместимую модель или её значение по умолчанию.
3. Пересчитывает список моделей и поддерживаемых параметров.
4. Возвращает полный `configOptions` с согласованными `currentValue`.

ACP не задаёт, когда создавать API-клиент провайдера или проверять его credentials.
Это можно сделать при применении опции либо при первом обращении к LLM.

Получение каталога провайдеров до создания сессии потребует собственного
расширения: стандартного метода для такого discovery в проверенной схеме нет.

Источник: [Session Config Options](https://agentclientprotocol.com/protocol/v2/session-config-options).

## 6. Sampling и другие параметры генерации

Встроенные типы опций ACP v2 — `select` и `boolean`. Стандарт не определяет
общего объекта `sampling` или числового поля для свободного ввода temperature.

Для передачи конечного набора значений temperature, top_k и других параметров
достаточно стандартных `configOptions` и `session/set_config_option`. Названия
параметров и их сопоставление с backend определяет агент. Ниже — пример такого
сопоставления, а не обязательные sampling-опции из спецификации.

### 6.1. Объявить доступные значения

Агент включает следующие два объекта в `configOptions` ответа на `session/new`
или `session/resume`. Они могут дополнять показанные ранее опции модели и контекста.
Первый объект описывает temperature:

```json
{
  "configId": "temperature",
  "name": "Temperature",
  "category": "model_config",
  "type": "select",
  "currentValue": "0.2",
  "options": [
    { "value": "0.0", "name": "0.0" },
    { "value": "0.2", "name": "0.2" },
    { "value": "0.7", "name": "0.7" }
  ]
}
```

Второй объект описывает top_k. Значения выбраны для иллюстрации; агент объявляет
только параметры и варианты, которые поддерживает выбранный backend и модель:

```json
{
  "configId": "top_k",
  "name": "Top K",
  "category": "model_config",
  "type": "select",
  "currentValue": "20",
  "options": [
    { "value": "20", "name": "20" },
    { "value": "40", "name": "40" },
    { "value": "80", "name": "80" }
  ]
}
```

`category: "model_config"` помогает клиенту показать настройки рядом с выбором
модели. Она сама не задаёт правила преобразования параметра или его применения
к LLM-запросу.

### 6.2. Установить значения стандартными запросами

Для сессии, которая объявила обе опции, клиент сначала устанавливает temperature:

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "session/set_config_option",
  "params": {
    "sessionId": "sess_abc123def456",
    "configId": "temperature",
    "type": "id",
    "value": "0.7"
  }
}
```

После ответа клиент устанавливает top_k:

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "session/set_config_option",
  "params": {
    "sessionId": "sess_abc123def456",
    "configId": "top_k",
    "type": "id",
    "value": "40"
  }
}
```

На каждый запрос агент возвращает полный актуальный `configOptions`, как показано
в разделе 4. При принятии этих значений после второго ответа у `temperature`
будет `currentValue: "0.7"`, а у `top_k` — `currentValue: "40"`. Ответ также
содержит остальные опции сессии, включая модель и размер контекста, если они
объявлены. Клиент проверяет фактические значения из ответа: агент может
скорректировать запрос.

Вызовы меняют по одной опции. Стандартный `session/set_config_option` не задаёт
атомарную установку сразу всего объекта sampling.

### 6.3. Преобразовать выбранные значения в параметры backend

На проводе `"0.7"` — строковый ID. Агент сопоставляет ему числовой параметр
`temperature=0.7`, а `"40"` — целому `top_k=40`. ACP SDK не выполняет такое
преобразование и не добавляет параметры в запрос провайдера автоматически.

Обработчик `session/set_config_option` в агенте должен:

1. Проверить `sessionId`, объявленный `configId`, `type: "id"` и наличие `value`
   среди текущих вариантов этой опции.
2. Проверить поддержку настройки выбранной моделью и backend.
3. Сохранить выбранное значение и соответствующее ему значение для генерации.
4. Вернуть полный актуальный список `configOptions` с применёнными значениями.

Ниже псевдокод преобразования уже проверенных значений и вызова адаптера:

```python
temperature_values = {"0.0": 0.0, "0.2": 0.2, "0.7": 0.7}
top_k_values = {"20": 20, "40": 40, "80": 80}

generation = {
    "temperature": temperature_values[session.current_values["temperature"]],
    "top_k": top_k_values[session.current_values["top_k"]],
}

await backend.generate(
    model=session.model,
    messages=messages,
    **generation,
)
```

`backend.generate` здесь — условный метод адаптера агента, а не метод ACP или
универсальная сигнатура API провайдеров. Адаптер формирует запрос, допустимый
для конкретного backend. При смене модели агент пересчитывает доступные опции
и их значения, чтобы не оставлять неподдерживаемые параметры.

Аналогично можно объявить конечные варианты `top_p`, лимита выхода и других
поддерживаемых настроек. Эти имена `configId` не имеют отдельной стандартной
семантики ACP.

### 6.4. Отправить prompt и определить момент применения

После ответов на оба изменения клиент отправляет обычный `session/prompt`:

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "session/prompt",
  "params": {
    "sessionId": "sess_abc123def456",
    "prompt": [
      {
        "type": "text",
        "text": "Предложи несколько вариантов решения."
      }
    ]
  }
}
```

Параметры generation в этом сообщении не дублируются: агент берёт их из состояния
сессии. Они сохраняются до следующего изменения, а не сбрасываются после одного
prompt. Ответ на prompt и дальнейшие уведомления описаны в разделе 7.

Один prompt может привести к нескольким внутренним LLM-вызовам, например до и
после исполнения инструмента. ACP не определяет, как адресовать отдельный такой
инференс или какие параметры применять к каждому из них: это логика агента.

Для предсказуемой реализации можно зафиксировать снимок настроек в начале
обработки prompt и использовать его для связанных LLM-вызовов. Это выбранная
политика агента, а не требование ACP. Если настройки меняют во время работы,
сам агент определяет, затрагивает изменение эту работу или только последующую.
Последовательность «настроить, дождаться ответов, отправить prompt» предполагает,
что других одновременных изменений настроек нет.

### 6.5. Границы стандартных опций

Установка `0.731`, если такого ID нет в `options`, недопустима для `select`.
Для произвольных числовых значений или сложных объектов нужны согласованные
расширения клиента и агента:

- собственный тип конфигурационной опции, начинающийся с `_`;
- либо собственный JSON-RPC метод с префиксом `_` и согласованной схемой параметров.

Незнакомый тип опции клиент может сохранить и проигнорировать при отображении;
наличие такого объекта в схеме не означает, что клиент умеет его редактировать.
Собственные методы и их поддержку следует объявлять через namespaced capability
в `_meta` объекта capabilities. Для имён используют префикс контролируемого
домена в обратной DNS-нотации, например `com.example/generationConfig` для
capability и `_com.example/set_generation_config` для метода.

Не следует добавлять произвольные поля `sampling` или `temperature` в корень
стандартного `session/prompt` и рассчитывать на их применение: расширения имеют
собственный контракт и должны быть понятны обеим сторонам.

Для части других настроек в протоколе есть отдельные поля. Например,
`systemPrompt` в запросах жизненного цикла сессии доступен при объявленной
`capabilities.session.systemPrompt`; это отдельная возможность, а не sampling.

Источники: [Session Config Options](https://agentclientprotocol.com/protocol/v2/session-config-options),
[Extensibility](https://agentclientprotocol.com/protocol/v2/extensibility),
[Session Setup](https://agentclientprotocol.com/protocol/v2/session-setup).

## 7. Prompt, ответ модели и завершение работы

Следующая последовательность сокращает примеры
[Prompt Lifecycle](https://agentclientprotocol.com/protocol/v2/prompt-lifecycle)
до текстового сообщения. Она продолжает сессию из раздела 4. Поля и значения
подтверждения и уведомлений соответствуют документации v2.

Клиент → агент:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "session/prompt",
  "params": {
    "sessionId": "sess_abc123def456",
    "prompt": [
      {
        "type": "text",
        "text": "Can you analyze this code for potential issues?"
      }
    ]
  }
}
```

Агент подтверждает добавление пользовательского сообщения в беседу. Он возвращает
`messageId`, не дожидаясь завершения работы:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "messageId": "msg_user_8f7a1"
  }
}
```

Агент также сообщает добавленное пользовательское сообщение:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_abc123def456",
    "update": {
      "sessionUpdate": "user_message",
      "messageId": "msg_user_8f7a1",
      "content": [
        {
          "type": "text",
          "text": "Can you analyze this code for potential issues?"
        }
      ]
    }
  }
}
```

Начало работы:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_abc123def456",
    "update": {
      "sessionUpdate": "state_update",
      "state": "running"
    }
  }
}
```

Агент формирует запрос к LLM из истории и выбранных настроек сессии. Ответ модели
может поступать порциями с одним `messageId`:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_abc123def456",
    "update": {
      "sessionUpdate": "agent_message_chunk",
      "messageId": "msg_agent_c42b9",
      "content": {
        "type": "text",
        "text": " Let me examine it..."
      }
    }
  }
}
```

Завершение работы:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_abc123def456",
    "update": {
      "sessionUpdate": "state_update",
      "state": "idle",
      "stopReason": "end_turn"
    }
  }
}
```

Порядок выше иллюстративный: уведомления могут прийти до или после ответа
`session/prompt`. Ответ связывает запрос с добавленным пользовательским сообщением;
состояние и завершение работы клиент определяет по `state_update`. Агент отправляет
ожидающие обновления завершившейся работы до уведомления `idle`.

В `PromptRequest` определены `sessionId`, `prompt` и `_meta`; настроек sampling
в нём нет. Успешный `PromptResponse` содержит `messageId`. Изменение параметров
перед следующим prompt снова выполняется через `session/set_config_option`.

## 8. Обновления конфигурации от агента

Агент может изменить конфигурацию самостоятельно, например переключить модель.
Он сообщает это через `session/update`, где `update.sessionUpdate` имеет значение
`config_option_update`, а `update.configOptions` содержит полный актуальный список.

Агенту рекомендовано отправлять такое обновление на каждом соединении, где сессия
активна, в том числе после настройки через `session/set_config_option`. Для
запрашивающего клиента ответ на метод остаётся источником актуального состояния;
дублирующее уведомление рекомендуется отправлять до ответа, а последующие
изменения — после него.

Клиент применяет ответы и уведомления в порядке получения, каждый раз заменяя
весь список. `config_option_update` не является частичным патчем.

Источник: [Session Config Options](https://agentclientprotocol.com/protocol/v2/session-config-options).

## 9. Получение истории сообщений сессии

В ACP v2 история запрашивается через `session/resume` с
`replayFrom: { "type": "start" }`. Агент воспроизводит сохранённую историю
уведомлениями `session/update` до ответа на запрос. Массив сообщений в самом
`ResumeSessionResponse` не возвращается.

Это одновременно возобновление или синхронизация сессии: отдельного стандартного
метода чтения истории без активации сессии в проверенной схеме нет. Перед запросом
клиент должен завершить `initialize`; агент должен объявить `capabilities.session`.

### 9.1. Найти сессию: `session/list`

Если `sessionId` ещё неизвестен, клиент запрашивает список сессий. Ниже сокращённый
пример по схеме v2:

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "session/list",
  "params": {}
}
```

Агент возвращает метаданные сессий, а не их сообщения:

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "sessions": [
      {
        "sessionId": "sess_789xyz",
        "cwd": "/home/user/project",
        "title": "Question about France"
      }
    ]
  }
}
```

При наличии `nextCursor` следующая страница запрашивается передачей этого значения
в `params.cursor`. Эта пагинация относится к списку сессий, а не к сообщениям
внутри одной сессии.

Источник: [Session List](https://agentclientprotocol.com/protocol/v2/session-list).

### 9.2. Запросить воспроизведение: `session/resume`

Следующий самостоятельный пример использует сессию `sess_789xyz`. В нём клиент
не подключает MCP-серверы. Запрос соответствует примеру из раздела
[Resuming sessions](https://agentclientprotocol.com/protocol/v2/session-setup#resuming-sessions)
с явно указанным пустым `mcpServers` и новым JSON-RPC ID:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "session/resume",
  "params": {
    "sessionId": "sess_789xyz",
    "cwd": "/home/user/project",
    "mcpServers": [],
    "replayFrom": {
      "type": "start"
    }
  }
}
```

`cwd` должен соответствовать рабочей директории этой сессии. Если сессии нужны
MCP-серверы, клиент должен передать их полный актуальный набор: отсутствие
`mcpServers` и `[]` означают пустой набор; агенту не следует восстанавливать его
из предыдущего запроса.

Без `replayFrom`, а также при `replayFrom: null`, агент возобновляет контекст
без воспроизведения предыдущей истории. Для варианта `start` он обязан
воспроизвести всю сохранённую историю до ответа на `session/resume`.

### 9.3. Получить сообщения и дождаться ответа

Примеры двух сообщений ниже взяты из
[Replay contents](https://agentclientprotocol.com/protocol/v2/session-setup#replay-contents).
Сначала агент воспроизводит пользовательское сообщение:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_789xyz",
    "update": {
      "sessionUpdate": "user_message",
      "messageId": "msg_user_8f7a1",
      "content": [
        {
          "type": "text",
          "text": "What's the capital of France?"
        }
      ]
    }
  }
}
```

Затем ответ агента:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_789xyz",
    "update": {
      "sessionUpdate": "agent_message",
      "messageId": "msg_agent_c42b9",
      "content": [
        {
          "type": "text",
          "text": "The capital of France is Paris."
        }
      ]
    }
  }
}
```

После запрошенного replay и до ответа агент обязан отправить актуальный
`state_update`. Например, снимок сессии, в которой сейчас нет foreground-работы:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "sess_789xyz",
    "update": {
      "sessionUpdate": "state_update",
      "state": "idle"
    }
  }
}
```

Затем приходит ответ на `session/resume`. Для сессии, у которой нет объявленных
настроек и команд, он пустой:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {}
}
```

Если сессия имеет настройки или команды, ответ должен содержать их полные
актуальные списки в `configOptions` и `availableCommands`. Клиент заменяет ими
свои списки. Историю он к этому моменту уже получил в уведомлениях.

Пример с `idle` не означает, что возобновление всегда завершает работу агента.
У сессии может продолжаться работа; тогда снимок показывает её текущее состояние.
Сам ответ на `session/resume` не устанавливает состояние `idle`.

### 9.4. Содержимое replay и ограничения

- Воспроизводится вся **сохранённая** история. ACP не требует сохранять каждое
  принятое пользовательское сообщение или хранить его определённый срок.
  Успешный ответ на `session/prompt` подтверждает добавление сообщения в беседу,
  но не гарантирует его последующую доступность при replay.
- Помимо сообщений история может содержать вызовы инструментов, терминалы,
  планы и события сжатия контекста. Агент может воспроизвести последовательность
  обновлений сущности или её итоговое состояние, например целое сообщение
  вместо отдельных chunks.
- Сохранённое сообщение при replay сохраняет свой `messageId`. На клиенте
  сущность сообщения различается по `sessionId`, виду сообщения (`user`,
  `agent`, `thought`) и `messageId`. Полное сообщение обновляет соответствующую
  сущность; chunks дополняют её содержимое. `agent_message` и
  `agent_message_chunk` относятся к одному виду сущности, а не к двум разным.
- Исторические `state_update`, notices и прежние запросы разрешений не являются
  replay истории. Текущее состояние и актуальные ожидания действий пользователя
  синхронизируются отдельно при возобновлении. Исторические значения конфигурации,
  usage и метаданных также не обязаны воспроизводиться.
- Повторный `session/resume` для уже активной на этом соединении сессии снова
  синхронизирует её. Работа продолжается; переданные MCP-серверы и рабочие корни
  заменяют предыдущие. Это следует учитывать при использовании resume для
  повторного получения истории.
- В базовом `ReplayFrom` определён вариант `start`. Неизвестный или
  неподдерживаемый тип курсора, а также некорректный курсор должны приводить к
  `-32602`; агент не должен молча подменять его другим курсором или отключать replay.
- Если агент больше не хранит указанную сессию, `session/resume` возвращает
  `-32002`.

Источники: [Session Setup](https://agentclientprotocol.com/protocol/v2/session-setup),
[ResumeSessionRequest](https://agentclientprotocol.com/protocol/v2/schema#resumesessionrequest),
[ResumeSessionResponse](https://agentclientprotocol.com/protocol/v2/schema#resumesessionresponse),
[Message IDs](https://agentclientprotocol.com/protocol/v2/prompt-lifecycle#message-ids).

<a id="boba-auth-contract"></a>

## 10. Авторизация Boba: доступ к службе до ACP

Принято 2026-11-08: credentials и подключение к модельному провайдеру настроены
заранее; отдельный вход агента через auth/login не нужен. В целевой реализации
initialize возвращает authMethods: [], клиент не вызывает auth/login и auth/logout.

~~~text
Нет действительного credential → HTTP 401 с challenge
Получение/предъявление Bearer → initialize → session/list / new / resume
~~~

Человеческий OAuth использует local/LDAP/Kerberos через boba-auth либо разрешённый
корпоративный issuer. Машина использует фиксированный Bearer или client_credentials.
Proxy — доверенный клиент, сообщающий пользователя и роли подписанными заголовками;
их проверяет тот же boba-auth, что в AID. HTTP-адаптер службы выдаёт access token,
после чего используется обычный ACP. Proxy не проходит через auth/login.

Bearer подтверждает личность и права, Acp-Connection-Id выбирает принадлежащий ей
контекст; сам ID не даёт доступа. Второе состояние входа после проверки credential
не создаётся. Закрытие соединения и отзыв токена — разные действия.

Полный контракт, карта SDK/Boba, примеры HTTP и критерии находятся в
[задании на реализацию](acp-v2-sdk-auth-implementation.md). Там отдельно описаны
текущий код и требуемые изменения. URL-elicitation остаётся механизмом внешних
взаимодействий агента/инструментов, а не обязательным входом клиента в Boba.

## 11. Источники и происхождение примеров

Официальная документация ACP v2:

- [Overview](https://agentclientprotocol.com/protocol/v2/overview).
- [Initialization](https://agentclientprotocol.com/protocol/v2/initialization).
- [Authentication](https://agentclientprotocol.com/protocol/v2/authentication).
- [Elicitation](https://agentclientprotocol.com/protocol/v2/elicitation).
- [Transports](https://agentclientprotocol.com/protocol/v2/transports).
- [Session Setup](https://agentclientprotocol.com/protocol/v2/session-setup).
- [Session List](https://agentclientprotocol.com/protocol/v2/session-list).
- [Session Config Options](https://agentclientprotocol.com/protocol/v2/session-config-options).
- [Prompt Lifecycle](https://agentclientprotocol.com/protocol/v2/prompt-lifecycle).
- [Extensibility](https://agentclientprotocol.com/protocol/v2/extensibility).
- [Schema](https://agentclientprotocol.com/protocol/v2/schema).

Проверенная ревизия исходников:

- [schema/v2/schema.json](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/schema/v2/schema.json).
- [docs/protocol/v2/initialization.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/initialization.mdx).
- [docs/protocol/v2/authentication.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/authentication.mdx).
- [docs/protocol/v2/elicitation.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/elicitation.mdx).
- [docs/protocol/v2/session-config-options.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/session-config-options.mdx).
- [docs/protocol/v2/session-setup.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/session-setup.mdx).
- [docs/protocol/v2/session-list.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/session-list.mdx).
- [docs/protocol/v2/prompt-lifecycle.mdx](https://github.com/agentclientprotocol/agent-client-protocol/blob/7ec5185c18ca928b48e457d2d8a163800be013c8/docs/protocol/v2/prompt-lifecycle.mdx).

Инициализация взята из документации. Ответ с `model-1`, `model-2` и `context_size`
сокращён по списку опций. Запросы смены модели и контекста применяют документированный
метод к этим опциям; ответ на смену модели составлен для показанной сокращённой
сессии. Примеры реальных имён провайдеров, формата `provider/model`, temperature
и top_k —
варианты реализации агента поверх стандартного механизма, а не стандартный каталог
или обязательные параметры ACP.

В разделе истории два сообщения replay взяты из документации. Запрос
`session/resume` дополняет её пример пустым `mcpServers`; JSON-RPC ID изменён
для последовательности примеров. Обмен `session/list` и завершающие сообщения
resume составлены по схеме v2 для показанной сессии без настроек и команд.

Раздел 10 описывает выбранную архитектуру Boba; наличие в ACP методов
аутентификации не требует их объявления, когда отдельный вход агента не нужен.
Примеры HTTP/OAuth и их нормативные источники находятся в задании на реализацию.
Статус сетевого RFD проверен 2026-11-08; его старые иллюстрации RPC не заменяют
закреплённую JSON Schema v2.
