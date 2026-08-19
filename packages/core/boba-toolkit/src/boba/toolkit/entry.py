"""Вход модуля инструментов и контракт вызова: адрес, argv, конверт, CLI.

Одна и та же команда `python -m <модуль> <имя> --флаги` исполняется под
launcher'ом приложения и человеком в терминале; разница только в источнике
Injected-конфига (stdin против --config) и приёмнике результата (конверт в
fd из env против stdout).

Ошибки:
ArgumentTooLargeError — значение аргумента не помещается в argv (MAX_ARG_STRLEN).
ToolEntryError — нарушен контракт запуска: имени нет в TOOLS, флаги или конфиг
    не прошли валидацию; kind из EntryErrorKind.
PayloadFailureError — исполненное тело подняло ожидаемое исключение (EXPECTED
    модуля); прочие исключения тела уходят наверх как есть.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import IntEnum, StrEnum
from types import NoneType, UnionType
from typing import (
    Any,
    ClassVar,
    Protocol,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.toolkit.calls import ToolCallView, ToolCallViews
from boba.toolkit.channels import ToolChannel
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.protocol import ReplyError, ReplyOk, ToolCommand
from boba.toolkit.timing import Elapsed, ProcessAge

__all__ = [
    "ArgumentTooLargeError",
    "EntryErrorKind",
    "ExpectedErrors",
    "ToolAddress",
    "ToolArgv",
    "ToolEntryError",
    "ToolLike",
    "ToolMain",
]


logger = logging.getLogger(__name__)


class ArgumentTooLargeError(Exception):
    """Значение аргумента не помещается в один элемент argv."""

    def __init__(self, param: str, size: int, limit: int) -> None:
        msg = f"argument {param!r} is {size} bytes, argv value limit is {limit}"
        super().__init__(msg)
        self.param = param


class EntryErrorKind(StrEnum):
    """Отказы контракта запуска; с доменными kind'ами не пересекаются."""

    UNKNOWN_TOOL = "unknown_tool"
    INVALID_REQUEST = "invalid_request"
    INTERNAL_ERROR = "internal_error"


