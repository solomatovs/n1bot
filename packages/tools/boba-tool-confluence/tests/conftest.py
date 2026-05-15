"""Pytest-фикстуры пакета boba-tool-confluence."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from boba.indexing import PipelineContext, PipelineId


@pytest.fixture
def pipeline_ctx() -> PipelineContext:
    """`PipelineContext` с тестовым `PipelineId('t')`."""
    return PipelineContext(pipeline_id=PipelineId("t"))


@pytest.fixture
def patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, Callable[[httpx.Request], httpx.Response]], None]:
    """Фабрика подмены `httpx.Client` на client с `MockTransport(handler)`.

    Принимает `target` — fully-qualified import path до атрибута `httpx.Client`,
    который нужно подменить (модули внутри codebase ссылаются на `httpx.Client`
    по-разному, единого «правильного» target нет).
    """

    def _factory(
        target: str,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        real_client = httpx.Client

        def mock_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(**kwargs)

        monkeypatch.setattr(target, mock_client)

    return _factory
