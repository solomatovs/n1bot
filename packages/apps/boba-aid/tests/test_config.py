"""Конфиг приложения обязан разбираться моделями.

Проверяется тот же файл, с которым работает приложение: путь берётся из
BOBA_CONFIG_PATH, как и в остальных тестах. Правки структуры (новая секция,
обязательное поле, лимит профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import SecretStr, ValidationError

from boba.aid.config.app import AppConfig
from boba.aid.config.models import AgentCommandConfig, DataConfig, ProcessAgentConfig
from boba.aid.infra.bootstrap import SandboxCors
from boba.auth.config import ProxyAuthConfig
from boba.config import ConfigError, bind


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestConfigStaysValid:
    """Конфиг разбирается моделями приложения: пропущенное поле — падение."""

    def test_app_section_binds(self, raw_config: DictConfig) -> None:
        bind(raw_config, path="app", model=AppConfig)

    def test_process_agent_needs_no_technical_sections(self) -> None:
        config = ProcessAgentConfig.model_validate(
            {
                "command": "boba",
                "history": {"kind": "memory"},
                "public_url": "https://chat.invalid",
                "question_timeout_sec": 3600,
            }
        )

        process = config
        assert isinstance(process, AgentCommandConfig)
        assert process.program() == ("boba",)
        assert tuple(process.args) == ("--stdio",)
        assert process.start_timeout_sec == 30
        assert process.request_timeout_sec == 120
        assert process.stop_timeout_sec == 10
        assert tuple(config.tools.always_load) == ()
        assert config.limits.tool_result_window_share == 0.25

    def test_default_timeouts_do_not_mask_an_invalid_override(self) -> None:
        with pytest.raises(ValidationError, match="start_timeout_sec"):
            AgentCommandConfig(command="boba", start_timeout_sec=0)


class TestAppsSandbox:
    """Песочница приложений не делит источник с чатом: [server] с портом
    страницы чата в браузере или портом, который слушает сам чат, — отказ
    конфига. Песочница платформы одна на процесс."""

    CHAT: str = "http://chat.invalid:8123/aid/"

    @staticmethod
    def _app(raw_config: DictConfig, chat_url: str, port: int) -> dict[str, Any]:
        app = OmegaConf.to_container(OmegaConf.select(raw_config, "app"), resolve=True)
        assert isinstance(app, dict)
        aid = app["server"]
        aid["public_url"] = chat_url
        aid["sandbox_port"] = port

        return {str(key): value for key, value in app.items()}

    def test_sandbox_on_the_chat_origin_is_refused(
        self, raw_config: DictConfig
    ) -> None:
        app = self._app(raw_config, self.CHAT, 8123)

        with pytest.raises(ValidationError) as refused:
            AppConfig.model_validate(app)

        assert "of the chat page itself" in str(refused.value)
        assert self.CHAT in str(refused.value)

    def test_sandbox_on_the_listening_port_of_the_chat_is_refused(
        self, raw_config: DictConfig
    ) -> None:
        port = int(str(OmegaConf.select(raw_config, "app.server.port")))
        app = self._app(raw_config, self.CHAT, port)

        with pytest.raises(ValidationError) as refused:
            AppConfig.model_validate(app)

        assert "the chat itself listens on" in str(refused.value)

    def test_sandbox_on_its_own_port_binds(self, raw_config: DictConfig) -> None:
        app = self._app(raw_config, self.CHAT, 8124)

        config = AppConfig.model_validate(app)

        assert config.server.sandbox_url() == "http://chat.invalid:8124/"
        assert config.server.sandbox_origin() == "http://chat.invalid:8124"
        assert config.server.sandbox_origin() != config.server.chat_origin()


class TestSandboxCors:
    """Старт отказывает, если CORS chainlit открыт песочнице: allow_origins
    с "*", с непрозрачным "null" (так представляется iframe приложения) или
    с её источником."""

    CHAT: str = "http://chat.invalid/aid/"

    @pytest.mark.parametrize("opened", ["*", "null", "http://chat.invalid:8123"])
    def test_cors_open_to_the_sandbox_refuses_the_start(
        self, raw_config: DictConfig, opened: str
    ) -> None:
        config = AppConfig.model_validate(
            TestAppsSandbox._app(raw_config, self.CHAT, 8123)
        )

        with pytest.raises(ConfigError) as refused:
            SandboxCors(["https://chat.invalid", opened]).check(config)

        assert "allow_origins" in str(refused.value)
        assert repr(opened) in str(refused.value)

    def test_closed_cors_starts(self, raw_config: DictConfig) -> None:
        config = AppConfig.model_validate(
            TestAppsSandbox._app(raw_config, self.CHAT, 8123)
        )

        SandboxCors(["https://chat.invalid"]).check(config)


class TestProxyBackend:
    def test_duplicate_proxy_backends_are_refused(self, raw_config: DictConfig) -> None:
        raw = OmegaConf.to_container(OmegaConf.select(raw_config, "app"), resolve=True)
        assert isinstance(raw, dict)
        proxy = ProxyAuthConfig(secret=SecretStr("proxy-signing-key"))
        raw["auth"] = [proxy, proxy.model_copy(update={"path": "/auth/other"})]
        with pytest.raises(ValidationError, match="configured twice"):
            AppConfig.model_validate(raw)


def test_scalar_data_path_builds_the_instance_paths() -> None:
    raw = OmegaConf.create({"data": "/srv/boba/data"})

    paths = bind(raw, "data", DataConfig)

    assert paths.data("workspace") == Path("/srv/boba/data/workspace")


def test_scalar_data_path_rejects_a_relative_directory() -> None:
    raw = OmegaConf.create({"data": "relative/data"})

    with pytest.raises(ConfigError, match="expected an absolute path"):
        bind(raw, "data", DataConfig)


def test_instance_paths_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        DataConfig(data_dir="data")