class ToolEntryError(Exception):
    """Нарушение контракта запуска с классификацией для конверта."""

    def __init__(self, kind: EntryErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ToolLike(Protocol):
    """Tool-объект duck-typing'ом; toolkit не зависит от langchain.

    Только read-only свойства: mutable-атрибут протокола инвариантен, и
    StructuredTool с его `args_schema: ArgsSchema | None` его не проходит.
    """

    @property
    def name(self) -> str: ...

    @property
    def args_schema(self) -> Any: ...

    @property
    def func(self) -> Callable[..., Any] | None: ...

    @property
    def coroutine(self) -> Callable[..., Awaitable[Any]] | None: ...


class ToolAddress(BaseModel):
    """Адрес инструмента: модуль тела и имя в TOOLS.

    Захватывается при постановке обёртки запуска, пока тело не подменено;
    вычислять на вызове нельзя — __module__ обёртки укажет не туда.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(min_length=1)
    name: str = Field(min_length=1)

    @classmethod
    def of(cls, tool: ToolLike) -> ToolAddress:
        body = tool.coroutine or tool.func
        if body is None:
            msg = f"tool {tool.name!r} has neither coroutine nor func"
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        return cls(module=body.__module__, name=tool.name)

    PYTHON: ClassVar[str] = "python3"
    """Интерпретатор по PATH окружения запуска: python песочницы — не python
    приложения, абсолютный sys.executable внутрь не переносится."""

    def argv_head(self) -> list[str]:
        return [self.PYTHON, "-m", self.module, self.name]


class ExpectedErrors:
    """Ожидаемые исключения модуля инструментов: разбор и применение EXPECTED."""

    ATTRIBUTE: ClassVar[str] = "EXPECTED"

    @classmethod
    def of_body(cls, body: Callable[..., object]) -> Mapping[type[Exception], str]:
        """Карта EXPECTED модуля тела; модуль без атрибута ошибок не ожидает."""
        module = sys.modules.get(body.__module__)
        if module is None:
            return {}

        mapping = getattr(module, cls.ATTRIBUTE, None)
        if not isinstance(mapping, Mapping):
            return {}

        return {
            declared: str(kind)
            for declared, kind in mapping.items()
            if isinstance(declared, type) and issubclass(declared, Exception)
        }

    @staticmethod
    def kind_of(error: Exception, mapping: Mapping[type[Exception], str]) -> str | None:
        """Kind первого совпадения по MRO; None — исключение неожиданное."""
        for klass in type(error).__mro__:
            for declared, kind in mapping.items():
                if klass is declared:
                    return kind

        return None


class ToolArgv:
    """kwargs <-> argv по args_schema: одна логика у обёртки запуска и CLI.

    Правило одно: параметр, видимый LLM, — флаг argv; параметр с injected-
    метадатой — ключ в JSON на stdin. Текстовые значения едут как есть,
    остальные — JSON'ом в значении флага.
    """

    MAX_VALUE_BYTES: ClassVar[int] = 131_071
    """MAX_ARG_STRLEN минус завершающий нуль; ровно 131072 даёт E2BIG."""

    INJECTED_MARKERS: ClassVar[frozenset[str]] = frozenset(
        {"Injected", "InjectedToolArg", "InjectedToolCallId"}
    )
    """Имена injected-маркеров по MRO: Injected — свой (facade), остальные —
    langchain-метадата; её типов toolkit не импортирует."""

    @classmethod
    def render(
        cls,
        address: ToolAddress,
        schema: type[BaseModel],
        kwargs: Mapping[str, object],
    ) -> ToolCommand:
        """LLM-аргументы во флаги, injected-параметры в stdin."""
        argv = address.argv_head()

        stdin_payload: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if name not in kwargs:
                continue

            value = kwargs[name]
            if cls._injected(field.metadata):
                stdin_payload[name] = cls._reveal(field.annotation, value)
                continue

            if value is None:
                continue

            encoded = cls._encode(name, field.annotation, value)
            argv.append(cls.flag_of(name))
            argv.append(encoded)

        stdin = json.dumps(stdin_payload, ensure_ascii=False).encode("utf-8")
        return ToolCommand(argv=tuple(argv), stdin=stdin)

    @classmethod
    def parse(
        cls,
        tool: ToolLike,
        argv: Sequence[str],
        stdin: bytes,
    ) -> dict[str, Any]:
        """Обратный разбор: флаги и stdin в kwargs вызова тела."""
        schema = cls.schema_of(tool)
        by_flag: dict[str, str] = {}
        for name in schema.model_fields:
            by_flag[cls.flag_of(name)] = name

        kwargs: dict[str, Any] = {}
        pending = list(argv)
        while pending:
            flag = pending.pop(0)
            name = by_flag.get(flag)
            if name is None:
                msg = f"unknown flag {flag!r} for tool {tool.name!r}"
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            if not pending:
                msg = f"flag {flag!r} has no value"
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            raw = pending.pop(0)
            field = schema.model_fields[name]
            kwargs[name] = cls._decode(name, field.annotation, raw)

        kwargs.update(cls._parse_injected(tool, schema, stdin))
        return kwargs

    @classmethod
    def schema_of(cls, tool: ToolLike) -> type[BaseModel]:
        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            msg = f"tool {tool.name!r} has no pydantic args_schema"
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        return schema

    @classmethod
    def injected_fields(cls, schema: type[BaseModel]) -> dict[str, Any]:
        """Injected-параметры схемы: имя -> аннотация."""
        fields: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if cls._injected(field.metadata):
                fields[name] = field.annotation

        return fields

    @staticmethod
    def flag_of(param: str) -> str:
        return "--" + param.replace("_", "-")

    @classmethod
    def _injected(cls, metadata: Sequence[Any]) -> bool:
        for item in metadata:
            klass = item if isinstance(item, type) else type(item)
            names = {parent.__name__ for parent in klass.__mro__}
            if names & cls.INJECTED_MARKERS:
                return True

        return False

    @classmethod
    def _encode(cls, name: str, annotation: Any, value: object) -> str:
        if cls._texty(annotation):
            encoded = str(value)
        else:
            encoded = TypeAdapter(annotation).dump_json(value).decode("utf-8")

        size = len(encoded.encode("utf-8"))
        if size > cls.MAX_VALUE_BYTES:
            raise ArgumentTooLargeError(name, size, cls.MAX_VALUE_BYTES)

        return encoded

    @classmethod
    def _decode(cls, name: str, annotation: Any, raw: str) -> Any:
        try:
            if cls._texty(annotation):
                return TypeAdapter(annotation).validate_python(raw)
            return TypeAdapter(annotation).validate_json(raw)
        except (ValidationError, ValueError) as exc:
            msg = f"invalid value for {name!r}: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

    @staticmethod
    def _texty(annotation: Any) -> bool:
        """Строковый параметр: значение едет как есть, без JSON-кавычек."""
        if annotation is str:
            return True

        if get_origin(annotation) in (Union, UnionType):
            return set(get_args(annotation)) == {str, NoneType}

        return False

    @staticmethod
    def _reveal(annotation: Any, value: object) -> Any:
        """JSON-совместимый дамп injected-значения с раскрытыми секретами."""
        revealed = getattr(value, "revealed", None)
        if callable(revealed):
            return revealed()

        return TypeAdapter(annotation).dump_python(value, mode="json")

    @classmethod
    def _parse_injected(
        cls, tool: ToolLike, schema: type[BaseModel], stdin: bytes
    ) -> dict[str, Any]:
        injected = cls.injected_fields(schema)
        if not injected:
            return {}

        try:
            payload = json.loads(stdin.decode("utf-8")) if stdin else {}
        except ValueError as exc:
            msg = f"stdin config is not valid JSON: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        if not isinstance(payload, dict):
            msg = "stdin config must be a JSON object keyed by parameter names"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        kwargs: dict[str, Any] = {}
        for name, annotation in injected.items():
            if name not in payload:
                msg = f"injected parameter {name!r} is missing from stdin config"
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            try:
                kwargs[name] = TypeAdapter(annotation).validate_python(payload[name])
            except ValidationError as exc:
                msg = f"invalid config for {name!r}: {exc}"
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        return kwargs


class ToolMain:
    """CLI модуля инструментов: argv -> тело -> конверт либо вывод человеку.

    Конверт пишется в fd из env-переменной канала tool_result, когда она
    есть, — так зовёт launcher; без неё content печатается в stdout — так
    зовёт человек. Injected-конфиг приезжает JSON'ом со stdin либо
    собирается из toml по --config; источники взаимоисключающие.
    """

    CONFIG_FLAG: ClassVar[str] = "--config"
    ARTIFACT_FLAG: ClassVar[str] = "--artifact"
    HELP_FLAG: ClassVar[str] = "--help"

    class Exit(IntEnum):
        OK = 0
        EXPECTED_FAILURE = 1
        ENTRY_ERROR = 2

    REQUIRED_ATTRIBUTES: ClassVar[tuple[str, ...]] = (
        "name",
        "args_schema",
        "func",
        "coroutine",
    )

    @classmethod
    def toolset(
        cls,
        *tools: object,
        views: Mapping[str, ToolCallView] | None = None,
    ) -> tuple[ToolLike, ...]:
        """Кортеж TOOLS из tool-объектов с проверкой duck-полей.

        Декоратор @tool статически отдаёт BaseTool без func/coroutine —
        мост к ToolLike делается здесь, один раз на модуль.

        views — представления вызовов инструментов модуля: имя тула ->
        вариант ToolCallView. Неперечисленные показываются как JsonCall.
        Имя вне модуля — ошибка: опечатка не должна тихо оставить дефолт.
        """
        checked: list[ToolLike] = []
        for tool in tools:
            for attribute in cls.REQUIRED_ATTRIBUTES:
                if not hasattr(tool, attribute):
                    msg = f"{tool!r} is not a tool object: no {attribute!r}"
                    raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

            accepted: Any = tool
            checked.append(accepted)

        if views:
            cls._register_views(checked, views)

        return tuple(checked)

    @classmethod
    def _register_views(
        cls, tools: Sequence[ToolLike], views: Mapping[str, ToolCallView]
    ) -> None:
        names = {tool.name for tool in tools}

        for tool_name, view in views.items():
            if tool_name not in names:
                known = ", ".join(sorted(names))
                msg = f"call view for unknown tool: {tool_name!r} (module has {known})"
                raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

            ToolCallViews.register(tool_name, view)

    LOG_FORMAT: ClassVar[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    @classmethod
    def run(cls, tools: Sequence[ToolLike], argv: Sequence[str] | None = None) -> int:
        arguments = list(sys.argv[1:]) if argv is None else list(argv)

        cls._setup_logging()

        # возраст процесса = вложенный bwrap + python + импорты модуля тела
        logger.info(
            "payload up %dms after exec (bwrap + python + imports)",
            ProcessAge.ms(),
        )

        try:
            return cls._run(tools, arguments)
        except ToolEntryError as exc:
            cls._emit_error(str(exc.kind), str(exc))
            return cls.Exit.ENTRY_ERROR
        except PayloadFailureError as exc:
            cls._emit_error(exc.kind, str(exc))
            return cls.Exit.EXPECTED_FAILURE

    @classmethod
    def _setup_logging(cls) -> None:
        """Логи тела — в stdout процесса: он журналируется и стримится в панель.

        Конверт уезжает отдельным дескриптором, stdout протоколом не занят;
        уровень приходит от хоста переменной окружения (BOBA_LOG_LEVEL).
        """
        from boba.toolkit.payload import PayloadLogging  # noqa: PLC0415

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(cls.LOG_FORMAT))
        logging.basicConfig(
            level=PayloadLogging.level(), handlers=[handler], force=True
        )

    @classmethod
    def _run(cls, tools: Sequence[ToolLike], arguments: list[str]) -> int:
        if not arguments or arguments == [cls.HELP_FLAG]:
            print(cls._tools_help(tools))  # noqa: T201
            return cls.Exit.OK

        name = arguments.pop(0)
        tool = cls._lookup(tools, name)

        if cls.HELP_FLAG in arguments:
            print(cls._tool_help(tool))  # noqa: T201
            return cls.Exit.OK

        want_artifact = cls.ARTIFACT_FLAG in arguments
        if want_artifact:
            arguments.remove(cls.ARTIFACT_FLAG)

        config_path = cls._pop_config(arguments)

        config_read = Elapsed()
        stdin = cls._config_source(tool, config_path)
        kwargs = ToolArgv.parse(tool, arguments, stdin)
        logger.info(
            "tool[%s]: args ready in %dms (config %d bytes)",
            tool.name,
            config_read.ms(),
            len(stdin),
        )

        reply = cls._call(tool, kwargs)
        return cls._deliver(reply, want_artifact)

    @classmethod
    def _lookup(cls, tools: Sequence[ToolLike], name: str) -> ToolLike:
        for tool in tools:
            if tool.name == name:
                return tool

        known = ", ".join(sorted(tool.name for tool in tools))
        msg = f"unknown tool {name!r}; known tools: {known}"
        raise ToolEntryError(EntryErrorKind.UNKNOWN_TOOL, msg)

    @classmethod
    def _pop_config(cls, arguments: list[str]) -> str | None:
        if cls.CONFIG_FLAG not in arguments:
            return None

        index = arguments.index(cls.CONFIG_FLAG)
        if index + 1 >= len(arguments):
            msg = f"{cls.CONFIG_FLAG} requires a path"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        arguments.pop(index)
        return arguments.pop(index)

    @classmethod
    def _config_source(cls, tool: ToolLike, config_path: str | None) -> bytes:
        """Injected-конфиг: JSON со stdin либо сборка из toml по SECTION."""
        schema = ToolArgv.schema_of(tool)
        injected = ToolArgv.injected_fields(schema)
        if not injected:
            return b"{}"

        if config_path is not None:
            return cls._config_from_toml(injected, config_path)

        data = sys.stdin.buffer.read()
        if not data:
            msg = "injected config expected on stdin (or pass --config <toml>)"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        return data

    @classmethod
    def _config_from_toml(cls, injected: Mapping[str, Any], path: str) -> bytes:
        """Сборка моделей из toml приложения; каждая знает свою секцию SECTION.

        omegaconf импортируется лениво: под launcher'ом эта ветка не
        исполняется, а тянуть его в песочницу нельзя.
        """
        from pathlib import Path  # noqa: PLC0415

        from boba.settings import bind, build_app_config  # noqa: PLC0415

        raw = build_app_config(config_path=Path(path))

        payload: dict[str, Any] = {}
        for name, annotation in injected.items():
            section = getattr(annotation, "SECTION", None)
            if not isinstance(section, str):
                msg = (
                    f"injected parameter {name!r} has no SECTION; "
                    f"it cannot be built from --config"
                )
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            model = bind(raw, section, annotation)
            payload[name] = ToolArgv._reveal(annotation, model)

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @classmethod
    def _call(cls, tool: ToolLike, kwargs: dict[str, Any]) -> ReplyOk:
        body = tool.coroutine or tool.func
        if body is None:
            msg = f"tool {tool.name!r} has no body"
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        expected = ExpectedErrors.of_body(body)

        elapsed = Elapsed()
        try:
            if tool.coroutine is not None:
                result = asyncio.run(cls._acall(tool.coroutine, kwargs))
            else:
                result = body(**kwargs)
        except Exception as exc:
            logger.info("tool[%s]: body failed in %dms", tool.name, elapsed.ms())

            kind = ExpectedErrors.kind_of(exc, expected)
            if kind is None:
                raise

            raise PayloadFailureError(kind, str(exc)) from exc

        logger.info("tool[%s]: body finished in %dms", tool.name, elapsed.ms())

        return cls._pack(tool, result)

    @staticmethod
    async def _acall(
        coroutine: Callable[..., Awaitable[Any]], kwargs: dict[str, Any]
    ) -> Any:
        return await coroutine(**kwargs)

    @classmethod
    def _pack(cls, tool: ToolLike, result: object) -> ReplyOk:
        if not isinstance(result, tuple) or len(result) != 2:  # noqa: PLR2004
            msg = (
                f"tool {tool.name!r} must return (content, artifact), "
                f"got {type(result).__name__}"
            )
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        content, artifact = result

        try:
            return ReplyOk(content=str(content), artifact=artifact)
        except ValidationError as exc:
            msg = f"tool {tool.name!r} artifact is not a ToolResult: {exc}"
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg) from exc

    @classmethod
    def _deliver(cls, reply: ReplyOk, want_artifact: bool) -> int:
        fd = cls._result_fd()
        if fd is not None:
            cls._write_envelope(fd, reply)
            return cls.Exit.OK

        print(reply.content)  # noqa: T201
        if want_artifact:
            print(reply.artifact.model_dump_json())  # noqa: T201

        return cls.Exit.OK

    @classmethod
    def _emit_error(cls, kind: str, message: str) -> None:
        reply = ReplyError(kind=kind, message=message)

        fd = cls._result_fd()
        if fd is not None:
            cls._write_envelope(fd, reply)
            return

        print(f"{kind}: {message}", file=sys.stderr)  # noqa: T201

    @staticmethod
    def _result_fd() -> int | None:
        raw = os.environ.get(ToolChannel.RESULT.env_name)
        if raw is None:
            return None

        return int(raw)

    @staticmethod
    def _write_envelope(fd: int, reply: ReplyOk | ReplyError) -> None:
        # fd унаследован от launcher: закрывает его вызывающий, не этот writer
        with os.fdopen(fd, "wb", closefd=False) as channel:
            channel.write(reply.model_dump_json().encode("utf-8"))

    @classmethod
    def _tools_help(cls, tools: Sequence[ToolLike]) -> str:
        names = ", ".join(sorted(tool.name for tool in tools))
        return f"tools: {names}"

    @classmethod
    def _tool_help(cls, tool: ToolLike) -> str:
        schema = ToolArgv.schema_of(tool)

        lines: list[str] = []
        body = tool.coroutine or tool.func
        if body is not None:
            doc = inspect.getdoc(body)
            if doc:
                lines.append(doc.splitlines()[0])

        for name, field in schema.model_fields.items():
            if ToolArgv._injected(field.metadata):
                continue

            description = field.description or ""
            lines.append(f"  {ToolArgv.flag_of(name)} {description}".rstrip())

        config_help = "application toml with injected config sections"
        lines.append(f"  {cls.CONFIG_FLAG} PATH  {config_help}")
        return "\n".join(lines)
