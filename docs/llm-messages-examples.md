# LLM Messages: API -> Domain Models

Примеры маппинга сообщений из API провайдеров в доменные модели.

---

## Доменные модели (справка)

```python
# Основные
class LLMMessage: pass
class SystemMessage(LLMMessage):        content: str
class DeveloperMessage(LLMMessage):     content: str
class UserMessage(LLMMessage):          content: str
class AssistantMessage(LLMMessage):     content: str
class AssistantToolMessage(LLMMessage): tool_calls: list[ToolCall]
class ToolMessage(LLMMessage):          content: str, tool_call_id: str
class ToolCall:                         id: str, tool_id: ToolId, arguments: TParams

# Thinking (Claude API)
class ThinkingMessage(LLMMessage):          content: str
class RedactedThinkingMessage(LLMMessage):  data: str

# Серверные ToolParams (Claude API) — используют те же AssistantToolMessage/ToolMessage
class WebSearchParams(ToolParams):     query: str
class WebFetchParams(ToolParams):      url: str
class CodeExecutionParams(ToolParams): code: str
class BashExecutionParams(ToolParams): command: str
class TextEditorParams(ToolParams):    command: str, path: str, ...
```

---

## OpenAI API

### System prompt

```json
{"role": "system", "content": "You are a helpful assistant."}
```
```python
SystemMessage(content="You are a helpful assistant.")
```

### Developer prompt (reasoning-модели o1/o3/o4)

```json
{"role": "developer", "content": "Always respond in JSON format."}
```
```python
DeveloperMessage(content="Always respond in JSON format.")
```

### User message

```json
{"role": "user", "content": "Прочитай файл main.py"}
```
```python
UserMessage(content="Прочитай файл main.py")
```

### Assistant: текстовый ответ

```json
{"role": "assistant", "content": "Привет! Чем могу помочь?"}
```
```python
AssistantMessage(content="Привет! Чем могу помочь?")
```

### Assistant: текст + tool calls

```json
{
  "role": "assistant",
  "content": "Сейчас прочитаю файл...",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "read",
        "arguments": "{\"file_path\": \"/home/user/main.py\", \"offset\": 10}"
      }
    }
  ]
}
```
```python
AssistantMessage(content="Сейчас прочитаю файл...")
AssistantToolMessage(tool_calls=[
    ToolCall(
        id="call_abc123",
        tool_id=ToolId("read"),
        arguments=ReadParams(file_path="/home/user/main.py", offset=10),
    )
])
```

Адаптер разбивает одно API-сообщение на два доменных объекта.

### Assistant: только tool calls (content=null)

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_def456",
      "type": "function",
      "function": {
        "name": "bash",
        "arguments": "{\"command\": \"ls -la\"}"
      }
    }
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(
        id="call_def456",
        tool_id=ToolId("bash"),
        arguments=BashParams(command="ls -la"),
    )
])
```

`AssistantMessage` не создаётся — текста нет.

### Assistant: параллельные tool calls

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_111",
      "type": "function",
      "function": {
        "name": "read",
        "arguments": "{\"file_path\": \"/main.py\"}"
      }
    },
    {
      "id": "call_222",
      "type": "function",
      "function": {
        "name": "read",
        "arguments": "{\"file_path\": \"/config.yaml\"}"
      }
    }
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="call_111", tool_id=ToolId("read"),
             arguments=ReadParams(file_path="/main.py")),
    ToolCall(id="call_222", tool_id=ToolId("read"),
             arguments=ReadParams(file_path="/config.yaml")),
])
```

### Tool result

```json
{"role": "tool", "tool_call_id": "call_abc123", "content": "1\tprint('hello')"}
```
```python
ToolMessage(content="1\tprint('hello')", tool_call_id="call_abc123")
```

### Полный цикл диалога (OpenAI)

```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "system", "content": "You are a coding assistant."},
    {"role": "user", "content": "Что в файле main.py?"},
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {"name": "read", "arguments": "{\"file_path\": \"/main.py\"}"}
      }]
    },
    {"role": "tool", "tool_call_id": "call_abc123", "content": "print('hello')"},
    {"role": "assistant", "content": "Файл main.py содержит одну строку: print('hello')"}
  ]
}
```
```python
[
    SystemMessage(content="You are a coding assistant."),
    UserMessage(content="Что в файле main.py?"),
    AssistantToolMessage(tool_calls=[
        ToolCall(id="call_abc123", tool_id=ToolId("read"),
                 arguments=ReadParams(file_path="/main.py")),
    ]),
    ToolMessage(content="print('hello')", tool_call_id="call_abc123"),
    AssistantMessage(content="Файл main.py содержит одну строку: print('hello')"),
]
```

