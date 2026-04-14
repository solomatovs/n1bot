# Tools: конкретные реализации

Реализации абстрактных `ToolParams` и `Tool`, определённых в [workspace-architecture.md](workspace-architecture.md#4-tools).

---

## Конкретные ToolParams

### Клиентские инструменты

```python
@dataclass(frozen=True)
class ReadParams(ToolParams):
    """Чтение по строкам."""
    file_path: str
    offset: int | None = None   # номер строки (0-based)
    limit: int | None = None    # количество строк

@dataclass(frozen=True)
class ReadBytesParams(ToolParams):
    """Чтение по байтам."""
    file_path: str
    offset: int | None = None   # байтовое смещение
    limit: int | None = None    # количество байт

@dataclass(frozen=True)
class SearchParams(ToolParams):
    query: str
    top_k: int = 5

@dataclass(frozen=True)
class WriteParams(ToolParams):
    file_path: str
    content: str

@dataclass(frozen=True)
class BashParams(ToolParams):
    command: str
    timeout: int = 120000
```

### Серверные инструменты (Claude API)

Используют те же `ToolCall`/`AssistantToolMessage`/`ToolMessage`.
Адаптер Claude парсит `server_tool_use` → `ToolCall`, `*_tool_result` → `ToolMessage`.

```python
@dataclass(frozen=True)
class WebSearchParams(ToolParams):
    """Серверный веб-поиск."""
    query: str

@dataclass(frozen=True)
class WebFetchParams(ToolParams):
    """Серверная загрузка веб-страницы."""
    url: str

@dataclass(frozen=True)
class CodeExecutionParams(ToolParams):
    """Серверное выполнение Python-кода."""
    code: str

@dataclass(frozen=True)
class BashExecutionParams(ToolParams):
    """Серверное выполнение bash-команды."""
    command: str

@dataclass(frozen=True)
class TextEditorParams(ToolParams):
    """Серверное редактирование файла."""
    command: str       # "create", "str_replace" и др.
    path: str
    file_text: str = ""
    old_str: str = ""
    new_str: str = ""
```

---

## Конкретные Tool реализации

### ReadTool

```python
class ReadTool(Tool[ReadParams]):
    """Построчное чтение файла. offset/limit — в строках."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=ToolId("read"),
            description="Read a file by lines.",
            input_schema=ToolInputSchema(params=[
                ParamSchema(name="file_path", type=JsonType.STRING,
                            description="Absolute path to the file."),
                ParamSchema(name="offset", type=JsonType.INTEGER,
                            description="Line number to start from (0-based).",
                            required=False, default=None),
                ParamSchema(name="limit", type=JsonType.INTEGER,
                            description="Number of lines to read.",
                            required=False, default=None),
            ]),
        )

    @property
    def params_type(self) -> type[ReadParams]:
        return ReadParams

    async def execute(self, params: ReadParams) -> ToolResult:
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)

        start = params.offset or 0
        limit = params.limit

        result_lines: list[str] = []
        with open(path, "r") as f:
            for _ in range(start):
                if f.readline() == "":
                    break

            line_no = start
            while True:
                line = f.readline()
                if line == "":
                    break
                line_no += 1
                result_lines.append(f"{line_no}\t{line}")
                if limit is not None and len(result_lines) >= limit:
                    break

        return ToolResult(content="".join(result_lines))
```

### ReadBytesTool

```python
class ReadBytesTool(Tool[ReadBytesParams]):
    """Чтение файла по байтовому смещению. offset/limit — в байтах.
    Использует seek() — мгновенный переход без сканирования."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=ToolId("read_bytes"),
            description="Read a file by byte offset.",
            input_schema=ToolInputSchema(params=[
                ParamSchema(name="file_path", type=JsonType.STRING,
                            description="Absolute path to the file."),
                ParamSchema(name="offset", type=JsonType.INTEGER,
                            description="Byte offset to start from.",
                            required=False, default=None),
                ParamSchema(name="limit", type=JsonType.INTEGER,
                            description="Number of bytes to read.",
                            required=False, default=None),
            ]),
        )

    @property
    def params_type(self) -> type[ReadBytesParams]:
        return ReadBytesParams

    async def execute(self, params: ReadBytesParams) -> ToolResult:
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)

        with open(path, "r") as f:
            if params.offset:
                f.seek(params.offset)
            chunk = f.read(params.limit) if params.limit else f.read()

        return ToolResult(content=chunk)
```

### WriteTool

```python
class WriteTool(Tool[WriteParams]):
    """Создаёт или полностью перезаписывает файл."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            id=ToolId("write"),
            description="Create or overwrite a file.",
            input_schema=ToolInputSchema(params=[
                ParamSchema(name="file_path", type=JsonType.STRING,
                            description="Absolute path to the file."),
                ParamSchema(name="content", type=JsonType.STRING,
                            description="Content to write."),
            ]),
        )

    @property
    def params_type(self) -> type[WriteParams]:
        return WriteParams

    async def execute(self, params: WriteParams) -> ToolResult:
        path = Path(params.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params.content)
        return ToolResult(content=f"Written {len(params.content)} bytes to {path}")
```
