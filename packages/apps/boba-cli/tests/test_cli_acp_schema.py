"""Кадры `boba acp` против JSON-схемы протокола и моделей SDK, золотые
кадры python-sdk. Процесс агента проходит сценарий клиентом SDK (рукопожатие,
сессия с сервером MCP, ход с рассуждениями, вызовами, вопросом разрешения и
отчётом о ходе в `_meta`, смена режима и настройки, `load` с
воспроизведением, `resume`, `list`, `fork`, `close`); каждый кадр, который
агент написал в stdout, проверяется как есть, с `_meta`: конверт JSON-RPC,
`$def` схемы по `x-method`, отсутствие чужих корневых ключей, разбор моделью
`acp.schema` и обратная запись без потерь. Золотые кадры python-sdk
проходят те же проверки, и каждый вид `session/update`, у которого есть
золотой кадр, встречается среди кадров агента.

Данные: `acp_schema/schema.json` — JSON-схема ACP v1 из репозитория
спецификации (`schema/v1/schema.json`, commit 6d08f412a7a1370d3cc9a124e3be3d6acf92641e,
как её хранит acp-tck 0.2.0); `golden/*.json` — файлы `tests/golden`
python-sdk (main, schema-v1.23.0) для кадров, которые агент принимает или
посылает."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from acp import text_block
from acp.connection import StreamDirection
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ClientCapabilities,
    CloseSessionResponse,
    ConfigOptionUpdate,
    ForkSessionResponse,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
    RequestPermissionRequest,
    ResumeSessionResponse,
    SessionNotification,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from test_cli_acp import (
    AcpProcess,
    FakeModel,
    Frames,
    McpStand,
    RecordingClient,
    call,
)

from boba.cli.acp import AcpMethod, ConfigOptionId
from boba.stand.ui.fake_llm import ScenarioName

pytestmark = pytest.mark.anyio

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "acp_schema" / "schema.json"
GOLDEN_DIR = HERE / "golden"
Document = dict[str, Any]


class WireKey(StrEnum):
    """Ключи конверта JSON-RPC и кадров ACP."""

    JSONRPC = "jsonrpc"
    ID = "id"
    METHOD = "method"
    PARAMS = "params"
    RESULT = "result"
    ERROR = "error"
    CODE = "code"
    MESSAGE = "message"
    META = "_meta"
    SESSION_UPDATE = "sessionUpdate"
    UPDATE = "update"
    VERSION = "2.0"


class SchemaKey(StrEnum):
    """Ключи JSON-схемы, по которым она читается."""

    DEFS = "$defs"
    REF = "$ref"
    REF_PREFIX = "#/$defs/"
    METHOD = "x-method"
    PROPERTIES = "properties"
    ALL_OF = "allOf"
    ANY_OF = "anyOf"
    ONE_OF = "oneOf"
    RESPONSE_SUFFIX = "Response"


class SchemaDef(StrEnum):
    """Имена `$defs`, к которым обращаемся напрямую."""

    SESSION_UPDATE = "SessionUpdate"
    INITIALIZE_REQUEST = "InitializeRequest"
    PROMPT_REQUEST = "PromptRequest"
    INITIALIZE_RESPONSE = "InitializeResponse"
    NEW_SESSION_RESPONSE = "NewSessionResponse"
    REQUEST_PERMISSION_REQUEST = "RequestPermissionRequest"
    SET_CONFIG_OPTION_RESPONSE = "SetSessionConfigOptionResponse"


class UpdateKind(StrEnum):
    """Виды `session/update`, которые агент обязан показать в сценарии."""

    USER_MESSAGE_CHUNK = "user_message_chunk"
    AGENT_MESSAGE_CHUNK = "agent_message_chunk"
    AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALL_UPDATE = "tool_call_update"
    CURRENT_MODE_UPDATE = "current_mode_update"
    CONFIG_OPTION_UPDATE = "config_option_update"
    SESSION_INFO_UPDATE = "session_info_update"
    USAGE_UPDATE = "usage_update"


class ProtocolSchema:
    """JSON-схема ACP v1: валидатор по имени `$def`, карта метода на `$def`
    параметров и результата по аннотации `x-method`, допустимые корневые ключи
    объекта (объединение `properties` по `allOf`/`anyOf`/`oneOf`/`$ref` плюс
    `_meta` — схема не ставит `additionalProperties: false`)."""

    def __init__(self, path: Path) -> None:
        self._document: Document = json.loads(path.read_text(encoding="utf-8"))
        self._defs: Mapping[str, Document] = self._document[SchemaKey.DEFS.value]
        self._validators: dict[str, Draft202012Validator] = {}

    def errors(self, def_name: str, instance: Any) -> list[str]:
        validator = self._validator(def_name)
        found: list[str] = []
        for error in validator.iter_errors(instance):
            found.append(f"{def_name}: {error.json_path}: {error.message}")

        return found

    def params_def(self, method: str) -> str | None:
        for name, definition in self._defs.items():
            if definition.get(SchemaKey.METHOD.value) != method:
                continue

            if name.endswith(SchemaKey.RESPONSE_SUFFIX.value):
                continue

            return name

        return None

    def result_def(self, method: str) -> str | None:
        for name, definition in self._defs.items():
            if definition.get(SchemaKey.METHOD.value) != method:
                continue

            if name.endswith(SchemaKey.RESPONSE_SUFFIX.value):
                return name

        return None

    def unknown_root_keys(self, def_name: str, instance: Any) -> list[str]:
        if not isinstance(instance, dict):
            return []

        allowed = self._root_properties(def_name)
        if not allowed:
            return []

        allowed.add(WireKey.META.value)
        unknown: list[str] = []
        for key in instance:
            if key not in allowed:
                unknown.append(f"{def_name}: unknown root key {key!r}")

        return unknown

    def _validator(self, def_name: str) -> Draft202012Validator:
        known = self._validators.get(def_name)
        if known is not None:
            return known

        if def_name not in self._defs:
            msg = f"the protocol schema has no $def named {def_name}"
            raise AssertionError(msg)

        wrapper = {
            "$schema": self._document["$schema"],
            SchemaKey.REF.value: f"{SchemaKey.REF_PREFIX.value}{def_name}",
            SchemaKey.DEFS.value: dict(self._defs),
        }
        Draft202012Validator.check_schema(wrapper)
        validator = Draft202012Validator(wrapper)
        self._validators[def_name] = validator

        return validator

    def _root_properties(self, def_name: str) -> set[str]:
        allowed: set[str] = set()
        seen: set[str] = set()
        self._walk(self._defs.get(def_name, {}), allowed, seen)

        return allowed

    def _walk(self, node: Any, allowed: set[str], seen: set[str]) -> None:
        if not isinstance(node, dict):
            return

        ref = node.get(SchemaKey.REF.value)
        if isinstance(ref, str) and ref.startswith(SchemaKey.REF_PREFIX.value):
            name = ref.removeprefix(SchemaKey.REF_PREFIX.value)
            if name not in seen:
                seen.add(name)
                self._walk(self._defs.get(name, {}), allowed, seen)

        properties = node.get(SchemaKey.PROPERTIES.value)
        if isinstance(properties, dict):
            allowed.update(properties.keys())

        for key in (SchemaKey.ALL_OF, SchemaKey.ANY_OF, SchemaKey.ONE_OF):
            branches = node.get(key.value)
            if not isinstance(branches, list):
                continue

            for branch in branches:
                self._walk(branch, allowed, seen)


class SdkModels:
    """Модели `acp.schema` SDK 0.12.1 для кадров агента: результат по методу,
    параметры уведомления и запроса к клиенту; обратная запись модели должна
    совпасть с кадром — так виден каждый ключ, которого в схеме SDK нет."""

    RESULTS: Mapping[str, type[BaseModel]] = {
        "initialize": InitializeResponse,
        "session/new": NewSessionResponse,
        "session/prompt": PromptResponse,
        "session/load": LoadSessionResponse,
        "session/resume": ResumeSessionResponse,
        "session/list": ListSessionsResponse,
        "session/set_mode": SetSessionModeResponse,
        "session/set_config_option": SetSessionConfigOptionResponse,
        "session/fork": ForkSessionResponse,
        "session/close": CloseSessionResponse,
    }
    PARAMS: Mapping[str, type[BaseModel]] = {
        "session/update": SessionNotification,
        "session/request_permission": RequestPermissionRequest,
    }

    def result_model(self, method: str) -> type[BaseModel]:
        model = self.RESULTS.get(method)
        if model is None:
            msg = f"no SDK response model is registered for {method}"
            raise AssertionError(msg)

        return model

    def params_model(self, method: str) -> type[BaseModel]:
        model = self.PARAMS.get(method)
        if model is None:
            msg = f"no SDK params model is registered for {method}"
            raise AssertionError(msg)

        return model

    def roundtrip(self, model: type[BaseModel], raw: Any) -> list[str]:
        try:
            parsed = model.model_validate(raw)
        except ValidationError as exc:
            return [f"{model.__name__}: {exc}"]

        dumped = parsed.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude_unset=True
        )
        if dumped != raw:
            return [
                f"{model.__name__}: round trip changed the frame: {raw} -> {dumped}"
            ]

        return []


@dataclass(frozen=True)
class Golden:
    """Золотой кадр python-sdk: `$def` схемы и модель SDK, которым он должен
    отвечать."""

    name: str
    def_name: SchemaDef
    model: type[BaseModel]

    def load(self) -> Document:
        return json.loads((GOLDEN_DIR / f"{self.name}.json").read_text("utf-8"))


class GoldenCases:
    """Соответствие золотых кадров `$defs` схемы и моделям SDK (как в
    `tests/test_golden.py` python-sdk); виды `session/update` среди них."""

    CASES: Sequence[Golden] = (
        Golden("initialize_request", SchemaDef.INITIALIZE_REQUEST, InitializeRequest),
        Golden(
            "initialize_response", SchemaDef.INITIALIZE_RESPONSE, InitializeResponse
        ),
        Golden(
            "new_session_response", SchemaDef.NEW_SESSION_RESPONSE, NewSessionResponse
        ),
        Golden("prompt_request", SchemaDef.PROMPT_REQUEST, PromptRequest),
        Golden(
            "request_permission_request",
            SchemaDef.REQUEST_PERMISSION_REQUEST,
            RequestPermissionRequest,
        ),
        Golden(
            "set_session_config_option_response",
            SchemaDef.SET_CONFIG_OPTION_RESPONSE,
            SetSessionConfigOptionResponse,
        ),
        Golden(
            "session_update_agent_message_chunk",
            SchemaDef.SESSION_UPDATE,
            AgentMessageChunk,
        ),
        Golden(
            "session_update_agent_thought_chunk",
            SchemaDef.SESSION_UPDATE,
            AgentThoughtChunk,
        ),
        Golden(
            "session_update_user_message_chunk",
            SchemaDef.SESSION_UPDATE,
            UserMessageChunk,
        ),
        Golden("session_update_tool_call", SchemaDef.SESSION_UPDATE, ToolCallStart),
        Golden(
            "session_update_tool_call_read", SchemaDef.SESSION_UPDATE, ToolCallStart
        ),
        Golden(
            "session_update_tool_call_update_content",
            SchemaDef.SESSION_UPDATE,
            ToolCallProgress,
        ),
        Golden(
            "session_update_tool_call_update_more_fields",
            SchemaDef.SESSION_UPDATE,
            ToolCallProgress,
        ),
        Golden(
            "session_update_config_option_update",
            SchemaDef.SESSION_UPDATE,
            ConfigOptionUpdate,
        ),
    )

    def update_kinds(self) -> set[str]:
        kinds: set[str] = set()
        for case in self.CASES:
            if case.def_name is not SchemaDef.SESSION_UPDATE:
                continue

            kinds.add(str(case.load()[WireKey.SESSION_UPDATE.value]))

        return kinds

    def all_files_are_cases(self) -> bool:
        on_disk: set[str] = set()
        for path in GOLDEN_DIR.glob("*.json"):
            on_disk.add(path.stem)

        named: set[str] = set()
        for case in self.CASES:
            named.add(case.name)

        return on_disk == named


class AgentFrames:
    """Кадры, которые агент написал в stdout, по записям наблюдателя: ответы
    с именем метода запроса, уведомления и запросы к клиенту."""

    def __init__(self, frames: Frames) -> None:
        self._frames = frames

    def responses(self) -> list[tuple[str, Document]]:
        methods = self._methods_by_id()
        found: list[tuple[str, Document]] = []
        for message in self._incoming():
            if WireKey.METHOD.value in message:
                continue

            method = methods.get(json.dumps(message.get(WireKey.ID.value)))
            if method is None:
                msg = f"a response to an unknown request: {message}"
                raise AssertionError(msg)

            found.append((method, message))

        return found

    def notifications(self) -> list[Document]:
        found: list[Document] = []
        for message in self._incoming():
            if WireKey.METHOD.value not in message:
                continue

            if WireKey.ID.value in message:
                continue

            found.append(message)

        return found

    def requests(self) -> list[Document]:
        found: list[Document] = []
        for message in self._incoming():
            if WireKey.METHOD.value not in message:
                continue

            if WireKey.ID.value not in message:
                continue

            found.append(message)

        return found

    def answered_methods(self) -> set[str]:
        methods: set[str] = set()
        for method, _response in self.responses():
            methods.add(method)

        return methods

    def requested_methods(self) -> set[str]:
        methods: set[str] = set()
        for request in self.requests():
            methods.add(str(request[WireKey.METHOD.value]))

        return methods

    def update_kinds(self) -> set[str]:
        kinds: set[str] = set()
        for notification in self.notifications():
            if notification[WireKey.METHOD.value] != AcpMethod.SESSION_UPDATE.value:
                continue

            update = notification[WireKey.PARAMS.value][WireKey.UPDATE.value]
            kinds.add(str(update[WireKey.SESSION_UPDATE.value]))

        return kinds

    def _incoming(self) -> Iterator[Document]:
        for event in self._frames.events:
            if event.direction is StreamDirection.INCOMING:
                yield dict(event.message)

    def _methods_by_id(self) -> dict[str, str]:
        methods: dict[str, str] = {}
        for event in self._frames.events:
            if event.direction is not StreamDirection.OUTGOING:
                continue

            message = event.message
            if WireKey.METHOD.value not in message:
                continue

            if WireKey.ID.value not in message:
                continue

            methods[json.dumps(message[WireKey.ID.value])] = str(
                message[WireKey.METHOD.value]
            )

        return methods


class FrameAudit:
    """Проверки кадров агента: конверт JSON-RPC, `$def` схемы, корневые ключи,
    модель SDK с обратной записью; методы без `$def` в схеме v1 (нестабильный
    `session/fork`) проверяются только моделью SDK."""

    def __init__(self, schema: ProtocolSchema, models: SdkModels) -> None:
        self._schema = schema
        self._models = models

    def issues(self, frames: AgentFrames) -> list[str]:
        found: list[str] = []
        for method, response in frames.responses():
            found.extend(self._response_issues(method, response))

        for notification in frames.notifications():
            found.extend(self._params_issues(notification))

        for request in frames.requests():
            found.extend(self._params_issues(request))
            if not isinstance(request[WireKey.ID.value], int | str):
                found.append(f"request id is not an integer or string: {request}")

        return found

    def _response_issues(self, method: str, response: Document) -> list[str]:
        found = self._envelope_issues(response)
        if WireKey.ERROR.value in response:
            return found + self._error_issues(response[WireKey.ERROR.value])

        if WireKey.RESULT.value not in response:
            return found

        result = response[WireKey.RESULT.value]
        def_name = self._schema.result_def(method)
        if def_name is not None:
            found.extend(self._schema.errors(def_name, result))
            found.extend(self._schema.unknown_root_keys(def_name, result))

        found.extend(self._models.roundtrip(self._models.result_model(method), result))

        return found

    def _params_issues(self, message: Document) -> list[str]:
        found = self._envelope_issues(message)
        method = str(message[WireKey.METHOD.value])
        params = message.get(WireKey.PARAMS.value)
        def_name = self._schema.params_def(method)
        if def_name is None:
            found.append(f"{method} is not a method of the protocol schema")
            return found

        found.extend(self._schema.errors(def_name, params))
        found.extend(self._schema.unknown_root_keys(def_name, params))
        found.extend(self._models.roundtrip(self._models.params_model(method), params))

        return found

    @staticmethod
    def _envelope_issues(message: Document) -> list[str]:
        found: list[str] = []
        if message.get(WireKey.JSONRPC.value) != WireKey.VERSION.value:
            found.append(f"jsonrpc is not 2.0: {message}")

        has_result = WireKey.RESULT.value in message
        has_error = WireKey.ERROR.value in message
        if has_result and has_error:
            found.append(f"a response carries both result and error: {message}")

        return found

    @staticmethod
    def _error_issues(error: Any) -> list[str]:
        found: list[str] = []
        if not isinstance(error, dict):
            return [f"error is not an object: {error!r}"]

        if not isinstance(error.get(WireKey.CODE.value), int):
            found.append(f"error code is not an integer: {error}")

        message = error.get(WireKey.MESSAGE.value)
        if not isinstance(message, str) or not message:
            found.append(f"error message is not a non-empty string: {error}")

        return found


class Exchange:
    """Сценарий против `boba acp`: все методы, которые агент объявляет, плюс
    ход с рассуждениями, вызовами инструментов, вопросом разрешения и отчётом
    о ходе; кадры копятся в наблюдателе процесса."""

    def __init__(self, process: AcpProcess, mcp: McpStand, cwd: Path) -> None:
        self._process = process
        self._mcp = mcp
        self._cwd = cwd
        self.client = RecordingClient()

    async def run(self) -> None:
        conn = await self._process.start(self.client)
        try:
            await conn.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="boba-schema-tests", version="0"),
            )
            session = await conn.new_session(
                cwd=str(self._cwd), mcp_servers=[self._mcp.server()]
            )
            session_id = session.session_id
            await conn.prompt(
                session_id=session_id,
                prompt=[text_block(ScenarioName.THINKING_ANSWER.value)],
            )
            await conn.prompt(
                session_id=session_id,
                prompt=[
                    text_block(
                        call(
                            {"name": "mcp__stand__shout", "arguments": {"text": "hi"}},
                            {"name": "mcp__stand__progress", "arguments": {"steps": 2}},
                        )
                    )
                ],
            )
            await conn.set_session_mode(session_id=session_id, mode_id="plan")
            await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODE.value,
                value="default",
            )
            await conn.load_session(
                session_id=session_id,
                cwd=str(self._cwd),
                mcp_servers=[self._mcp.server()],
            )
            await conn.resume_session(session_id=session_id, cwd=str(self._cwd))
            await conn.list_sessions(cwd=str(self._cwd))
            fork = await conn.fork_session(
                session_id=session_id,
                cwd=str(self._cwd),
                mcp_servers=[self._mcp.server()],
            )
            await conn.close_session(session_id=fork.session_id)
            await conn.close_session(session_id=session_id)
        finally:
            await conn.close()
            await self._process.close_stdin()

        assert await self._process.wait() == 0

    def progress_updates_with_meta(self) -> list[ToolCallProgress]:
        """Обновления вызова с отчётом о ходе в `_meta`."""
        found: list[ToolCallProgress] = []
        for update in self.client.of(ToolCallProgress):
            if update.field_meta is not None:
                found.append(update)

        return found


@pytest.fixture(scope="module")
def schema() -> ProtocolSchema:
    return ProtocolSchema(SCHEMA_PATH)


class TestGoldenFrames:
    def test_every_golden_file_is_a_case(self) -> None:
        assert GoldenCases().all_files_are_cases()

    @pytest.mark.parametrize("case", GoldenCases.CASES, ids=lambda case: case.name)
    def test_golden_frame_matches_the_schema_and_the_sdk_model(
        self, schema: ProtocolSchema, case: Golden
    ) -> None:
        raw = case.load()

        assert schema.errors(case.def_name, raw) == []
        assert schema.unknown_root_keys(case.def_name, raw) == []
        assert SdkModels().roundtrip(case.model, raw) == []


class TestAgentFrames:
    async def test_every_frame_of_the_exchange_matches_the_schema(
        self, schema: ProtocolSchema, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        process = AcpProcess(tmp_path, model)
        exchange = Exchange(process, mcp, tmp_path)
        try:
            await exchange.run()
        finally:
            await process.kill()

        frames = AgentFrames(process.frames)
        audit = FrameAudit(schema, SdkModels())

        assert audit.issues(frames) == []

        assert frames.answered_methods() == set(SdkModels.RESULTS)
        assert frames.requested_methods() == {AcpMethod.REQUEST_PERMISSION.value}

        kinds = frames.update_kinds()
        assert kinds == set(UpdateKind)
        assert GoldenCases().update_kinds() <= kinds
        assert exchange.progress_updates_with_meta()
