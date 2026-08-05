"""Doc-инструменты: payload в песочнице и разбор его ответа по контракту."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_origin

import pytest
from pydantic import BaseModel

from boba.sandbox import SandboxPayload
from boba.tool.doc import DocToolsConfig, build_doc_tools
from boba.tool.doc.engine import DocEngine
from boba.tool.doc.protocol import (
    DocOutlineRow,
    DocOutlineTrailer,
    DocPagesTrailer,
    DocSearchRow,
    DocSearchTrailer,
    DocWindowTrailer,
)
from boba.toolkit.launcher import ChunkSink

# Двухстраничный PDF: стр.1 "Alpha page one", стр.2 "Beta page two Alpha again".
_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 50>>stream
BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
7 0 obj<</Length 60>>stream
BT /F1 20 Tf 20 200 Td (Beta page two Alpha again) Tj ET
endstream endobj
trailer<</Root 1 0 R/Size 8>>
%%EOF"""

_PROFILE: dict[str, Any] = {
    "rootfs": "",
    "ro_binds": ("/usr", "/bin", "/sbin", "/lib", "/lib64"),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin"},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


PAYLOAD_MODULE = "boba.tool.doc.payload"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _config(**kw: Any) -> DocToolsConfig:
    fields: dict[str, Any] = {
        "tessdata_path": (
            "/app/docker/compose/boba/build/artifacts/sandbox/data/tessdata"
        ),
        "sandbox": {
            "profile": _PROFILE,
            "override": {},
        },
    }
    fields.update(kw)
    return DocToolsConfig.model_validate(fields)


class _Caller:
    """Подменяет песочницу: тот же контракт, но payload запускается локально."""

    def __init__(self, pdf: Path) -> None:
        self.pdf = pdf
        self.requests: list[dict[str, Any]] = []

    def call_stream(
        self,
        entry: tuple[str, ...],
        request: BaseModel,
        sink: ChunkSink,
        trailer: type[BaseModel],
    ) -> Any:
        body = json.loads(request.model_dump_json())
        self.requests.append(json.loads(request.model_dump_json()))
        body["path"] = str(self.pdf)
        run = _PayloadRun(json.dumps(body))
        if run.returncode != 0:
            raise RuntimeError(run.stderr)
        for chunk in run.chunks:
            sink.write(chunk)
        if run.trailer is None:
            msg = f"payload не напечатал трейлер: {run.stdout!r}"
            raise RuntimeError(msg)
        return trailer.model_validate(run.trailer)


class _PayloadRun:
    """Локальный запуск payload'а: кадры и трейлер по тому же контракту."""

    def __init__(self, stdin: str) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        self.returncode = result.returncode
        self.stderr = result.stderr
        self.stdout = result.stdout
        self.chunks: list[str] = []
        self.trailer: dict[str, Any] | None = None
        for line in result.stdout.splitlines():
            if line.startswith(SandboxPayload.CHUNK_MARKER):
                body = line[len(SandboxPayload.CHUNK_MARKER) :]
                self.chunks.append(json.loads(body))
                continue
            if line.startswith(SandboxPayload.MARKER):
                self.trailer = json.loads(line[len(SandboxPayload.MARKER) :])

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk in self.chunks:
            rows.append(json.loads(chunk))
        return rows


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_PDF)
    return path