---

## Anthropic Claude API

### Типы content-блоков в ответе assistant

В Claude API `content` — это массив блоков, каждый со своим `type`:

| Type | Описание |
|---|---|
| `text` | Текстовый ответ |
| `tool_use` | Вызов клиентского инструмента |
| `thinking` | Внутренние рассуждения модели (extended thinking) |
| `redacted_thinking` | Отредактированные рассуждения с подписью для multi-turn |
| `server_tool_use` | Вызов серверного инструмента (web_search, code_execution и др.) |
| `web_search_tool_result` | Результат веб-поиска |
| `web_fetch_tool_result` | Результат загрузки веб-страницы |
| `code_execution_tool_result` | Результат выполнения Python-кода |
| `bash_code_execution_tool_result` | Результат bash-команды |
| `text_editor_code_execution_tool_result` | Результат редактирования файла |
| `tool_search_tool_result` | Результат поиска по доступным инструментам |

---

### Маппинг на доменные модели

#### System prompt — отдельный параметр, не сообщение

```json
{
  "model": "claude-sonnet-4-6",
  "system": "You are a coding assistant.",
  "messages": [...]
}
```
```python
SystemMessage(content="You are a coding assistant.")
```

Передаётся адаптером как top-level параметр `system`, а не в массив `messages`.

#### User message

```json
{"role": "user", "content": "Прочитай файл main.py"}
```
```python
UserMessage(content="Прочитай файл main.py")
```

#### Assistant: текстовый ответ (type: text)

```json
{
  "role": "assistant",
  "content": [{"type": "text", "text": "Привет! Чем могу помочь?"}]
}
```
```python
AssistantMessage(content="Привет! Чем могу помочь?")
```

Адаптер извлекает текст из блока `type: "text"`.

#### Assistant: текст + tool use

```json
{
  "role": "assistant",
  "content": [
    {"type": "text", "text": "Сейчас прочитаю файл..."},
    {
      "type": "tool_use",
      "id": "toolu_01A09q",
      "name": "read",
      "input": {"file_path": "/home/user/main.py", "offset": 10}
    }
  ]
}
```
```python
AssistantMessage(content="Сейчас прочитаю файл...")
AssistantToolMessage(tool_calls=[
    ToolCall(
        id="toolu_01A09q",
        tool_id=ToolId("read"),
        arguments=ReadParams(file_path="/home/user/main.py", offset=10),
    )
])
```

#### Assistant: только tool use (без текста)

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_02B",
      "name": "bash",
      "input": {"command": "ls -la"}
    }
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(
        id="toolu_02B",
        tool_id=ToolId("bash"),
        arguments=BashParams(command="ls -la"),
    )
])
```

#### Assistant: параллельные tool use

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_01",
      "name": "read",
      "input": {"file_path": "/main.py"}
    },
    {
      "type": "tool_use",
      "id": "toolu_02",
      "name": "read",
      "input": {"file_path": "/config.yaml"}
    }
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="toolu_01", tool_id=ToolId("read"),
             arguments=ReadParams(file_path="/main.py")),
    ToolCall(id="toolu_02", tool_id=ToolId("read"),
             arguments=ReadParams(file_path="/config.yaml")),
])
```

