"""Doc-узлы: payload на настоящих каналах, обогащение args и фасады для LLM."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest
from local_stage import LocalStageLauncher

from boba.tool.doc import DocToolsConfig, build_doc_tools
from boba.tool.doc.engine import DocEngine
from boba.tool.doc.protocol import DocOp
from boba.toolkit.launcher import LauncherError, PayloadFailureError, ToolLauncher
from boba.toolkit.workflow import WorkflowError

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

_TESSDATA = "/usr/share/tessdata"


class Launchers:
    """Фабрика порта: один локальный исполнитель на все метки инструментов."""

    def __init__(self, cfg: DocToolsConfig) -> None:
        self.launcher = LocalStageLauncher(dict(DocEngine.stages(cfg)))

    def __call__(self, tool: str, /) -> ToolLauncher:
        return self.launcher


def _config(**kw: Any) -> DocToolsConfig:
    fields: dict[str, Any] = {"tessdata_path": _TESSDATA}
    fields.update(kw)
    return DocToolsConfig.model_validate(fields)


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_PDF)
    return path


class TestNodeContract:
    """Узел реально парсит документ и отвечает по контракту каналов."""

    OCR: ClassVar[dict[str, Any]] = {
        "ocr_enabled": False,
        "num_workers": 1,
        "ocr_language": "eng",
    }

    def test_read_document_returns_all_pages(self, pdf: Path) -> None:
        cfg = _config()
        engine = DocEngine(cfg, Launchers(cfg))

        answer = asyncio.run(engine.read_document(str(pdf), "1-2", **self.OCR))

        assert answer.pages == (1, 2)
        assert "Alpha page one" in answer.text
        assert "Beta page two" in answer.text
        assert answer.truncated is False

    def test_read_document_selects_subset(self, pdf: Path) -> None:
        cfg = _config()
        engine = DocEngine(cfg, Launchers(cfg))

        answer = asyncio.run(engine.read_document(str(pdf), "2", **self.OCR))

        assert answer.pages == (2,)
        assert "Beta page two" in answer.text
        assert "page one" not in answer.text

    def test_read_document_clips_text(self, pdf: Path) -> None:
        cfg = _config(max_text_chars=5)
        engine = DocEngine(cfg, Launchers(cfg))

        answer = asyncio.run(engine.read_document(str(pdf), "1-2", **self.OCR))

        assert answer.truncated is True
        assert len(answer.text) == 5

    def test_outline_has_row_per_page(self, pdf: Path) -> None:
        cfg = _config()
        engine = DocEngine(cfg, Launchers(cfg))

        answer = asyncio.run(engine.outline(str(pdf), **self.OCR))

        assert answer.num_pages == 2
        assert [row.page for row in answer.rows] == [1, 2]
        assert answer.rows[0].chars > 0

    def test_search_returns_coordinates_and_snippet(self, pdf: Path) -> None:
        cfg = _config(search_context_chars=5)
        engine = DocEngine(cfg, Launchers(cfg))

        answer = asyncio.run(engine.search(str(pdf), "Alpha", **self.OCR))

        assert [row.page for row in answer.rows] == [1, 2]
        assert "Alpha" in answer.rows[0].snippet
        assert answer.rows[0].height > 0
        assert answer.limit_reached is False

    def test_search_reports_limit(self, pdf: Path) -> None:
        cfg = _config(search_context_chars=5, search_max_matches=1)
        engine = DocEngine(cfg, Launchers(cfg))

        answer = asyncio.run(engine.search(str(pdf), "Alpha", **self.OCR))

        assert len(answer.rows) == 1
        assert answer.limit_reached is True

    def test_ndjson_rows_travel_as_lines(self, pdf: Path) -> None:
        """Продукт узла — NDJSON: одна запись на строку канала данных."""
        cfg = _config()
        launchers = Launchers(cfg)
        engine = DocEngine(cfg, launchers)

        asyncio.run(engine.outline(str(pdf), **self.OCR))

        lines = launchers.launcher.payloads[0].splitlines()
        assert len(lines) == 2

    def test_text_travels_without_protocol(self, pdf: Path) -> None:
        """Продукт read_document — сырой текст: разметки в канале нет."""
        cfg = _config()
        launchers = Launchers(cfg)
        engine = DocEngine(cfg, launchers)

        asyncio.run(engine.read_document(str(pdf), "1", **self.OCR))

        payload = launchers.launcher.payloads[0].decode("utf-8")
        assert payload.startswith("Alpha page one")

    UNREADABLE: ClassVar[tuple[str, ...]] = (
        DocOp.READ,
        DocOp.OUTLINE,
        DocOp.SEARCH,
    )

    @pytest.mark.parametrize("op", UNREADABLE)
    def test_unsupported_format_is_a_declared_failure(
        self, tmp_path: Path, op: str
    ) -> None:
        """Формат, который liteparse не читает: конверт отказа, не трейсбек.

        Нативный парсер (search_document) сообщает об этом иначе, чем публичный,
        поэтому проверяются все операции разом.
        """
        doc = tmp_path / "notes.md"
        doc.write_text("# Заметки", encoding="utf-8")
        cfg = _config()
        engine = DocEngine(cfg, Launchers(cfg))

        with pytest.raises(PayloadFailureError) as failure:
            asyncio.run(self._call(engine, op, str(doc)))

        assert failure.value.kind == "document_unreadable"
        assert ".md" in str(failure.value)

    def test_broken_args_are_rejected_before_the_stage(self, pdf: Path) -> None:
        """Аргумент не по модели запроса: отказ на валидации, без запуска."""
        cfg = _config()
        engine = DocEngine(cfg, Launchers(cfg))

        with pytest.raises(WorkflowError, match="pages"):
            asyncio.run(engine.read_document(str(pdf), "", **self.OCR))

    def test_missing_file_has_no_receipt(self, tmp_path: Path) -> None:
        """Отсутствие файла — не объявленный отказ: стадия падает трейсбеком."""
        cfg = _config()
        engine = DocEngine(cfg, Launchers(cfg))

        with pytest.raises(LauncherError):
            asyncio.run(
                engine.read_document(str(tmp_path / "нет.pdf"), "1", **self.OCR)
            )

    @staticmethod
    async def _call(engine: DocEngine, op: str, path: str) -> Any:
        if op == DocOp.READ:
            return await engine.read_document(
                path, "1", ocr_enabled=False, num_workers=1, ocr_language="eng"
            )
        if op == DocOp.SEARCH:
            return await engine.search(
                path, "x", ocr_enabled=False, num_workers=1, ocr_language="eng"
            )
        return await engine.outline(
            path, ocr_enabled=False, num_workers=1, ocr_language="eng"
        )


class TestStageArgs:
    """Обогатитель узла собирает запрос из args вызова и настроек конфига."""

    @staticmethod
    def _requests(cfg: DocToolsConfig) -> tuple[Launchers, DocEngine]:
        launchers = Launchers(cfg)
        return launchers, DocEngine(cfg, launchers)

    def test_parser_params_travel_in_request(self, pdf: Path) -> None:
        launchers, engine = self._requests(_config(ocr_enabled=True))

        asyncio.run(
            engine.read_document(
                str(pdf), "1-2", ocr_enabled=False, num_workers=3, ocr_language="rus"
            )
        )

        request = launchers.launcher.requests[0]
        assert request["ocr_enabled"] is False
        assert request["num_workers"] == 3
        assert request["ocr_language"] == "rus"

    def test_config_limits_beat_call_args(self, pdf: Path) -> None:
        """Лимиты и каталог моделей задаёт конфиг: вызов их не переопределяет."""
        launchers, engine = self._requests(_config(max_pages=7, max_text_chars=99))

        asyncio.run(
            engine.read_document(
                str(pdf), "1", ocr_enabled=False, num_workers=1, ocr_language="eng"
            )
        )

        request = launchers.launcher.requests[0]
        assert request["max_pages"] == 7
        assert request["max_text_chars"] == 99
        assert request["tessdata_path"] == _TESSDATA

    def test_pages_and_op_travel_in_request(self, pdf: Path) -> None:
        launchers, engine = self._requests(_config())

        asyncio.run(
            engine.read_document(
                str(pdf), "2-3", ocr_enabled=False, num_workers=1, ocr_language="eng"
            )
        )

        request = launchers.launcher.requests[0]
        assert request["op"] == DocOp.READ
        assert request["pages"] == "2-3"

    def test_path_from_llm_goes_as_is(self, pdf: Path) -> None:
        """Приложение путь не переписывает: его разрешает песочница."""
        launchers, engine = self._requests(_config())

        asyncio.run(
            engine.outline(
                str(pdf), ocr_enabled=False, num_workers=1, ocr_language="eng"
            )
        )

        request = launchers.launcher.requests[0]
        assert request["path"] == str(pdf)
        assert request["op"] == DocOp.OUTLINE

    def test_search_limits_come_from_config(self, pdf: Path) -> None:
        launchers, engine = self._requests(
            _config(search_context_chars=7, search_max_matches=3)
        )

        asyncio.run(
            engine.search(
                str(pdf), "Alpha", ocr_enabled=False, num_workers=1, ocr_language="eng"
            )
        )

        request = launchers.launcher.requests[0]
        assert request["context_chars"] == 7
        assert request["max_matches"] == 3


class TestStages:
    """Реестр узлов пакета: контракты потоков и модели запросов."""

    def test_registry_names(self) -> None:
        nodes = DocEngine.stages(_config())
        assert sorted(nodes) == [
            DocOp.OUTLINE,
            DocOp.READ,
            DocOp.SEARCH,
        ]

    def test_nodes_read_nothing_from_stdin(self) -> None:
        """Источник данных у doc-узла — файл, поэтому входа у него нет."""
        for node in DocEngine.stages(_config()).values():
            assert node.contract.accepts == frozenset()

    def test_stream_formats(self) -> None:
        nodes = DocEngine.stages(_config())
        assert nodes[DocOp.READ].contract.out == "text/plain"
        assert nodes[DocOp.OUTLINE].contract.out == "application/x-ndjson"
        assert nodes[DocOp.SEARCH].contract.out == "application/x-ndjson"


class TestTools:
    """Фасады для LLM: имена, схема аргументов и сборка результата."""

    @staticmethod
    def _schema(tool: Any) -> dict[str, Any]:
        """model_json_schema через Any: langchain типизирует схему как v1-модель."""
        return tool.get_input_schema().model_json_schema()

    @staticmethod
    def _tools(cfg: DocToolsConfig) -> dict[str, Any]:
        built = build_doc_tools(cfg, Launchers(cfg))
        return {tool.name: tool for tool in built}

    def test_all_tools_registered(self) -> None:
        cfg = _config()
        assert list(self._tools(cfg)) == [
            "read_document",
            "document_outline",
            "search_document",
        ]

    def test_read_document_exposes_pages_to_llm(self) -> None:
        schema = self._schema(self._tools(_config())["read_document"])
        props = schema["properties"]

        assert "pages" in props
        assert "pages" in schema["required"]
        assert "path" in schema["required"]

    @pytest.mark.parametrize(
        "name",
        ["read_document", "document_outline", "search_document"],
    )
    def test_ocr_controls_are_optional_with_defaults(self, name: str) -> None:
        schema = self._schema(self._tools(_config())[name])
        props = schema["properties"]

        assert props["ocr_enabled"]["type"] == "boolean"
        assert props["ocr_enabled"]["default"] is False
        assert props["num_workers"]["maximum"] == 4
        assert props["num_workers"]["default"] == 1
        assert props["ocr_language"]["default"] == "rus+eng"

        for control in ("ocr_enabled", "num_workers", "ocr_language"):
            assert control not in schema["required"]

    @staticmethod
    def _read(cfg: DocToolsConfig, pdf: Path, **args: Any) -> Any:
        call: dict[str, Any] = {"path": str(pdf), "pages": "1-2"}
        call.update(args)

        tools = {tool.name: tool for tool in build_doc_tools(cfg, Launchers(cfg))}

        return asyncio.run(
            tools["read_document"].ainvoke(
                {
                    "args": call,
                    "id": "call-doc",
                    "name": "read_document",
                    "type": "tool_call",
                }
            )
        )

    def test_text_carries_metadata(self, pdf: Path) -> None:
        message = self._read(_config(), pdf)

        assert "Alpha page one" in message.content
        assert message.artifact.metadata["pages"] == "1,2"

    def test_truncation_is_marked_for_llm(self, pdf: Path) -> None:
        message = self._read(_config(max_text_chars=5), pdf)

        assert "[обрезано до 5 символов]" in message.content

    def test_facade_defaults_reach_the_node(self, pdf: Path) -> None:
        """Пропущенные вызовом настройки падают на дефолты фасада, а не теряются."""
        cfg = _config()
        launchers = Launchers(cfg)
        tools = {tool.name: tool for tool in build_doc_tools(cfg, launchers)}

        asyncio.run(
            tools["read_document"].ainvoke(
                {
                    "args": {"path": str(pdf), "pages": "1-2"},
                    "id": "call-doc",
                    "name": "read_document",
                    "type": "tool_call",
                }
            )
        )

        request = launchers.launcher.requests[0]
        assert request["ocr_enabled"] is False
        assert request["num_workers"] == 1
        assert request["ocr_language"] == "rus+eng"
