"""Зигота kb в песочнице: прогрев эмбеддера получает из секции только свои
ключи — профиль соединения postgres в долгоживущий процесс не уезжает."""

from __future__ import annotations

import logging

import pytest
from omegaconf import DictConfig

from boba.sandbox import SandboxToolConfig
from boba.stand.sandbox import (
    needs_sandbox,
    needs_userns,
    plugin_launch,
    sandbox_profile,
)
from boba.stand.zygote import WarmupTrace, ZygoteStand

KB = "boba.tool.kb.plugin:KB"
"""Объявление плагина kb: прогрев эмбеддера объявлен в нём."""

WARM_EMBEDDER = "boba.tool.kb.tools.warm_embedder"


@needs_sandbox
@needs_userns
def test_kb_zygote_warms_up_without_the_connection_profile(
    zygote_stand: ZygoteStand,
    sandbox_raw_config: DictConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Зиготе kb хост отдаёт секцию целиком, как сервис, а до неё доходит
    только ключ прогрева."""
    caplog.set_level(logging.INFO, logger="boba")
    sandbox = SandboxToolConfig.model_validate({"profile": sandbox_profile("kb")})
    section = plugin_launch(sandbox_raw_config, "kb").section
    assert "connection" in section

    try:
        zygote_stand.caller(
            "kb-test", sandbox.profile, [KB], warmup=section
        ).supervisor.start()
    finally:
        zygote_stand.stop()

    trace = WarmupTrace()
    keys = trace.keys(caplog.records)
    assert keys == ("embedding",)
    assert "connection" not in keys
    assert WARM_EMBEDDER in trace.warmed(caplog.records)
