"""Гостевая сторона вызова: вход тела инструмента и разбор его аргументов.

Вызов инструмента — подкоманда `call <имя>` программы плагина: её
исполняет лончер приложения и человек в терминале. Здесь живёт всё, что
превращает вызов в исполнение тела: аргументы одним JSON-объектом
(ToolArgv), каналы вызова из флагов (CallWiring), сам вход run (ToolMain).
Хост передаёт аргументы каналом --args-fd, а каналы — номерами
дескрипторов во флагах (--fd-result, --fd-in на каждый вход и т. д.);
человек передаёт аргументы файлом --args и читает результат из stdout.
Входы несут только прикладные кадры и при ручном запуске пусты.

Ошибки:
ToolEntryError — нарушен контракт запуска: имени нет среди инструментов,
    флаги или аргументы не прошли валидацию, файл --args не читается; kind
    из InvokeErrorKind.
PayloadFailureError — исполненное тело подняло исключение; оно уже упаковано
    в результат-ошибку (FailurePacker).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic_core import to_jsonable_python

from boba.core.secrets import SecretReveal
from boba.core.validation import ValidationText
from boba.toolkit.closing import ProcessClosers
from boba.toolkit.facade import PayloadTool, ToolFacadeError
from boba.toolkit.failure import FailurePacker, InvokeErrorKind
from boba.toolkit.frames import ToolIo
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.manifest import InjectedRole, ToolDecl
from boba.toolkit.ports import (
    PortDecl,
    PortDeclarationError,
    PortDirection,
    StreamGroup,
    StreamPorts,
    StreamSpec,
    ToolProgress,
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

__all__ = [
    "CallWiring",
    "EntryFlag",
    "InputWire",
    "OutputWire",
    "PortWire",
    "ToolArgv",
    "ToolEntryError",
    "ToolMain",
]


logger = logging.getLogger(__name__)


class ToolEntryError(Exception):
    """Нарушение контракта запуска с классификацией для конверта."""

    def __init__(self, kind: InvokeErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class EntryFlag(StrEnum):
    """Флаги подкоманды call — аргументы, каналы вызова, справка. На эти же
    имена ссылаются лончеры, дописывая флаги каналов в команду."""

    ARGS = "--args"
    ARGS_FD = "--args-fd"
    FD_RESULT = "--fd-result"
    FD_IN = "--fd-in"
    FD_OUT = "--fd-out"
    FD_GATE = "--fd-gate"
    FD_VERDICT = "--fd-verdict"
    FD_PROGRESS = "--fd-progress"
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
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

        try:
            return cls(port=port, fd=int(fd))
        except ValueError as exc:
            msg = (
                f"{cls.FLAG} expects <port>{cls.SEPARATOR}<fd> with a "
                f"descriptor number, got {raw!r}: {exc}"
            )
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg) from exc


class InputWire(PortWire):
    """Канал входа: флаг --fd-in."""

    FLAG: ClassVar[EntryFlag] = EntryFlag.FD_IN


class OutputWire(PortWire):
    """Канал выхода: флаг --fd-out."""

    FLAG: ClassVar[EntryFlag] = EntryFlag.FD_OUT


class CallWiring(BaseModel):
    """Каналы вызова, разобранные из argv: номера дескрипторов аргументов,
    конверта, входов и выходов, которые лончер выдал телу.

    Сами дескрипторы достаются процессу наследованием, а номера едут
    флагами — команда самодостаточна, по argv видно все каналы вызова.
    -1 значит «канала нет»: так выглядит запуск человеком. strip() вынимает
    флаги из argv в начале ToolMain.run, дальше объект живёт весь вызов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    args_fd: int = -1
    result_fd: int = -1
    gate_fd: int = -1
    verdict_fd: int = -1
    progress_fd: int = -1
    inputs: tuple[InputWire, ...] = ()
    outputs: tuple[OutputWire, ...] = ()

    def attached(self) -> bool:
        """Вызов пришёл от лончера: канал конверта в argv. Без него вызов
        запустил человек, и каналы данных отвязаны."""
        return self.result_fd >= 0

    FLAGS: ClassVar[Mapping[str, str]] = {
        EntryFlag.ARGS_FD.value: "args_fd",
        EntryFlag.FD_RESULT.value: "result_fd",
        EntryFlag.FD_GATE.value: "gate_fd",
        EntryFlag.FD_VERDICT.value: "verdict_fd",
        EntryFlag.FD_PROGRESS.value: "progress_fd",
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
                raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg) from exc

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
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

        arguments.pop(index)
        return arguments.pop(index)