#### Tool result — внутри user-сообщения

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_01A09q",
      "content": "1\tprint('hello')"
    }
  ]
}
```
```python
ToolMessage(content="1\tprint('hello')", tool_call_id="toolu_01A09q")
```

При сериализации обратно адаптер оборачивает `ToolMessage` в `user`-сообщение с блоком `tool_result`.

#### Полный цикл диалога

```json
{
  "model": "claude-sonnet-4-6",
  "system": "You are a coding assistant.",
  "messages": [
    {"role": "user", "content": "Что в файле main.py?"},
    {
      "role": "assistant",
      "content": [
        {"type": "tool_use", "id": "toolu_01A09q", "name": "read",
         "input": {"file_path": "/main.py"}}
      ]
    },
    {
      "role": "user",
      "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01A09q",
         "content": "print('hello')"}
      ]
    },
    {
      "role": "assistant",
      "content": [
        {"type": "text",
         "text": "Файл main.py содержит одну строку: print('hello')"}
      ]
    }
  ]
}
```
```python
[
    SystemMessage(content="You are a coding assistant."),
    UserMessage(content="Что в файле main.py?"),
    AssistantToolMessage(tool_calls=[
        ToolCall(id="toolu_01A09q", tool_id=ToolId("read"),
                 arguments=ReadParams(file_path="/main.py")),
    ]),
    ToolMessage(content="print('hello')", tool_call_id="toolu_01A09q"),
    AssistantMessage(
        content="Файл main.py содержит одну строку: print('hello')"
    ),
]
```

---

#### Thinking (extended thinking)

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "thinking",
      "thinking": "Мне нужно прочитать файл, чтобы понять его структуру..."
    },
    {"type": "text", "text": "Давайте посмотрим файл."},
    {
      "type": "tool_use",
      "id": "toolu_03C",
      "name": "read",
      "input": {"file_path": "/main.py"}
    }
  ]
}
```
```python
ThinkingMessage(content="Мне нужно прочитать файл, чтобы понять его структуру...")
AssistantMessage(content="Давайте посмотрим файл.")
AssistantToolMessage(tool_calls=[
    ToolCall(id="toolu_03C", tool_id=ToolId("read"),
             arguments=ReadParams(file_path="/main.py")),
])
```

#### Redacted thinking

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "redacted_thinking",
      "data": "W3siYXR0cmlidXRlcyI6ey..."
    },
    {"type": "text", "text": "Вот ответ на ваш вопрос."}
  ]
}
```
```python
RedactedThinkingMessage(data="W3siYXR0cmlidXRlcyI6ey...")
AssistantMessage(content="Вот ответ на ваш вопрос.")
```

#### Server tool: web_search

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "server_tool_use",
      "id": "srvtoolu_01",
      "name": "web_search",
      "input": {"query": "python dataclass frozen"}
    },
    {
      "type": "web_search_tool_result",
      "tool_use_id": "srvtoolu_01",
      "content": [
        {
          "type": "web_search_result",
          "url": "https://docs.python.org/...",
          "title": "dataclasses — Data Classes",
          "snippet": "If frozen is true, assignment to fields..."
        }
      ]
    },
    {"type": "text", "text": "Согласно документации Python..."}
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="srvtoolu_01", tool_id=ToolId("web_search"),
             arguments=WebSearchParams(query="python dataclass frozen")),
])
ToolMessage(content='[{"url": "https://docs.python.org/...", ...}]',
            tool_call_id="srvtoolu_01")
AssistantMessage(content="Согласно документации Python...")
```

#### Server tool: code_execution

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "server_tool_use",
      "id": "srvtoolu_02",
      "name": "code_execution",
      "input": {"code": "print(2 + 2)"}
    },
    {
      "type": "code_execution_tool_result",
      "tool_use_id": "srvtoolu_02",
      "content": {
        "stdout": "4\n",
        "stderr": "",
        "return_code": 0
      }
    },
    {"type": "text", "text": "Результат: 4"}
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="srvtoolu_02", tool_id=ToolId("code_execution"),
             arguments=CodeExecutionParams(code="print(2 + 2)")),
])
ToolMessage(content="4\n", tool_call_id="srvtoolu_02")
AssistantMessage(content="Результат: 4")
```

#### Server tool: bash_code_execution

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "server_tool_use",
      "id": "srvtoolu_03",
      "name": "bash_code_execution",
      "input": {"command": "ls -la /tmp"}
    },
    {
      "type": "bash_code_execution_tool_result",
      "tool_use_id": "srvtoolu_03",
      "content": {
        "stdout": "total 0\ndrwxrwxrwt 2 root root 40 Apr 13 ...",
        "stderr": "",
        "return_code": 0
      }
    },
    {"type": "text", "text": "Содержимое /tmp:..."}
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="srvtoolu_03", tool_id=ToolId("bash_code_execution"),
             arguments=BashExecutionParams(command="ls -la /tmp")),
])
ToolMessage(content="total 0\ndrwxrwxrwt 2 root root 40 Apr 13 ...",
            tool_call_id="srvtoolu_03")
AssistantMessage(content="Содержимое /tmp:...")
```

