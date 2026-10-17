"""Конфиг приложения обязан разбираться моделями.

Проверяется тот же файл, с которым работает приложение: путь берётся из
BOBA_CONFIG_PATH, как и в остальных тестах. Правки структуры (новая секция,
обязательное поле, лимит профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.aid.infra.bootstrap import SandboxCors
from boba.aid.infra.config import AppConfig
from boba.config import bind
from boba.config.section import ConfigError


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestConfigStaysValid:
    """Конфиг разбирается моделями приложения: пропущенное поле — падение."""

    def test_app_section_binds(self, raw_config: DictConfig) -> None:
        bind(raw_config, path="app", model=AppConfig)


class TestAppsSandbox:
    """Песочница приложений сервера не делит источник с чатом: секция apps
    с sandbox_url на источнике [agent].public_url — отказ конфига."""

    @staticmethod
    def _app(raw_config: DictConfig, sandbox_url: str) -> dict[str, Any]:
        app = OmegaConf.to_container(OmegaConf.select(raw_config, "app"), resolve=True)
        assert isinstance(app, dict)
        servers = app["mcp"]["servers"]
        name = sorted(servers)[0]
        servers[name]["apps"] = {"sandbox_url": sandbox_url}

        return {str(key): value for key, value in app.items()}

    def test_sandbox_on_the_chat_origin_is_refused(
        self, raw_config: DictConfig
    ) -> None:
        public = str(OmegaConf.select(raw_config, "app.agent.public_url"))
        app = self._app(raw_config, urljoin(public, "/mcp-apps/sandbox.html"))

        with pytest.raises(ValidationError) as refused:
            AppConfig.model_validate(app)

        assert "of the chat itself" in str(refused.value)
        assert "public_url" in str(refused.value)

    def test_sandbox_on_its_own_origin_binds(self, raw_config: DictConfig) -> None:
        app = self._app(raw_config, "http://sandbox.invalid/mcp-apps/sandbox.html")

        config = AppConfig.model_validate(app)

        assert any(server.apps for server in config.mcp.servers.values())


class TestSandboxCors:
    """Старт с секцией apps отказывает, если CORS chainlit открыт песочнице:
    allow_origins с "*" или с её источником; без секции apps список не
    проверяется."""

    SANDBOX: str = "http://sandbox.invalid:8123/mcp-apps/sandbox.html"

    @pytest.mark.parametrize("opened", ["*", "http://sandbox.invalid:8123"])
    def test_cors_open_to_the_sandbox_refuses_the_start(
        self, raw_config: DictConfig, opened: str
    ) -> None:
        config = AppConfig.model_validate(
            TestAppsSandbox._app(raw_config, self.SANDBOX)
        )

        with pytest.raises(ConfigError) as refused:
            SandboxCors(["https://chat.invalid", opened]).check(config)

        assert "allow_origins" in str(refused.value)
        assert repr(opened) in str(refused.value)

    def test_closed_cors_or_no_apps_start(self, raw_config: DictConfig) -> None:
        drawn = AppConfig.model_validate(TestAppsSandbox._app(raw_config, self.SANDBOX))
        plain = bind(raw_config, path="app", model=AppConfig)

        SandboxCors(["https://chat.invalid"]).check(drawn)
        SandboxCors(["*"]).check(plain)
