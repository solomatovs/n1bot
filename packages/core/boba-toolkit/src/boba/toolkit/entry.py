"""Гостевая сторона вызова: CLI модуля инструментов и разбор его команды.

Каждый модуль инструментов — обычная программа: `python -m <модуль> <имя>
--флаги`. Одну и ту же команду исполняет launcher приложения и человек в
терминале; здесь живёт всё, что превращает команду в вызов тела: разбор
argv в kwargs (ToolArgv), каналы вызова из аргументов (CallWiring), сам
вход run (ToolMain). Хост передаёт каналы номерами дескрипторов в флагах
(--injected-fd, --fd-result, --fd-frames, --fd-in на каждый вход); человек
передаёт конфиг файлом --injected и читает результат из stdout. Входы
несут только прикладные кадры и при ручном запуске пусты.

Ошибки:
ArgumentTooLargeError — значение аргумента не помещается в argv (MAX_ARG_STRLEN).
ToolEntryError — нарушен контракт запуска: имени нет в TOOLS, флаги или конфиг
    не прошли валидацию, файл --injected не читается; kind из EntryErrorKind.
PayloadFailureError — исполненное тело подняло исключение; оно уже упаковано
    в результат-ошибку (FailurePacker).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from enum import IntEnum, StrEnum
from pathlib import Path
from types import NoneType, UnionType
from typing import (
    Any,
    ClassVar,
    Protocol,
    Self,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.toolkit.calls import FieldMarks
from boba.toolkit.closing import ProcessClosers
from boba.toolkit.facade import PayloadTool
from boba.toolkit.failure import FailurePacker, ValidationText
from boba.toolkit.frames import ToolIo
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.ports import (
    PortDecl,
    PortDeclarationError,
    PortDirection,
    StreamGroup,
    StreamPorts,
    StreamSpec,
)
from boba.toolkit.protocol import (
    CallInputSpec,
    CallOutputSpec,
    ReplyError,
    ReplyOk,
    ToolCommand,
)
from boba.toolkit.result import FailureResult, ToolResultBase
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import SecretReveal

__all__ = [
    "ArgumentTooLargeError",
    "CallWiring",
    "EntryErrorKind",
    "EntryFlag",
    "InputWire",
    "OutputWire",
    "PortWire",
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
        msg = (
            f"argument {param!r} encoded to {size} bytes, the argv value "
            f"limit is {limit} bytes; pass large values by a channel instead"
        )
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
    """Служебные флаги команды модуля инструментов — каналы вызова, конфиг,
    справка; с флагами параметров тела (их порождает схема инструмента) не
    пересекаются. На эти же имена ссылаются лончеры, дописывая флаги каналов
    в команду."""

    INJECTED = "--injected"
    INJECTED_FD = "--injected-fd"
    FD_RESULT = "--fd-result"
    FD_IN = "--fd-in"
    FD_OUT = "--fd-out"
    FD_GATE = "--fd-gate"
    FD_VERDICT = "--fd-verdict"
    ARTIFACT = "--artifact"
    HELP = "--help"


class PortWire(BaseModel):
    """Один канал вызова в argv: порт тела и номер дескриптора пайпа.

    Лончер дописывает `--fd-in <порт>=<fd>` на каждый вход по порядку
    ToolCommand.inputs и `--fd-out <порт>=<fd>` на каждый выход по порядку
    ToolCommand.outputs; у порта-списка флагов столько, сколько у него
    каналов. Запись и разбор значения флага живут здесь, у обеих сторон
    границы процесса; направление задаёт наследник своим FLAG.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str = Field(min_length=1)
    fd: int = Field(ge=0)

    SEPARATOR: ClassVar[str] = "="
    FLAG: ClassVar[EntryFlag]

    def argv(self) -> tuple[str, str]:
        """Флаг канала и его значение для команды тела."""
        return (self.FLAG.value, f"{self.port}{self.SEPARATOR}{self.fd}")

    @classmethod
    def of_value(cls, raw: str) -> Self:
        """Значение своего флага обратно в модель; битое — ToolEntryError."""
        port, separator, fd = raw.rpartition(cls.SEPARATOR)
        if not separator:
            msg = f"{cls.FLAG} expects <port>{cls.SEPARATOR}<fd>, got {raw!r}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        try:
            return cls(port=port, fd=int(fd))
        except ValueError as exc:
            msg = (
                f"{cls.FLAG} expects <port>{cls.SEPARATOR}<fd> with a "
                f"descriptor number, got {raw!r}: {exc}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc


class InputWire(PortWire):
    """Канал входа: флаг --fd-in."""

    FLAG: ClassVar[EntryFlag] = EntryFlag.FD_IN


class OutputWire(PortWire):
    """Канал выхода: флаг --fd-out."""

    FLAG: ClassVar[EntryFlag] = EntryFlag.FD_OUT


class CallWiring(BaseModel):
    """Каналы вызова, разобранные из argv: номера дескрипторов конфига,
    конверта, входов и выходов, которые лончер выдал телу.

    Сами дескрипторы достаются процессу наследованием, а номера едут
    флагами — команда самодостаточна, по argv видно все каналы вызова.
    -1 значит «канала нет»: так выглядит запуск человеком. strip() вынимает
    флаги из argv в начале ToolMain.run, дальше объект живёт весь вызов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    injected_fd: int = -1
    result_fd: int = -1
    gate_fd: int = -1
    verdict_fd: int = -1
    inputs: tuple[InputWire, ...] = ()
    outputs: tuple[OutputWire, ...] = ()

    def attached(self) -> bool:
        """Вызов пришёл от лончера: канал конверта в argv. Без него вызов
        запустил человек, и каналы данных отвязаны."""
        return self.result_fd >= 0

    FLAGS: ClassVar[Mapping[str, str]] = {
        EntryFlag.INJECTED_FD.value: "injected_fd",
        EntryFlag.FD_RESULT.value: "result_fd",
        EntryFlag.FD_GATE.value: "gate_fd",
        EntryFlag.FD_VERDICT.value: "verdict_fd",
    }

    @classmethod
    def strip(cls, arguments: list[str]) -> CallWiring:
        """Вынуть свои флаги из argv; значение не-число — нарушение контракта."""
        values: dict[str, int] = {}

        for flag, field in cls.FLAGS.items():
            raw = cls._pop_value(arguments, flag)
            if raw is None:
                continue

            try:
                values[field] = int(raw)
            except ValueError as exc:
                msg = f"{flag} expects a descriptor number, got {raw!r}: {exc}"
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        inputs: list[InputWire] = []
        while raw_input := cls._pop_value(arguments, EntryFlag.FD_IN.value):
            inputs.append(InputWire.of_value(raw_input))

        outputs: list[OutputWire] = []
        while raw_output := cls._pop_value(arguments, EntryFlag.FD_OUT.value):
            outputs.append(OutputWire.of_value(raw_output))

        return cls(**values, inputs=tuple(inputs), outputs=tuple(outputs))

    @staticmethod
    def _pop_value(arguments: list[str], flag: str) -> str | None:
        if flag not in arguments:
            return None

        index = arguments.index(flag)
        if index + 1 >= len(arguments):
            msg = f"{flag} expects a descriptor number after it, got the end of argv"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        arguments.pop(index)
        return arguments.pop(index)


class ToolLike(Protocol):
    """Протокол tool-объекта (имя, схема, тело) — то, что toolkit'у нужно от
    langchain-инструмента без зависимости от langchain.

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
    """Адрес инструмента для командной строки: модуль тела и имя в TOOLS —
    из них собирается префикс команды `python -m <модуль> <имя>`.

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
            msg = (
                f"tool {tool.name!r} has neither coroutine nor func: "
                "its module path for the command line cannot be resolved"
            )
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        return cls(module=body.__module__, name=tool.name)

    PYTHON: ClassVar[str] = "python3"
    """Интерпретатор по PATH окружения запуска: python песочницы — не python
    приложения, абсолютный sys.executable внутрь не переносится."""

    def argv_head(self) -> list[str]:
        return [self.PYTHON, "-m", self.module, self.name]


class ToolArgv:
    """Переводит kwargs вызова в argv команды и обратно по args_schema
    инструмента — одна логика у обёртки запуска (хост) и CLI (гость).

    Правило одно: параметр, видимый LLM, — флаг argv; параметр с injected-
    метадатой — ключ в JSON конфига, который едет телу каналом --injected-fd.
    Текстовые значения едут как есть, остальные — JSON'ом в значении флага.

    Особый случай — порты Inbound/Outbound (boba.toolkit.ports): это каналы
    вызова, а не значения. Хост их не сериализует, гость строит и
    подставляет свои объекты.
    """

    MAX_VALUE_BYTES: ClassVar[int] = 131_071
    """MAX_ARG_STRLEN минус завершающий нуль; ровно 131072 даёт E2BIG."""

    @classmethod
    def render(
        cls,
        address: ToolAddress,
        schema: type[BaseModel],
        kwargs: Mapping[str, object],
        input_counts: Mapping[str, int],
        output_counts: Mapping[str, int],
    ) -> ToolCommand:
        """LLM-аргументы во флаги, injected-параметры в конфиг вызова.

        input_counts и output_counts — сколько каналов у портов вызова по
        имени порта. Одиночный порт без записи получает ровно один канал,
        порт-список без записи — ни одного.
        """
        argv = address.argv_head()

        config_payload: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if cls.is_io(field.annotation):
                continue

            if name not in kwargs:
                continue

            value = kwargs[name]
            if FieldMarks.injected(field):
                config_payload[name] = cls.reveal(field.annotation, value)
                continue

            if FieldMarks.connection(field):
                config_payload[name] = cls.reveal(field.annotation, value)
                continue

            if value is None:
                continue

            encoded = cls._encode(name, field.annotation, value)
            argv.append(cls.flag_of(name))
            argv.append(encoded)

        config = json.dumps(config_payload, ensure_ascii=False).encode("utf-8")
        spec = StreamSpec.of_schema(schema)

        inputs: list[CallInputSpec] = []
        for port, raw_port in cls._channels(spec, spec.inbound(), input_counts):
            inputs.append(CallInputSpec(port=port, raw=raw_port))

        outputs: list[CallOutputSpec] = []
        for port, raw_port in cls._channels(spec, spec.outbound(), output_counts):
            outputs.append(CallOutputSpec(port=port, raw=raw_port))

        return ToolCommand(
            argv=tuple(argv),
            config=config,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
        )

    @classmethod
    def _channels(
        cls,
        spec: StreamSpec,
        ports: Sequence[PortDecl],
        counts: Mapping[str, int],
    ) -> Iterator[tuple[str, bool]]:
        """Каналы вызова по порядку портов одной стороны подписи."""
        declared: set[str] = set()
        for port in ports:
            declared.add(port.name)

        for name, count in counts.items():
            cls._check_count(spec.port(name), declared, count)

        for port in ports:
            count = 1
            if port.many:
                count = 0

            if port.name in counts:
                count = counts[port.name]

            for _ in range(count):
                yield port.name, port.raw

    @staticmethod
    def _check_count(port: PortDecl, declared: set[str], count: int) -> None:
        if port.name not in declared:
            msg = (
                f"port {port.name!r} is {port.direction}, its channel count "
                "belongs to the other side of the call"
            )
            raise PortDeclarationError(msg)

        if port.many:
            return

        if count > 1:
            msg = (
                f"port {port.name!r} is a single port and takes at most "
                f"one channel, got {count}"
            )
            raise PortDeclarationError(msg)

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
                known = ", ".join(sorted(by_flag))
                msg = (
                    f"unknown flag {flag!r} for tool {tool.name!r}; "
                    f"known flags: {known}"
                )
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            if not pending:
                msg = (
                    f"flag {flag!r} of tool {tool.name!r} expects a value, "
                    "got the end of argv"
                )
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            raw = pending.pop(0)
            field = schema.model_fields[name]
            kwargs[name] = cls._decode(name, field.annotation, raw)

        kwargs.update(cls._parse_config(schema, config))
        return kwargs

    @classmethod
    def schema_of(cls, tool: ToolLike) -> type[BaseModel]:
        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            msg = (
                f"tool {tool.name!r} args_schema must be a pydantic model class, "
                f"got {schema!r}"
            )
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        return schema

    @classmethod
    def injected_fields(cls, schema: type[BaseModel]) -> dict[str, Any]:
        """Injected-параметры схемы: имя -> аннотация. Порты не в счёт."""
        fields: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if cls.is_io(field.annotation):
                continue

            if FieldMarks.injected(field):
                fields[name] = field.annotation

        return fields

    @classmethod
    def connection_fields(cls, schema: type[BaseModel]) -> dict[str, Any]:
        """Параметры-соединения схемы: имя -> аннотация профиля."""
        fields: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if cls.is_io(field.annotation):
                continue

            if FieldMarks.connection(field):
                fields[name] = field.annotation

        return fields

    @classmethod
    def port_fields(cls, schema: type[BaseModel]) -> dict[str, Any]:
        """Порты схемы: имя параметра -> аннотация Inbound/Outbound."""
        fields: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            if StreamPorts.is_port(field.annotation):
                fields[name] = field.annotation

        return fields

    @staticmethod
    def is_io(annotation: Any) -> bool:
        """Параметр — порт или барьер группы: значение строит гость, а не хост."""
        if StreamPorts.is_group(annotation):
            return True

        return StreamPorts.is_port(annotation)

    @classmethod
    def group_fields(cls, schema: type[BaseModel]) -> tuple[str, ...]:
        """Параметры барьера группы StreamGroup."""
        names: list[str] = []
        for name, field in schema.model_fields.items():
            if StreamPorts.is_group(field.annotation):
                names.append(name)

        return tuple(names)

    @staticmethod
    def flag_of(param: str) -> str:
        return "--" + param.replace("_", "-")

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
            detail = ValidationText.of(exc)
            msg = (
                f"argument {name!r} does not match its declared type "
                f"{annotation!r}: {detail}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from None
        except ValueError as exc:
            msg = f"argument {name!r} could not be decoded as {annotation!r}: {exc}"
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
            msg = (
                f"injected parameter {name!r}: model {annotation!r} must define "
                f"SECTION as the toml section name, got {section!r}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        if not isinstance(annotation, type):
            msg = (
                f"injected parameter {name!r} must be annotated with a pydantic "
                f"model class, got {annotation!r}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        if not issubclass(annotation, BaseModel):
            msg = (
                f"injected parameter {name!r} must subclass pydantic BaseModel, "
                f"got {annotation.__name__}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        return section

    @staticmethod
    def reveal(annotation: Any, value: object) -> Any:
        """JSON-совместимый дамп значения канала конфига с раскрытыми секретами.

        Канал доверенный: по нему едут секции конфига и профили соединений,
        и секреты в них нужны телу живыми.
        """
        revealed = getattr(value, "revealed", None)
        if callable(revealed):
            return revealed()

        if isinstance(value, BaseModel):
            return SecretReveal.dumped(value)

        return TypeAdapter(annotation).dump_python(value, mode="json")

    @classmethod
    def _parse_config(cls, schema: type[BaseModel], config: bytes) -> dict[str, Any]:
        """Ключи канала конфига в kwargs: injected-модели и профили соединений."""
        expected = dict(cls.injected_fields(schema))
        expected.update(cls.connection_fields(schema))
        if not expected:
            return {}

        payload: object = {}
        if config:
            try:
                payload = json.loads(config.decode("utf-8"))
            except ValueError as exc:
                msg = f"call config ({len(config)} bytes) is not valid JSON: {exc}"
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        if not isinstance(payload, dict):
            msg = (
                "call config must be a JSON object keyed by parameter names, "
                f"got {type(payload).__name__}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        kwargs: dict[str, Any] = {}
        for name, annotation in expected.items():
            if name not in payload:
                present = ", ".join(sorted(payload))
                msg = (
                    f"parameter {name!r} is missing from the call config; "
                    f"config keys: [{present}]"
                )
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            try:
                kwargs[name] = TypeAdapter(annotation).validate_python(payload[name])
            except ValidationError as exc:
                msg = (
                    f"call config for parameter {name!r} does not match "
                    f"{annotation!r}: {exc}"
                )
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

        return kwargs


class ToolMain:
    """CLI модуля инструментов: argv -> тело -> конверт либо вывод человеку.

    Конверт пишется в дескриптор из --fd-result, когда он передан, — так
    зовёт launcher; без него content печатается в stdout — так зовёт
    человек. Injected-конфиг приезжает каналом --injected-fd (лончер) либо
    файлом --injected (человек); сборка его из toml приложения — дело CLI
    над модулем. Тело, объявившее порты Inbound/Outbound (boba.toolkit.ports),
    получает их готовыми: у запуска лончером они привязаны к каналам вызова,
    у человека отвязаны.
    """

    class Exit(IntEnum):
        OK = 0
        FAILURE = 1
        ENTRY_ERROR = 2

    _FAILURES: ClassVar[FailurePacker] = FailurePacker()

    REQUIRED_ATTRIBUTES: ClassVar[tuple[str, ...]] = (
        "name",
        "args_schema",
        "func",
        "coroutine",
    )

    @classmethod
    def toolset(cls, *tools: object) -> tuple[ToolLike, ...]:
        """Кортеж TOOLS из tool-объектов с проверкой duck-полей.

        Декоратор @tool статически отдаёт BaseTool без func/coroutine —
        мост к ToolLike делается здесь, один раз на модуль.
        """
        checked: list[ToolLike] = []
        for tool in tools:
            for attribute in cls.REQUIRED_ATTRIBUTES:
                if not hasattr(tool, attribute):
                    msg = (
                        f"{tool!r} is not a tool object: attribute {attribute!r} "
                        f"is missing, a tool needs {cls.REQUIRED_ATTRIBUTES}"
                    )
                    raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

            accepted: Any = tool
            checked.append(accepted)

        return tuple(checked)

    LOG_FORMAT: ClassVar[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    @classmethod
    def run(cls, tools: Sequence[ToolLike], argv: Sequence[str] | None = None) -> int:
        arguments = list(sys.argv[1:]) if argv is None else list(argv)

        cls._setup_logging()

        try:
            wiring = CallWiring.strip(arguments)
        except ToolEntryError as exc:
            # каналы не разобраны: конверт писать некуда, причина — в stderr
            print(f"{exc.kind}: {exc}", file=sys.stderr)  # noqa: T201
            return cls.Exit.ENTRY_ERROR

        try:
            return cls._run(tools, arguments, wiring)
        except ToolEntryError as exc:
            cls._emit_error(wiring, cls._FAILURES.pack(exc))
            return cls.Exit.ENTRY_ERROR
        except PayloadFailureError as exc:
            cls._emit_error(wiring, exc.failure())
            return cls.Exit.FAILURE
        except Exception as exc:
            failure = cls._FAILURES.pack(exc)
            logger.error("tool call failed: %s", failure.log_view())
            cls._emit_error(wiring, failure)
            return cls.Exit.FAILURE

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
    def _run(
        cls, tools: Sequence[ToolLike], arguments: list[str], wiring: CallWiring
    ) -> int:
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
        config = cls._config_source(tool, wiring, injected_path)
        kwargs = ToolArgv.parse(tool, arguments, config)
        kwargs.update(cls._build_ports(tool, wiring))
        kwargs.update(cls._build_groups(tool, wiring))

        logger.info(
            "tool[%s]: args ready in %dms (config %d bytes)",
            tool.name,
            config_read.ms(),
            len(config),
        )

        reply = cls._call(tool, kwargs)
        return cls._deliver(reply, wiring, want_artifact)

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
            msg = f"{flag} expects a file path after it, got the end of argv"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        arguments.pop(index)
        return arguments.pop(index)

    @classmethod
    def _build_ports(cls, tool: ToolLike, wiring: CallWiring) -> dict[str, Any]:
        """Порты вызова для объявивших их параметров подписи.

        Валидирует декларацию (StreamSpec) и строит порты поверх транспорта
        ToolIo: у запуска лончером каждый вход сидит на своём пайпе из
        --fd-in, выход — на канале кадров; у человека среда отвязана — входы
        пусты, кадры наружу уходят в лог.
        """
        schema = ToolArgv.schema_of(tool)

        fields = ToolArgv.port_fields(schema)
        if not fields:
            cls._refuse_inputs(tool, wiring)
            return {}

        try:
            spec = StreamSpec.of_schema(schema)
        except (PortDeclarationError, ValidationError) as exc:
            listed = ", ".join(sorted(fields))
            msg = (
                f"tool {tool.name!r} declares broken ports on parameters "
                f"[{listed}]: {exc}"
            )
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg) from exc

        wires = cls._wires_by_port(tool, spec.inbound(), wiring.inputs)
        wires.update(cls._wires_by_port(tool, spec.outbound(), wiring.outputs))

        ports: dict[str, Any] = {}
        for decl in spec.ports:
            element = StreamPorts.element_of(fields[decl.name])
            ports[decl.name] = cls._port_value(tool, decl, element, wires)

        return ports

    @staticmethod
    def _build_groups(tool: ToolLike, wiring: CallWiring) -> dict[str, StreamGroup]:
        """Барьер группы на пайпах --fd-gate/--fd-verdict; у человека —
        отвязанный, ready() возвращается сразу."""
        names = ToolArgv.group_fields(ToolArgv.schema_of(tool))

        group = StreamGroup(-1, -1)
        if wiring.attached():
            group = StreamGroup(wiring.gate_fd, wiring.verdict_fd)

        groups: dict[str, StreamGroup] = {}
        for name in names:
            groups[name] = group

        return groups

    @staticmethod
    def _refuse_inputs(tool: ToolLike, wiring: CallWiring) -> None:
        """Инструмент без портов: канал из argv некому отдать."""
        if wiring.inputs:
            msg = (
                f"tool {tool.name!r} declares no inbound ports, got "
                f"{len(wiring.inputs)} {EntryFlag.FD_IN} inputs"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        if wiring.outputs:
            msg = (
                f"tool {tool.name!r} declares no outbound ports, got "
                f"{len(wiring.outputs)} {EntryFlag.FD_OUT} outputs"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

    @classmethod
    def _port_value(
        cls,
        tool: ToolLike,
        decl: PortDecl,
        element: Any,
        wires: Mapping[str, Sequence[PortWire]],
    ) -> object:
        """Значение параметра-порта: порт либо кортеж портов по его каналам."""
        own = wires[decl.name]

        if decl.many:
            return tuple(cls._ports_of(decl, element, own))

        if len(own) > 1:
            msg = (
                f"tool {tool.name!r}: port {decl.name!r} expects at most "
                f"one channel, got {len(own)}"
            )
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

        # без канала порт отвязан: так выглядит и запуск человеком
        for wire in own:
            return StreamPorts.build(element, cls._io_of(decl, wire))

        return StreamPorts.build(element, ToolIo.detached())

    @classmethod
    def _ports_of(
        cls, decl: PortDecl, element: Any, wires: Sequence[PortWire]
    ) -> Iterator[Any]:
        for wire in wires:
            yield StreamPorts.build(element, cls._io_of(decl, wire))

    @staticmethod
    def _io_of(decl: PortDecl, wire: PortWire) -> ToolIo:
        """Транспорт канала: пайп входа читается, пайп выхода пишется."""
        if decl.direction is PortDirection.INBOUND:
            return ToolIo.on_channels(wire.fd, -1)

        return ToolIo.on_channels(-1, wire.fd)

    @staticmethod
    def _wires_by_port(
        tool: ToolLike, ports: Sequence[PortDecl], wires: Sequence[PortWire]
    ) -> dict[str, list[PortWire]]:
        """Каналы из argv по портам одной стороны; канал чужого порта — отказ."""
        owned: dict[str, list[PortWire]] = {}
        for port in ports:
            owned[port.name] = []

        for wire in wires:
            own = owned.get(wire.port)
            if own is None:
                known = sorted(owned)
                msg = (
                    f"tool {tool.name!r}: {wire.FLAG} names port "
                    f"{wire.port!r}, the declared ports are {known}"
                )
                raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

            own.append(wire)

        return owned

    @classmethod
    def _config_source(
        cls, tool: ToolLike, wiring: CallWiring, injected_path: str | None
    ) -> bytes:
        """Injected-конфиг: канал --injected-fd лончера либо файл --injected.

        Источник однозначен по режиму запуска; stdin конфиг не несёт никогда
        — он принадлежит прикладным кадрам входа.
        """
        if injected_path is not None:
            return cls._config_from_file(injected_path)

        if wiring.injected_fd >= 0:
            return cls._config_from_fd(wiring.injected_fd)

        schema = ToolArgv.schema_of(tool)
        if not ToolArgv.injected_fields(schema):
            if not ToolArgv.connection_fields(schema):
                return b"{}"

        msg = (
            f"tool {tool.name!r} needs an injected config and none was given: "
            f"the launcher passes {EntryFlag.INJECTED_FD} <fd>, a human passes "
            f"{EntryFlag.INJECTED} <path>"
        )
        raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg)

    READ_BYTES: ClassVar[int] = 65536

    @classmethod
    def _config_from_fd(cls, fd: int) -> bytes:
        """Канал конфига от лончера: читается до EOF и закрывается."""
        chunks: list[bytes] = []

        try:
            while True:
                chunk = os.read(fd, cls.READ_BYTES)
                if not chunk:
                    break

                chunks.append(chunk)
        except OSError as exc:
            msg = f"reading injected config from fd {fd} failed: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc
        finally:
            with suppress(OSError):
                os.close(fd)

        return b"".join(chunks)

    @staticmethod
    def _config_from_file(path: str) -> bytes:
        """Файл с тем же JSON, что лончер шлёт каналом конфига."""
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            msg = f"reading injected config file {path!r} failed: {exc}"
            raise ToolEntryError(EntryErrorKind.INVALID_REQUEST, msg) from exc

    @classmethod
    def _call(cls, tool: ToolLike, kwargs: dict[str, Any]) -> ReplyOk:
        body = tool.coroutine or tool.func
        if body is None:
            msg = (
                f"tool {tool.name!r} has no body to call: "
                "both coroutine and func are None"
            )
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        if isinstance(tool, PayloadTool):
            kwargs = tool.packed_kwargs(kwargs)

        elapsed = Elapsed()
        try:
            if tool.coroutine is not None:
                result = asyncio.run(cls._acall(tool.coroutine, kwargs))
            else:
                result = body(**kwargs)
        except Exception as exc:
            failure = cls._FAILURES.pack(exc)
            logger.error(
                "tool[%s]: body failed in %dms: %s",
                tool.name,
                elapsed.ms(),
                failure.log_view(),
            )
            raise PayloadFailureError(failure) from exc

        logger.info("tool[%s]: body finished in %dms", tool.name, elapsed.ms())

        return cls._pack(tool, result)

    @staticmethod
    async def _acall(
        coroutine: Callable[..., Awaitable[Any]], kwargs: dict[str, Any]
    ) -> Any:
        try:
            return await coroutine(**kwargs)
        finally:
            await ProcessClosers().close_all()

    @classmethod
    def _pack(cls, tool: ToolLike, result: object) -> ReplyOk:
        if not isinstance(result, ToolResultBase):
            msg = (
                f"tool {tool.name!r} must return a ToolResultBase model, "
                f"got {type(result).__name__}"
            )
            raise ToolEntryError(EntryErrorKind.INTERNAL_ERROR, msg)

        return ReplyOk(content=result.llm_view(), artifact=result)

    @classmethod
    def _deliver(cls, reply: ReplyOk, wiring: CallWiring, want_artifact: bool) -> int:
        if wiring.result_fd >= 0:
            cls._write_envelope(wiring.result_fd, reply)
            return cls.Exit.OK

        print(reply.content)  # noqa: T201
        if want_artifact:
            print(reply.artifact.model_dump_json())  # noqa: T201

        return cls.Exit.OK

    @classmethod
    def _emit_error(cls, wiring: CallWiring, failure: FailureResult) -> None:
        if wiring.result_fd >= 0:
            cls._write_envelope(wiring.result_fd, ReplyError(failure=failure))
            return

        print(failure.log_view(), file=sys.stderr)  # noqa: T201

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

        hidden = dict(ToolArgv.injected_fields(schema))
        hidden.update(ToolArgv.connection_fields(schema))

        for name, field in schema.model_fields.items():
            if name in hidden:
                continue

            description = field.description or ""
            lines.append(f"  {ToolArgv.flag_of(name)} {description}".rstrip())

        injected_help = "call config as JSON: injected sections and connections"
        lines.append(f"  {EntryFlag.INJECTED} PATH  {injected_help}")
        return "\n".join(lines)