class ToolArgv:
    """Аргументы вызова через границу процесса: один JSON-объект, ключи —
    имена параметров тела.

    Хост собирает объект по объявлению инструмента из манифеста (render):
    аргументы модели как прислала модель, значения служебных параметров —
    секция конфига, контекст вызова, профили соединений. Гость разбирает
    объект по модели вызова тела (parse) и проверяет каждое значение её
    полем: аргументы модели хост не проверяет. Порты Inbound/Outbound,
    барьер группы и отчёты о ходе работы — каналы вызова, а не значения:
    гость строит их сам.
    """

    @classmethod
    def render(
        cls,
        decl: ToolDecl,
        kwargs: Mapping[str, object],
        input_counts: Mapping[str, int],
        output_counts: Mapping[str, int],
    ) -> ToolCommand:
        """Аргументы вызова и каналы портов по объявлению decl.

        input_counts и output_counts — сколько каналов у портов вызова по
        имени порта. Одиночный порт без записи получает ровно один канал,
        порт-список без записи — ни одного.
        """
        payload: dict[str, Any] = {}
        for name in decl.properties():
            if name not in kwargs:
                continue

            payload[name] = to_jsonable_python(kwargs[name])

        for name, role in decl.injected.items():
            if role is InjectedRole.PROGRESS:
                continue

            if name not in kwargs:
                continue

            payload[name] = cls.reveal(kwargs[name])

        for name in decl.connections:
            if name not in kwargs:
                continue

            payload[name] = cls.reveal(kwargs[name])

        args = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        spec = decl.stream

        inputs: list[CallInputSpec] = []
        for port, raw_port in cls._channels(spec, spec.inbound(), input_counts):
            inputs.append(CallInputSpec(port=port, raw=raw_port))

        outputs: list[CallOutputSpec] = []
        for port, raw_port in cls._channels(spec, spec.outbound(), output_counts):
            outputs.append(CallOutputSpec(port=port, raw=raw_port))

        return ToolCommand(
            argv=(decl.name,),
            args=args,
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
        tool: PayloadTool,
        argv: Sequence[str],
        args: bytes,
    ) -> dict[str, Any]:
        """Обратный разбор: JSON аргументов вызова в kwargs тела, каждое
        значение проверено полем модели вызова; отсутствующее необязательное
        поле получает свой дефолт, лишний ключ — отказ."""
        if argv:
            msg = (
                f"tool {tool.name!r} takes its arguments as one JSON object by "
                f"{EntryFlag.ARGS_FD} or {EntryFlag.ARGS}, got extra argv "
                f"{list(argv)[:3]}"
            )
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

        payload = cls._payload(args)
        model = cls._args_model(tool)

        try:
            call = model.model_validate(payload)
        except ValidationError as exc:
            # значения не пересказываем: в аргументах едут пароли и токены
            msg = (
                f"tool {tool.name!r}: arguments do not match its declaration: "
                f"{ValidationText.of(exc)}"
            )
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg) from None

        kwargs: dict[str, Any] = {}
        for name in model.model_fields:
            kwargs[name] = getattr(call, name)

        return kwargs

    @classmethod
    def _args_model(cls, tool: PayloadTool) -> type[BaseModel]:
        """Модель аргументов вызова: поля тела без каналов, лишний ключ — отказ."""
        fields: dict[str, Any] = {}
        for name, field in cls.schema_of(tool).model_fields.items():
            if cls.is_io(field.annotation):
                continue

            fields[name] = (field.annotation, field)

        return create_model(
            f"{tool.name}_args",
            __config__=ConfigDict(extra="forbid", arbitrary_types_allowed=True),
            **fields,
        )

    @staticmethod
    def _payload(args: bytes) -> Mapping[str, object]:
        """JSON-объект аргументов; пустой канал — вызов без аргументов."""
        if not args:
            return {}

        try:
            payload = json.loads(args.decode("utf-8"))
        except ValueError as exc:
            msg = f"call arguments ({len(args)} bytes) are not valid JSON: {exc}"
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg) from exc

        if not isinstance(payload, dict):
            msg = (
                "call arguments must be a JSON object keyed by parameter names, "
                f"got {type(payload).__name__}"
            )
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

        return payload

    @classmethod
    def schema_of(cls, tool: PayloadTool) -> type[BaseModel]:
        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            msg = (
                f"tool {tool.name!r} args_schema must be a pydantic model class, "
                f"got {schema!r}"
            )
            raise ToolEntryError(InvokeErrorKind.INTERNAL_ERROR, msg)

        return schema

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
        """Параметр — порт, барьер группы или отчёты о ходе работы:
        значение строит гость, а не хост."""
        if StreamPorts.is_group(annotation):
            return True

        if StreamPorts.is_progress(annotation):
            return True

        return StreamPorts.is_port(annotation)

    @classmethod
    def progress_fields(cls, schema: type[BaseModel]) -> tuple[str, ...]:
        """Параметры отчётов о ходе работы ToolProgress."""
        names: list[str] = []
        for name, field in schema.model_fields.items():
            if StreamPorts.is_progress(field.annotation):
                names.append(name)

        return tuple(names)

    @classmethod
    def group_fields(cls, schema: type[BaseModel]) -> tuple[str, ...]:
        """Параметры барьера группы StreamGroup."""
        names: list[str] = []
        for name, field in schema.model_fields.items():
            if StreamPorts.is_group(field.annotation):
                names.append(name)

        return tuple(names)

    @staticmethod
    def reveal(value: object) -> Any:
        """JSON-совместимый дамп служебного значения с раскрытыми секретами.

        Канал доверенный: по нему едут секции конфига и профили соединений,
        и секреты в них нужны телу живыми.
        """
        revealed = getattr(value, "revealed", None)
        if callable(revealed):
            return revealed()

        if isinstance(value, BaseModel):
            return SecretReveal.dumped(value)

        return to_jsonable_python(value)


