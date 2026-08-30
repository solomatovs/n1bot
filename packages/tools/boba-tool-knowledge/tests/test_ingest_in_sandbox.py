"""Ingest-модуль внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и отвечает
по контракту запуска, а не падает импортом.
"""

from __future__ import annotations

from omegaconf import DictConfig

from boba.sandbox import SandboxToolConfig
from boba.sandbox.guest import WarmupCall
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.settings import bind
from boba.stand.sandbox import needs_sandbox, needs_userns, sandbox_profile
from boba.tool.kb.confluence.ingest_tools import IngestWarmupConfig
from boba.toolkit.entry import ToolArgv
from boba.toolkit.protocol import ReplyError, ToolCommand

MODULE = "boba.tool.kb.confluence.ingest_tools"

ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


def _caller(raw_config: DictConfig) -> ZygoteToolCaller:
    """Зигота ingest: прогрев объявлен модулем, конфиг ему даёт вызывающий."""
    sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile()})
    profile = sandbox.profile

    warm = bind(raw_config, "tool.ingest", IngestWarmupConfig)
    calls = (
        WarmupCall(
            module=MODULE,
            hook="warm_embedder",
            config=ToolArgv.reveal(IngestWarmupConfig, warm),
        ),
    )

    supervisor = ZygoteRegistry.obtain(
        "ingest-test", profile, [MODULE], ZYGOTE, warmup_calls=calls
    )
    return ZygoteToolCaller("ingest-test", supervisor, profile)


@needs_sandbox
@needs_userns
def test_module_loads_and_validates_config(raw_config: DictConfig) -> None:
    """Пустой конфиг: важно, что ответ — про поля конфига, а не про импорт."""
    command = ToolCommand(
        argv=(
            "python3",
            "-m",
            MODULE,
            "confluence_index_pages",
            "--page-ids",
            '["1"]',
        ),
        stdin=b'{"cfg": {}}',
    )

    try:
        outcome = _caller(raw_config).run_tool(command)
    finally:
        ZygoteRegistry.stop_all()

    reply = outcome.reply
    if not (isinstance(reply, ReplyError)):
        raise AssertionError("isinstance(reply, ReplyError)")
    if reply.kind != "invalid_request":
        raise AssertionError('reply.kind == "invalid_request"')
    if "ModuleNotFoundError" in reply.message:
        raise AssertionError('"ModuleNotFoundError" not in reply.message')
    if "ImportError" in reply.message:
        raise AssertionError('"ImportError" not in reply.message')
    # параллелизм страниц задаётся явно — без него конфиг невалиден
    if "page_workers" not in reply.message:
        raise AssertionError('"page_workers" in reply.message')