#### Server tool: web_fetch

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "server_tool_use",
      "id": "srvtoolu_04",
      "name": "web_fetch",
      "input": {"url": "https://example.com"}
    },
    {
      "type": "web_fetch_tool_result",
      "tool_use_id": "srvtoolu_04",
      "content": "<!DOCTYPE html><html>..."
    },
    {"type": "text", "text": "Страница содержит..."}
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="srvtoolu_04", tool_id=ToolId("web_fetch"),
             arguments=WebFetchParams(url="https://example.com")),
])
ToolMessage(content="<!DOCTYPE html><html>...", tool_call_id="srvtoolu_04")
AssistantMessage(content="Страница содержит...")
```

#### Server tool: text_editor_code_execution

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "server_tool_use",
      "id": "srvtoolu_05",
      "name": "text_editor_code_execution",
      "input": {
        "command": "str_replace",
        "path": "/main.py",
        "old_str": "name: str",
        "new_str": "name: str = \"default\""
      }
    },
    {
      "type": "text_editor_code_execution_tool_result",
      "tool_use_id": "srvtoolu_05",
      "content": "Successfully replaced text in /main.py"
    },
    {"type": "text", "text": "Добавил значение по умолчанию для поля name."}
  ]
}
```
```python
AssistantToolMessage(tool_calls=[
    ToolCall(id="srvtoolu_05", tool_id=ToolId("text_editor_code_execution"),
             arguments=TextEditorParams(
                 command="str_replace", path="/main.py",
                 old_str="name: str", new_str="name: str = \"default\"",
             )),
])
ToolMessage(content="Successfully replaced text in /main.py",
            tool_call_id="srvtoolu_05")
AssistantMessage(content="Добавил значение по умолчанию для поля name.")
```

#### Полный цикл с thinking + server tools + client tools

```json
{
  "model": "claude-sonnet-4-6",
  "system": "You are a coding assistant.",
  "messages": [
    {"role": "user", "content": "Найди документацию по dataclass и прочитай main.py"},
    {
      "role": "assistant",
      "content": [
        {"type": "thinking", "thinking": "Нужно сначала найти документацию, потом прочитать файл..."},
        {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search",
         "input": {"query": "python dataclass"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01",
         "content": [{"type": "web_search_result", "url": "https://docs.python.org/...",
                       "title": "dataclasses", "snippet": "..."}]},
        {"type": "text", "text": "Нашёл документацию. Теперь прочитаю файл."},
        {"type": "tool_use", "id": "toolu_01", "name": "read",
         "input": {"file_path": "/main.py"}}
      ]
    },
    {
      "role": "user",
      "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01",
         "content": "from dataclasses import dataclass\n\n@dataclass\nclass Config:\n    name: str"}
      ]
    },
    {
      "role": "assistant",
      "content": [
        {"type": "text", "text": "Файл main.py содержит dataclass Config с полем name."}
      ]
    }
  ]
}
```
```python
[
    SystemMessage(content="You are a coding assistant."),
    UserMessage(content="Найди документацию по dataclass и прочитай main.py"),
    ThinkingMessage(content="Нужно сначала найти документацию, потом прочитать файл..."),
    AssistantToolMessage(tool_calls=[
        ToolCall(id="srvtoolu_01", tool_id=ToolId("web_search"),
                 arguments=WebSearchParams(query="python dataclass")),
    ]),
    ToolMessage(content='[{"url": "https://docs.python.org/...", ...}]',
                tool_call_id="srvtoolu_01"),
    AssistantMessage(content="Нашёл документацию. Теперь прочитаю файл."),
    AssistantToolMessage(tool_calls=[
        ToolCall(id="toolu_01", tool_id=ToolId("read"),
                 arguments=ReadParams(file_path="/main.py")),
    ]),
    ToolMessage(
        content="from dataclasses import dataclass\n\n@dataclass\nclass Config:\n    name: str",
        tool_call_id="toolu_01",
    ),
    AssistantMessage(
        content="Файл main.py содержит dataclass Config с полем name."
    ),
]
```

---

## Различия адаптеров

| | OpenAI | Claude |
|---|---|---|
| System prompt | `{"role": "system", ...}` в messages | top-level параметр `system` |
| Developer prompt | `{"role": "developer", ...}` | нет (сериализуется как system) |
| Tool call args | **строка** → `json.loads` → dict → TParams | **dict** → TParams |
| Tool result | `{"role": "tool", ...}` | `{"role": "user", content: [{type: "tool_result"}]}` |
| Content формат | строка или null | массив блоков `[{type, text/...}]` |
| Разбиение text+tools | одно сообщение с обоими полями | один блок `content` с text и tool_use |
| Thinking | нет | `type: "thinking"` / `"redacted_thinking"` |
| Server tools | нет | `server_tool_use` → `AssistantToolMessage`, `*_tool_result` → `ToolMessage` |

Доменные модели одинаковые — разница инкапсулирована в адаптерах.
