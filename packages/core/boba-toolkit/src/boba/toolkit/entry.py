"""Вход модуля инструментов и контракт вызова: адрес, argv, конверт, CLI.

Одна и та же команда `python -m <модуль> <имя> --флаги` исполняется под
launcher'ом приложения и человеком в терминале; разница в источнике
Injected-конфига (кадр config от лончера, stdin либо файл --injected у
человека) и приёмнике результата (конверт в fd из env против stdout).

Ошибки:
ArgumentTooLargeError — значение аргумента не помещается в argv (MAX_ARG_STRLEN).
ToolEntryError — нарушен контракт запуска: имени нет в TOOLS, флаги или конфиг
    не прошли валидацию, файл --injected не читается; kind из EntryErrorKind.
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
from pathlib import Path
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
from boba.toolkit.failure import ValidationText
from boba.toolkit.frames import FrameProtocolError, ToolIo
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.protocol import ReplyError, ReplyOk, ToolCommand
from boba.toolkit.timing import Elapsed

__all__ = [
    "ArgumentTooLargeError",
    "EntryErrorKind",
    "EntryFlag",
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


class EntryFlag(StrEnum):
    """Флаги CLI модуля инструментов, не относящиеся к параметрам тела."""

    INJECTED = "--injected"
    ARTIFACT = "--artifact"
    HELP = "--help"


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
    метадатой — ключ в JSON конфига, который едет телу первым кадром входа.
    Текстовые значения едут как есть, остальные — JSON'ом в значении флага.

    Особый случай — параметр типа ToolIo: это среда вызова, а не значение.
    Хост его не сериализует, гость подставляет свой объект.
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
        """LLM-аргументы во флаги, injected-параметры в конфиг вызова."""
        argv = address.argv_head()

        config_payload: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if cls.is_io(field.annotation):
                continue

            if name not in kwargs:
                continue

            value = kwargs[name]
            if cls._injected(field.metadata):
                config_payload[name] = cls.reveal(field.annotation, value)
                continue

            if value is None:
                continue

            encoded = cls._encode(name, field.annotation, value)
            argv.append(cls.flag_of(name))
            argv.append(encoded)

        config = json.dumps(config_payload, ensure_ascii=False).encode("utf-8")
        return ToolCommand(argv=tuple(argv), config=config)

    @classmethod
    def parse(
        cls,
        tool: ToolLike,
        argv: Sequence[str],
        config: bytes,
    ) -> dict[str, Any]:
        """Обратный разбор: флаги и конфиг вызова в kwargs тела."""
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

        kwargs.update(cls._parse_injected(tool, schema, config))
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
        """Injected-параметры схемы: имя -> аннотация. Среда вызова не в счёт."""
        fields: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if cls.is_io(field.annotation):
                continue

            if cls._injected(field.metadata):
                fields[name] = field.annotation

        return fields

    @classmethod
    def io_field(cls, schema: type[BaseModel]) -> str:
        """Имя параметра среды вызова; пусто — тело её не просило."""
        for name, field in schema.model_fields.items():
            if cls.is_io(field.annotation):
                return name

        return ""

    @staticmethod
    def is_io(annotation: Any) -> bool:
        """Параметр — среда вызова: значение подставляет гость, а не хост."""
        return annotation is ToolIo

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
        except ValidationError as exc:
            # значение не пересказываем: в cfg инструмента едут пароли и токены,
            # а traceback печатает причину сам, мимо FailureText
            msg = f"invalid value for {name!r}: {ValidationText.of(exc)}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from None
        except ValueError as exc:
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
    def section_of(name: str, annotation: Any) -> str:
        """Секция toml, из которой собирается injected-модель параметра."""
        section = getattr(annotation, "SECTION", None)
        if not isinstance(section, str):
            msg = f"injected parameter {name!r} has no SECTION on its model"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        if not isinstance(annotation, type):
            msg = f"injected parameter {name!r} is not a pydantic model"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        if not issubclass(annotation, BaseModel):
            msg = f"injected parameter {name!r} is not a pydantic model"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        return section

    @staticmethod
    def reveal(annotation: Any, value: object) -> Any:
        """JSON-совместимый дамп injected-значения с раскрытыми секретами."""
        revealed = getattr(value, "revealed", None)
        if callable(revealed):
            return revealed()

        return TypeAdapter(annotation).dump_python(value, mode="json")

    @classmethod
    def _parse_injected(
        cls, tool: ToolLike, schema: type[BaseModel], config: bytes
    ) -> dict[str, Any]:
        injected = cls.injected_fields(schema)
        if not injected:
            return {}

        try:
            payload = json.loads(config.decode("utf-8")) if config else {}
        except ValueError as exc:
            msg = f"call config is not valid JSON: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        if not isinstance(payload, dict):
            msg = "call config must be a JSON object keyed by parameter names"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        kwargs: dict[str, Any] = {}
        for name, annotation in injected.items():
            if name not in payload:
                msg = f"injected parameter {name!r} is missing from the call config"
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
    зовёт человек. Injected-конфиг приезжает первым кадром входа (kind
    config) либо файлом --injected; сборка его из toml приложения — дело CLI
    над модулем. Тело, объявившее параметр ToolIo, получает кадровую среду
    вызова: у запуска лончером она привязана к каналам, у человека отвязана.
    """

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
        if not arguments or arguments == [EntryFlag.HELP]:
            print(cls._tools_help(tools))  # noqa: T201
            return cls.Exit.OK

        name = arguments.pop(0)
        tool = cls._lookup(tools, name)

        if EntryFlag.HELP in arguments:
            print(cls._tool_help(tool))  # noqa: T201
            return cls.Exit.OK

        want_artifact = EntryFlag.ARTIFACT in arguments
        if want_artifact:
            arguments.remove(EntryFlag.ARTIFACT)

        injected_path = cls._pop_path(arguments, EntryFlag.INJECTED)

        config_read = Elapsed()
        io = cls._call_io(injected_path)
        config = cls._config_source(tool, io, injected_path)
        kwargs = ToolArgv.parse(tool, arguments, config)

        io_param = ToolArgv.io_field(ToolArgv.schema_of(tool))
        if io_param:
            kwargs[io_param] = io

        logger.info(
            "tool[%s]: args ready in %dms (config %d bytes)",
            tool.name,
            config_read.ms(),
            len(config),
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

    @staticmethod
    def _pop_path(arguments: list[str], flag: EntryFlag) -> str | None:
        if flag not in arguments:
            return None

        index = arguments.index(flag)
        if index + 1 >= len(arguments):
            msg = f"{flag} requires a path"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        arguments.pop(index)
        return arguments.pop(index)

    @staticmethod
    def _call_io(injected_path: str | None) -> ToolIo:
        """Среда вызова: каналы лончера либо отвязанная среда человека.

        Признак запуска лончером — канал кадров в env: без него читать кадры
        неоткуда, и вход остаётся пустым.
        """
        if injected_path is not None:
            return ToolIo.detached()

        raw = os.environ.get(ToolChannel.FRAMES.env_name)
        if raw is None:
            return ToolIo.detached()

        try:
            frames_fd = int(raw)
        except ValueError as exc:
            msg = f"{ToolChannel.FRAMES.env_name} is not a descriptor number: {raw!r}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        return ToolIo.on_channels(sys.stdin.fileno(), frames_fd)

    @classmethod
    def _config_source(
        cls, tool: ToolLike, io: ToolIo, injected_path: str | None
    ) -> bytes:
        """Injected-конфиг: файл --injected, кадр config либо stdin человека.

        Лончер всегда шлёт конфиг первым кадром; у человека кадров нет, и
        JSON приезжает обычным stdin либо файлом флага.
        """
        if injected_path is not None:
            return cls._config_from_file(injected_path)

        schema = ToolArgv.schema_of(tool)
        injected = ToolArgv.injected_fields(schema)
        io_param = ToolArgv.io_field(schema)
        if not injected and not io_param:
            return b"{}"

        if io.attached:
            return cls._config_frame(io)

        return cls._config_from_stdin()

    @staticmethod
    def _config_frame(io: ToolIo) -> bytes:
        try:
            return io.config()
        except FrameProtocolError as exc:
            msg = f"the call stream carries no config frame: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

    @staticmethod
    def _config_from_stdin() -> bytes:
        data = sys.stdin.buffer.read()
        if not data:
            msg = (
                "injected config expected on stdin "
                f"(or pass {EntryFlag.INJECTED} <path>)"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        return data

    @staticmethod
    def _config_from_file(path: str) -> bytes:
        """Файл с тем же JSON, что лончер шлёт кадром config."""
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            msg = f"injected config is not readable: {path}: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

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

        injected_help = "injected config as JSON, what the launcher sends on stdin"
        lines.append(f"  {EntryFlag.INJECTED} PATH  {injected_help}")
        return "\n".join(lines)
