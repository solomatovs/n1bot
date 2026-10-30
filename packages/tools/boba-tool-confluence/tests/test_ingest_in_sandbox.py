"""Ingest-модуль внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и отвечает
по контракту запуска, а не падает импортом.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.sandbox import SandboxToolConfig
from boba.sandbox.zygote import ZygoteToolCaller
from boba.stand.sandbox import (
    needs_sandbox,
    needs_userns,
    plugin_launch,
    sandbox_profile,
)
from boba.stand.zygote import WarmupTrace, ZygoteStand
from boba.toolkit.launcher import CollectedCall
from boba.toolkit.protocol import ReplyError, ToolCommand

INGEST = "boba.tool.confluence.plugin:INGEST"
"""Объявление плагина ingest: прогрев эмбеддера объявлен в нём."""

CONFLUENCE = "boba.tool.confluence.plugin:CONFLUENCE"
"""Объявление плагина confluence: прогревов у него нет."""

WARM_EMBEDDER = "boba.tool.confluence.ingest_tools.warm_embedder"


def _section(sandbox_raw_config: DictConfig) -> dict[str, Any]:
    """Секция [tool.ingest] значениями JSON, как её отдаёт сервер; тело живёт
    в песочнице, поэтому конфиг — в режиме sandbox."""
    node = OmegaConf.select(sandbox_raw_config, "tool.ingest")
    container = OmegaConf.to_container(node, resolve=True)

    section: dict[str, Any] = {}
    if isinstance(container, dict):
        for key, value in container.items():
            section[str(key)] = value

    return section


def _caller(
    zygote_stand: ZygoteStand, sandbox_raw_config: DictConfig
) -> ZygoteToolCaller:
    """Зигота ingest: прогрев объявлен плагином, секцию ему даёт вызывающий."""
    sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile("ingest")})
    section = _section(sandbox_raw_config)

    return zygote_stand.caller("ingest-test", sandbox.profile, [INGEST], warmup=section)


@needs_sandbox
@needs_userns
def test_module_loads_and_validates_config(
    zygote_stand: ZygoteStand, sandbox_raw_config: DictConfig
) -> None:
    """Секция без page_workers: важно, что ответ — про поле конфига, а не
    про импорт."""
    cfg = _section(sandbox_raw_config)
    cfg.pop("page_workers")
    args = json.dumps({"page_id": "1", "cfg": cfg}).encode("utf-8")
    command = ToolCommand(argv=("confluence_index_page",), args=args)

    try:
        outcome = CollectedCall.of(_caller(zygote_stand, sandbox_raw_config), command)
    finally:
        zygote_stand.stop()

    reply = outcome.reply
    if not (isinstance(reply, ReplyError)):
        raise AssertionError("isinstance(reply, ReplyError)")
    text = reply.failure.llm_view()
    if reply.failure.error_kind != "ToolEntryError":
        raise AssertionError(f"failure: {reply.failure!r}")
    if "ModuleNotFoundError" in text:
        raise AssertionError(f"import failure instead of config: {text!r}")
    if "ImportError" in text:
        raise AssertionError(f"import failure instead of config: {text!r}")
    # параллелизм страниц задаётся явно — без него конфиг невалиден
    if "page_workers" not in text:
        raise AssertionError(f"page_workers must be named: {text!r}")


@needs_sandbox
@needs_userns
def test_ingest_zygote_warms_up_without_profiles_and_tokens(
    zygote_stand: ZygoteStand,
    sandbox_raw_config: DictConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Зиготе ingest хост отдаёт секцию целиком, как сервис, а до неё доходит
    только ключ прогрева: ни профиля postgres, ни профиля Confluence с
    токеном."""
    caplog.set_level(logging.INFO, logger="boba")

    try:
        _caller(zygote_stand, sandbox_raw_config).supervisor.start()
    finally:
        zygote_stand.stop()

    trace = WarmupTrace()
    keys = trace.keys(caplog.records)
    assert keys == ("embedding",)
    assert "confluence" not in keys
    assert "connection" not in keys
    assert WARM_EMBEDDER in trace.warmed(caplog.records)


@needs_sandbox
@needs_userns
def test_confluence_zygote_without_warmups_gets_an_empty_section(
    zygote_stand: ZygoteStand,
    sandbox_raw_config: DictConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """У плагина confluence прогревов нет: его зигота не видит ни одного
    ключа секции, в том числе профиля Confluence с токеном."""
    caplog.set_level(logging.INFO, logger="boba")
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile("confluence")}
    )
    section = plugin_launch(sandbox_raw_config, "confluence").section
    assert "confluence" in section

    try:
        zygote_stand.caller(
            "confluence-test", sandbox.profile, [CONFLUENCE], warmup=section
        ).supervisor.start()
    finally:
        zygote_stand.stop()

    assert WarmupTrace().keys(caplog.records) == ()
