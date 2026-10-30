"""Ingest-модуль внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и отвечает
по контракту запуска, а не падает импортом.
"""

from __future__ import annotations

import json
from typing import Any

from omegaconf import DictConfig, OmegaConf

from boba.sandbox import SandboxToolConfig
from boba.sandbox.zygote import ZygoteToolCaller
from boba.stand.sandbox import needs_sandbox, needs_userns, sandbox_profile
from boba.stand.zygote import ZygoteStand
from boba.toolkit.launcher import CollectedCall
from boba.toolkit.protocol import ReplyError, ToolCommand

INGEST = "boba.tool.confluence.plugin:INGEST"
"""Объявление плагина ingest: прогрев эмбеддера объявлен в нём."""


def _section(raw_config: DictConfig) -> dict[str, Any]:
    """Секция [tool.ingest] значениями JSON, как её отдаёт сервер."""
    # тело живёт в песочнице: режимные развилки конфига — sandbox-веткой
    sandboxed = OmegaConf.merge(raw_config, {"env": {"tool_launcher": "sandbox"}})
    node = OmegaConf.select(sandboxed, "tool.ingest")
    container = OmegaConf.to_container(node, resolve=True)

    section: dict[str, Any] = {}
    if isinstance(container, dict):
        for key, value in container.items():
            section[str(key)] = value

    return section


def _caller(zygote_stand: ZygoteStand, raw_config: DictConfig) -> ZygoteToolCaller:
    """Зигота ingest: прогрев объявлен плагином, секцию ему даёт вызывающий."""
    sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile("ingest")})
    section = _section(raw_config)

    return zygote_stand.caller("ingest-test", sandbox.profile, [INGEST], warmup=section)


@needs_sandbox
@needs_userns
def test_module_loads_and_validates_config(
    zygote_stand: ZygoteStand, raw_config: DictConfig
) -> None:
    """Секция без page_workers: важно, что ответ — про поле конфига, а не
    про импорт."""
    cfg = _section(raw_config)
    cfg.pop("page_workers")
    args = json.dumps({"page_id": "1", "cfg": cfg}).encode("utf-8")
    command = ToolCommand(argv=("confluence_index_page",), args=args)

    try:
        outcome = CollectedCall.of(_caller(zygote_stand, raw_config), command)
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
