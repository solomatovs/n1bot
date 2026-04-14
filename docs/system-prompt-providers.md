# System Prompt: конкретные реализации провайдеров

Реализации абстрактного `SystemPromptProvider`, определённого в [workspace-architecture.md](workspace-architecture.md#3-system-prompt).

---

## StaticPromptProvider

Фиксированный текст, зашитый в код.

```python
class StaticPromptProvider(SystemPromptProvider):

    def __init__(self, id: SystemPromptId, priority: int, content: str) -> None:
        self._id = id
        self._priority = priority
        self._content = content

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        return SystemPromptBlock(name=self.id.name, content=self._content)
```

---

## FilePromptProvider

Читает блок из файла на диске. Держит ref count на workspace — защищает от удаления.

```python
class FilePromptProvider(SystemPromptProvider):

    def __init__(self, id: SystemPromptId, priority: int,
                 workspace: WorkspaceId,
                 folder: Path, file_name: str,
                 default_prompt: str = "") -> None:
        self._id = id
        self._priority = priority
        self._workspace = workspace
        self._folder = folder
        self._file_name = file_name
        self._default_prompt = default_prompt

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def path(self) -> Path:
        return self._folder / self._file_name

    async def enter(self) -> None:
        await self._workspace.acquire()

    async def close(self) -> None:
        await self._workspace.release()

    async def build(self) -> SystemPromptBlock:
        if self.path.exists():
            content = self.path.read_text()
        else:
            content = self._default_prompt
        return SystemPromptBlock(name=self.id.name, content=content)
```

---

## EnvironmentPromptProvider

Информация о среде выполнения.

```python
class EnvironmentPromptProvider(SystemPromptProvider):

    def __init__(self) -> None:
        self._id = SystemPromptId("environment")
        self._priority = 60

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        lines = [
            f"Platform: {platform.system()}",
            f"Shell: {os.environ.get('SHELL', 'unknown')}",
            f"OS Version: {platform.release()}",
            f"Current date: {date.today().isoformat()}",
        ]
        return SystemPromptBlock(name=self.id.name, content="\n".join(lines))
```

---

## GitPromptProvider

Текущее состояние git.

```python
class GitPromptProvider(SystemPromptProvider):

    def __init__(self) -> None:
        self._id = SystemPromptId("git_status")
        self._priority = 80

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        branch = await run("git branch --show-current")
        status = await run("git status --short")
        log = await run("git log --oneline -5")
        content = (
            f"Current branch: {branch}\n\n"
            f"Status:\n{status}\n\n"
            f"Recent commits:\n{log}"
        )
        return SystemPromptBlock(name=self.id.name, content=content)
```

---

## IDEPromptProvider

Инструкции, специфичные для IDE.

```python
class IDEPromptProvider(SystemPromptProvider):

    def __init__(self, ide_type: str) -> None:
        self._id = SystemPromptId("ide")
        self._priority = 70
        self._ide_type = ide_type

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority
```

---

## SkillsPromptProvider

Описание доступных skills (slash-commands).

```python
class SkillsPromptProvider(SystemPromptProvider):

    def __init__(self, skill_registry: SkillRegistry) -> None:
        self._id = SystemPromptId("skills")
        self._priority = 110
        self._skill_registry = skill_registry

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        lines = ["Available skills:"]
        for skill in self._skill_registry:
            lines.append(f"- {skill.name}: {skill.description}")
        return SystemPromptBlock(name=self.id.name, content="\n".join(lines))
```

---

## CallbackPromptProvider

Произвольная логика через callback.

```python
class CallbackPromptProvider(SystemPromptProvider):

    def __init__(self, id: SystemPromptId, priority: int,
                 callback: Callable[[], Awaitable[str]]) -> None:
        self._id = id
        self._priority = priority
        self._callback = callback

    @property
    def id(self) -> SystemPromptId:
        return self._id

    @property
    def priority(self) -> int:
        return self._priority

    async def build(self) -> SystemPromptBlock:
        content = await self._callback()
        return SystemPromptBlock(name=self.id.name, content=content)
```

---

## Таблица провайдеров (порядок сборки)

| Priority | Provider | Источник | Когда меняется |
|---|---|---|---|
| 0 | `StaticPromptProvider("identity")` | код | при обновлении версии |
| 10 | `StaticPromptProvider("security")` | код | при обновлении версии |
| 20 | `StaticPromptProvider("tool_rules")` | код | при обновлении версии |
| 30 | `StaticPromptProvider("task_guide")` | код | при обновлении версии |
| 40 | `StaticPromptProvider("git_guide")` | код | при обновлении версии |
| 50 | `StaticPromptProvider("tone")` | код | при обновлении версии |
| 60 | `EnvironmentPromptProvider` | OS, runtime | при каждом `build()` |
| 70 | `IDEPromptProvider` | тип IDE | при старте сессии |
| 80 | `GitPromptProvider` | git CLI | при каждом `build()` |
| 90 | `FilePromptProvider("boba_md")` | `BOBA.md` | при изменении файла |
| 100 | `FilePromptProvider("memory")` | `MEMORY.md` | при изменении файла |
| 110 | `SkillsPromptProvider` | реестр skills | при регистрации |