class ToolMain:
    """CLI модуля инструментов: argv -> тело -> конверт либо вывод человеку.

    Конверт пишется в дескриптор из --fd-result, когда он передан, — так
    зовёт launcher; без него content печатается в stdout — так зовёт
    человек. Аргументы приезжают JSON-объектом каналом --args-fd (лончер)
    либо файлом --args (человек); сборка служебных значений — дело хоста
    над модулем. Тело, объявившее порты Inbound/Outbound (boba.toolkit.ports),
    получает их готовыми: у запуска лончером они привязаны к каналам вызова,
    у человека отвязаны.
    """

    class Exit(IntEnum):
        OK = 0
        FAILURE = 1
        ENTRY_ERROR = 2

    _FAILURES: ClassVar[FailurePacker] = FailurePacker()

    @classmethod
    def toolset(cls, *tools: PayloadTool) -> tuple[PayloadTool, ...]:
        """Кортеж TOOLS модуля из инструментов фасада @tool."""
        for tool in tools:
            if not isinstance(tool, PayloadTool):
                msg = (
                    f"{tool!r} is not a tool object: expected a PayloadTool "
                    f"built by the @tool facade, got {type(tool).__name__}"
                )
                raise ToolEntryError(InvokeErrorKind.INTERNAL_ERROR, msg)

        return tuple(tools)

    LOG_FORMAT: ClassVar[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"

    @classmethod
    def run(
        cls, tools: Sequence[PayloadTool], argv: Sequence[str] | None = None
    ) -> int:
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
        cls, tools: Sequence[PayloadTool], arguments: list[str], wiring: CallWiring
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

        args_path = cls._pop_path(arguments, EntryFlag.ARGS)

        args_read = Elapsed()
        args = cls._args_source(wiring, args_path)
        kwargs = ToolArgv.parse(tool, arguments, args)
        kwargs.update(cls._build_ports(tool, wiring))
        kwargs.update(cls._build_groups(tool, wiring))
        progress = ToolProgress(wiring.progress_fd)
        for name in ToolArgv.progress_fields(ToolArgv.schema_of(tool)):
            kwargs[name] = progress

        logger.info(
            "tool[%s]: args ready in %dms (%d bytes)",
            tool.name,
            args_read.ms(),
            len(args),
        )

        try:
            reply = cls._call(tool, kwargs)
        finally:
            progress.close()

        return cls._deliver(reply, wiring, want_artifact)

    @classmethod
    def _lookup(cls, tools: Sequence[PayloadTool], name: str) -> PayloadTool:
        for tool in tools:
            if tool.name == name:
                return tool

        known = ", ".join(sorted(tool.name for tool in tools))
        msg = f"unknown tool {name!r}; known tools: {known}"
        raise ToolEntryError(InvokeErrorKind.UNKNOWN_TOOL, msg)

    @staticmethod
    def _pop_path(arguments: list[str], flag: EntryFlag) -> str | None:
        if flag not in arguments:
            return None

        index = arguments.index(flag)
        if index + 1 >= len(arguments):
            msg = f"{flag} expects a file path after it, got the end of argv"
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

        arguments.pop(index)
        return arguments.pop(index)

    @classmethod
    def _build_ports(cls, tool: PayloadTool, wiring: CallWiring) -> dict[str, Any]:
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
            raise ToolEntryError(InvokeErrorKind.INTERNAL_ERROR, msg) from exc

        wires = cls._wires_by_port(tool, spec.inbound(), wiring.inputs)
        wires.update(cls._wires_by_port(tool, spec.outbound(), wiring.outputs))

        ports: dict[str, Any] = {}
        for decl in spec.ports:
            element = StreamPorts.element_of(fields[decl.name])
            ports[decl.name] = cls._port_value(tool, decl, element, wires)

        return ports

    @staticmethod
    def _build_groups(tool: PayloadTool, wiring: CallWiring) -> dict[str, StreamGroup]:
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
    def _refuse_inputs(tool: PayloadTool, wiring: CallWiring) -> None:
        """Инструмент без портов: канал из argv некому отдать."""
        if wiring.inputs:
            msg = (
                f"tool {tool.name!r} declares no inbound ports, got "
                f"{len(wiring.inputs)} {EntryFlag.FD_IN} inputs"
            )
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

        if wiring.outputs:
            msg = (
                f"tool {tool.name!r} declares no outbound ports, got "
                f"{len(wiring.outputs)} {EntryFlag.FD_OUT} outputs"
            )
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

    @classmethod
    def _port_value(
        cls,
        tool: PayloadTool,
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
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

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
        tool: PayloadTool, ports: Sequence[PortDecl], wires: Sequence[PortWire]
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
                raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg)

            own.append(wire)

        return owned

    @classmethod
    def _args_source(cls, wiring: CallWiring, args_path: str | None) -> bytes:
        """Аргументы вызова: канал --args-fd лончера либо файл --args; без
        обоих — вызов без аргументов. stdin аргументов не несёт никогда — он
        принадлежит прикладным кадрам входа."""
        if args_path is not None:
            return cls._args_from_file(args_path)

        if wiring.args_fd >= 0:
            return cls._args_from_fd(wiring.args_fd)

        return b""

    READ_BYTES: ClassVar[int] = 65536

    @classmethod
    def _args_from_fd(cls, fd: int) -> bytes:
        """Канал аргументов от лончера: читается до EOF и закрывается."""
        chunks: list[bytes] = []

        try:
            while True:
                chunk = os.read(fd, cls.READ_BYTES)
                if not chunk:
                    break

                chunks.append(chunk)
        except OSError as exc:
            msg = f"reading call arguments from fd {fd} failed: {exc}"
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg) from exc
        finally:
            with suppress(OSError):
                os.close(fd)

        return b"".join(chunks)

    @staticmethod
    def _args_from_file(path: str) -> bytes:
        """Файл с тем же JSON, что лончер шлёт каналом аргументов."""
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            msg = f"reading call arguments file {path!r} failed: {exc}"
            raise ToolEntryError(InvokeErrorKind.INVALID_REQUEST, msg) from exc

    @classmethod
    def _call(cls, tool: PayloadTool, kwargs: dict[str, Any]) -> ReplyOk:
        elapsed = Elapsed()
        try:
            result = asyncio.run(cls._acall(tool, kwargs))
        except ToolFacadeError as exc:
            raise ToolEntryError(InvokeErrorKind.INTERNAL_ERROR, str(exc)) from exc
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

        return ReplyOk(content=result.llm_view(), artifact=result)

    @staticmethod
    async def _acall(tool: PayloadTool, kwargs: dict[str, Any]) -> ToolResultBase:
        try:
            return await tool.result(kwargs, False)
        finally:
            await ProcessClosers().close_all()

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
    def _tools_help(cls, tools: Sequence[PayloadTool]) -> str:
        names = ", ".join(sorted(tool.name for tool in tools))
        return f"tools: {names}"

    @classmethod
    def _tool_help(cls, tool: PayloadTool) -> str:
        """Первая строка описания и параметры тела: ключи JSON аргументов."""
        schema = ToolArgv.schema_of(tool)

        lines: list[str] = []
        first = tool.description.splitlines()
        if first:
            lines.append(first[0])

        for name, field in schema.model_fields.items():
            if ToolArgv.is_io(field.annotation):
                continue

            description = field.description or ""
            lines.append(f"  {name} {description}".rstrip())

        args_help = "call arguments as one JSON object keyed by the names above"
        lines.append(f"  {EntryFlag.ARGS} PATH  {args_help}")
        return "\n".join(lines)
