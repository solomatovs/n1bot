"""Ingest-модуль внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и отвечает
по контракту запуска, а не падает импортом.
"""

from __future__ import annotations

from omegaconf import DictConfig, OmegaConf

from boba.config import bind
from boba.sandbox import SandboxToolConfig
from boba.sandbox.guest import WarmupCall
from boba.sandbox.zygote import ZygotePolicy, ZygoteToolCaller
from boba.stand.sandbox import needs_sandbox, needs_userns, sandbox_profile
from boba.stand.zygote import ZygoteStand
from boba.tool.confluence.ingest_tools import IngestWarmupConfig
from boba.toolkit.chain import CallAmbient
from boba.toolkit.entry import ToolArgv
from boba.toolkit.launcher import CollectedCall
from boba.toolkit.protocol import ReplyError, ToolCommand

MODULE = "boba.tool.confluence.ingest_tools"

ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


def _caller(zygote_stand: ZygoteStand, raw_config: DictConfig) -> ZygoteToolCaller:
    """Зигота ingest: прогрев объявлен модулем, конфиг ему даёт вызывающий."""
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile("boba-tool-confluence")}
    )
    profile = sandbox.profile

    # тело живёт в песочнице: режимные развилки конфига — sandbox-веткой
    sandboxed = raw_config.copy()
    OmegaConf.update(sandboxed, "env.tool_launcher", "sandbox")
    warm = bind(sandboxed, "tool.ingest", IngestWarmupConfig)
    calls = (
        WarmupCall(
            module=MODULE,
            hook="warm_embedder",
            config=ToolArgv.reveal(IngestWarmupConfig, warm),
        ),
    )

    supervisor = zygote_stand.registry().obtain(
        "ingest-test", profile, [MODULE], ZYGOTE, warmup_calls=calls
    )
    return ZygoteToolCaller("ingest-test", supervisor, profile, CallAmbient())


@needs_sandbox
@needs_userns
def test_module_loads_and_validates_config(
    zygote_stand: ZygoteStand, raw_config: DictConfig
) -> None:
    """Пустой конфиг: важно, что ответ — про поля конфига, а не про импорт."""
    command = ToolCommand(
        argv=(
            "python3",
            "-m",
            MODULE,
            "confluence_index_page",
            "--page-id",
            "1",
        ),
        config=b'{"cfg": {}}',
    )

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