class _Recorder:
    """Записывает запрос и не исполняет payload: важно что послали, а не ответ."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def call_stream(
        self,
        entry: tuple[str, ...],
        request: BaseModel,
        sink: ChunkSink,
        trailer: type[BaseModel],
    ) -> Any:
        self.requests.append(json.loads(request.model_dump_json()))
        return trailer.model_validate(self._empty(trailer))

    @staticmethod
    def _empty(trailer: type[BaseModel]) -> dict[str, Any]:
        """Пустой трейлер по схеме: важно, что послали, а не что вернулось."""
        zeros: dict[Any, Any] = {int: 0, bool: False, str: "", tuple: ()}
        fields: dict[str, Any] = {}
        for name, field in trailer.model_fields.items():
            annotation = field.annotation
            origin = get_origin(annotation)
            if origin is not None:
                annotation = origin
            fields[name] = zeros[annotation]
        return fields


_LAUNCHER: dict[str, Any] = {}


class _Proxy:
    """Исполнитель разрешается в момент вызова: фикстура ставит его позже."""

    def call_text(self, command: str, stdin: str) -> Any:
        return _LAUNCHER["current"].call_text(command, stdin)

    def call_stream(self, entry: Any, request: Any, sink: Any, trailer: Any) -> Any:
        return _LAUNCHER["current"].call_stream(entry, request, sink, trailer)


def launchers(_tool: str) -> Any:
    """Фабрика-заглушка: исполнитель берётся из фикстуры при вызове."""
    return _Proxy()


@pytest.fixture
def payload_calls() -> Iterator[list[dict[str, Any]]]:
    """Исполнитель подменён: запросы инструмента складываются сюда."""
    recorder = _Recorder()
    _LAUNCHER["current"] = recorder
    yield recorder.requests
    _LAUNCHER.clear()


@pytest.fixture
def payload_runs(pdf: Path) -> Iterator[list[dict[str, Any]]]:
    """Исполнитель запускает payload локально: ответ настоящий."""
    caller = _Caller(pdf)
    _LAUNCHER["current"] = caller
    yield caller.requests
    _LAUNCHER.clear()


class TestPayloadContract:
    """Payload реально парсит документ и отвечает по контракту."""

    @staticmethod
    def _run(request: dict[str, Any]) -> _PayloadRun:
        run = _PayloadRun(json.dumps(request))
        assert run.returncode == 0, run.stderr
        assert run.trailer is not None
        return run

    @staticmethod
    def _request(pdf: Path, op: str, **kw: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "op": op,
            "path": str(pdf),
            "params": {
                "ocr_enabled": False,
                "ocr_language": "eng",
                "max_pages": 0,
                "tessdata_path": "/usr/share/tessdata",
                "num_workers": 1,
                "max_text_chars": 200_000,
            },
        }
        request.update(kw)
        return request

    def test_read_document_returns_all_pages(self, pdf: Path) -> None:
        run = self._run(self._request(pdf, "read_document", pages="1-2"))
        trailer = DocPagesTrailer.model_validate(run.trailer)
        assert trailer.pages == (1, 2)
        assert "Alpha page one" in run.text
        assert "Beta page two" in run.text
        assert trailer.truncated is False

    def test_read_document_selects_subset(self, pdf: Path) -> None:
        run = self._run(self._request(pdf, "read_document", pages="2"))
        trailer = DocPagesTrailer.model_validate(run.trailer)
        assert trailer.pages == (2,)
        assert "Beta page two" in run.text
        assert "page one" not in run.text

    def test_read_document_clips_text(self, pdf: Path) -> None:
        request = self._request(pdf, "read_document", pages="1-2")
        request["params"]["max_text_chars"] = 5
        run = self._run(request)
        assert DocPagesTrailer.model_validate(run.trailer).truncated is True
        assert len(run.text) == 5

    def test_window_reports_cursor(self, pdf: Path) -> None:
        run = self._run(
            self._request(pdf, "read_document_window", start_char=0, length=5)
        )
        trailer = DocWindowTrailer.model_validate(run.trailer)
        assert (trailer.start_char, trailer.end_char) == (0, 5)
        assert trailer.has_more is True
        assert trailer.total_chars > 5

    def test_outline_has_row_per_page(self, pdf: Path) -> None:
        run = self._run(self._request(pdf, "document_outline"))
        assert DocOutlineTrailer.model_validate(run.trailer).num_pages == 2
        rows: list[DocOutlineRow] = []
        for raw in run.rows():
            rows.append(DocOutlineRow.model_validate(raw))
        assert [row.page for row in rows] == [1, 2]
        assert rows[0].chars > 0

    def test_search_returns_coordinates_and_snippet(self, pdf: Path) -> None:
        run = self._run(self._request(pdf, "search_document", query="Alpha",
                                      context_chars=5, max_matches=50))
        rows: list[DocSearchRow] = []
        for raw in run.rows():
            rows.append(DocSearchRow.model_validate(raw))
        assert [row.page for row in rows] == [1, 2]
        assert "Alpha" in rows[0].snippet
        assert rows[0].height > 0
        assert DocSearchTrailer.model_validate(run.trailer).limit_reached is False

    def test_search_reports_limit(self, pdf: Path) -> None:
        run = self._run(self._request(pdf, "search_document", query="Alpha",
                                      context_chars=5, max_matches=1))
        assert len(run.rows()) == 1
        assert DocSearchTrailer.model_validate(run.trailer).limit_reached is True

    def test_unknown_op_fails(self, pdf: Path) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=json.dumps(self._request(pdf, "нет-такой-op")),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "unknown document op" in result.stderr

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", PAYLOAD_MODULE],
            input=json.dumps(
                self._request(tmp_path / "нет.pdf", "read_document", pages="1")
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert result.stderr.strip()


class TestEngineRequests:
    """Инструмент шлёт payload'у ровно то, что задано конфигом и LLM."""

    def test_parser_params_travel_in_request(
        self, payload_calls: list[dict[str, Any]]
    ) -> None:
        engine = DocEngine(_config(ocr_enabled=True), launchers)
        asyncio.run(
            engine.read_document(
                "/workspace/t1/upload/doc.pdf", pages="1-2", ocr_enabled=False,
                num_workers=3, ocr_language="rus"
            )
        )
        params = payload_calls[0]["params"]
        assert params["ocr_enabled"] is False
        assert params["num_workers"] == 3
        assert params["ocr_language"] == "rus"

    def test_num_workers_defaults_to_config(
        self, payload_calls: list[dict[str, Any]]
    ) -> None:
        """Настройка, не заданная вызовом, падает на значение из конфига."""
        engine = DocEngine(_config(num_workers=2), launchers)
        asyncio.run(
            engine.read_document(
                "/workspace/doc.pdf", pages="1", ocr_enabled=False, num_workers=2,
                ocr_language="rus+eng"
            )
        )
        assert payload_calls[0]["params"]["num_workers"] == 2

    def test_ocr_language_from_llm_beats_config(
        self, payload_calls: list[dict[str, Any]]
    ) -> None:
        """Язык OCR передаётся вызовом и перекрывает конфиг-значение."""
        engine = DocEngine(_config(ocr_language="eng"), launchers)
        asyncio.run(
            engine.read_document(
                "/workspace/doc.pdf", pages="1", ocr_enabled=True,
                num_workers=1, ocr_language="rus"
            )
        )
        assert payload_calls[0]["params"]["ocr_language"] == "rus"

    def test_pages_travel_in_request(self, payload_calls: list[dict[str, Any]]) -> None:
        engine = DocEngine(_config(), launchers)
        asyncio.run(
            engine.read_document(
                "/workspace/t1/upload/doc.pdf", pages="2-3", ocr_enabled=False,
                num_workers=1, ocr_language="rus+eng"
            )
        )
        assert payload_calls[0]["op"] == "read_document"
        assert payload_calls[0]["pages"] == "2-3"

    def test_path_from_llm_goes_as_is(
        self, payload_calls: list[dict[str, Any]]
    ) -> None:
        """Приложение путь не переписывает: его разрешает песочница."""
        engine = DocEngine(_config(), launchers)
        asyncio.run(
            engine.read_document(
                "/workspace/t1/upload/doc.pdf", pages="1", ocr_enabled=False,
                num_workers=1, ocr_language="rus+eng"
            )
        )
        assert payload_calls[0]["path"] == "/workspace/t1/upload/doc.pdf"

    def test_search_limits_come_from_config(
        self, payload_calls: list[dict[str, Any]]
    ) -> None:
        engine = DocEngine(
            _config(search_context_chars=7, search_max_matches=3), launchers
        )
        asyncio.run(
            engine.search(
                "/workspace/doc.pdf", "Alpha", ocr_enabled=False,
                num_workers=1, ocr_language="rus+eng"
            )
        )
        assert payload_calls[0]["context_chars"] == 7
        assert payload_calls[0]["max_matches"] == 3

    def test_op_matches_method(self, payload_calls: list[dict[str, Any]]) -> None:
        engine = DocEngine(_config(), launchers)
        asyncio.run(engine.outline(
            "/workspace/doc.pdf", ocr_enabled=False,
            num_workers=1, ocr_language="rus+eng"
        ))
        assert payload_calls[0]["op"] == "document_outline"


class TestTools:
    @staticmethod
    def _schema(tool: Any) -> dict[str, Any]:
        """model_json_schema через Any: langchain типизирует схему как v1-модель."""
        return tool.get_input_schema().model_json_schema()

    def test_all_tools_registered(self) -> None:
        names = [t.name for t in build_doc_tools(_config(), launchers)]
        assert names == [
            "read_document",
            "read_document_window",
            "document_outline",
            "search_document",
        ]

    def test_read_document_exposes_pages_to_llm(self) -> None:
        tools = {t.name: t for t in build_doc_tools(_config(), launchers)}
        schema = self._schema(tools["read_document"])
        props = schema["properties"]
        assert "pages" in props
        assert "pages" in schema["required"]
        assert "path" in schema["required"]

    @pytest.mark.parametrize("name", [
        "read_document",
        "read_document_window",
        "document_outline",
        "search_document",
    ])
    def test_ocr_controls_are_optional_with_defaults(self, name: str) -> None:
        tools = {t.name: t for t in build_doc_tools(_config(), launchers)}
        schema = self._schema(tools[name])
        props = schema["properties"]
        assert "ocr_enabled" in props
        assert props["ocr_enabled"]["type"] == "boolean"
        assert props["ocr_enabled"]["default"] is False
        assert "num_workers" in props
        assert props["num_workers"]["maximum"] == 4
        assert props["num_workers"]["default"] == 1
        assert "ocr_language" in props
        assert props["ocr_language"]["default"] == "rus+eng"
        for control in ("ocr_enabled", "num_workers", "ocr_language"):
            assert control not in schema["required"]

    def test_window_wider_than_limit_rejected(self) -> None:
        tools = {
            t.name: t for t in build_doc_tools(_config(max_text_chars=100), launchers)
        }
        window = tools["read_document_window"]
        with pytest.raises(RuntimeError, match="exceeds max_text_chars"):
            asyncio.run(
                window.ainvoke(
                    {
                        "path": "/workspace/a.pdf",
                        "start_char": 0,
                        "length": 500,
                        "ocr_enabled": False,
                        "num_workers": 1,
                    }
                )
            )

    @staticmethod
    def _read(cfg: DocToolsConfig) -> Any:
        tools = {t.name: t for t in build_doc_tools(cfg, launchers)}
        return asyncio.run(
            tools["read_document"].ainvoke(
                {
                    "args": {
                        "path": "/workspace/doc.pdf",
                        "pages": "1-2",
                        "ocr_enabled": False,
                        "num_workers": 1,
                        "ocr_language": "rus+eng",
                    },
                    "id": "call-doc",
                    "name": "read_document",
                    "type": "tool_call",
                }
            )
        )

    def test_text_carries_metadata(
        self, payload_runs: list[dict[str, Any]]
    ) -> None:
        message = self._read(_config())
        assert "Alpha page one" in message.content
        assert message.artifact.metadata["pages"] == "1,2"

    def test_truncation_is_marked_for_llm(
        self, payload_runs: list[dict[str, Any]]
    ) -> None:
        message = self._read(_config(max_text_chars=5))
        assert "[обрезано до 5 символов]" in message.content

    def test_llm_ocr_controls_reach_payload(
        self, payload_runs: list[dict[str, Any]]
    ) -> None:
        built = build_doc_tools(_config(ocr_enabled=True), launchers)
        tools = {t.name: t for t in built}
        asyncio.run(
            tools["read_document"].ainvoke(
                {
                    "args": {
                        "path": "/workspace/doc.pdf",
                        "pages": "1-2",
                        "ocr_enabled": True,
                        "num_workers": 2,
                        "ocr_language": "rus",
                    },
                    "id": "call-doc",
                    "name": "read_document",
                    "type": "tool_call",
                }
            )
        )
        params = payload_runs[0]["params"]
        assert params["ocr_enabled"] is True
        assert params["num_workers"] == 2
        assert params["ocr_language"] == "rus"

    def test_facade_defaults_reach_payload(
        self, payload_runs: list[dict[str, Any]]
    ) -> None:
        """Пропущенные фасадом настройки падают на дефолты, а не теряются."""
        tools = {t.name: t for t in build_doc_tools(_config(), launchers)}
        asyncio.run(
            tools["read_document"].ainvoke(
                {
                    "args": {"path": "/workspace/doc.pdf", "pages": "1-2"},
                    "id": "call-doc",
                    "name": "read_document",
                    "type": "tool_call",
                }
            )
        )
        params = payload_runs[0]["params"]
        assert params["ocr_enabled"] is False
        assert params["num_workers"] == 1
        assert params["ocr_language"] == "rus+eng"
