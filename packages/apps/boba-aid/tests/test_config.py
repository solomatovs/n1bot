"""Конфиг приложения обязан разбираться моделями.

Проверяется тот же файл, с которым работает приложение: путь берётся из
BOBA_CONFIG_PATH, как и в остальных тестах. Правки структуры (новая секция,
обязательное поле, лимит профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.aid.infra.bootstrap import SandboxCors
from boba.aid.infra.config import AppConfig
from boba.config import bind
from boba.config.section import ConfigError
from boba.stand.signin import SignInStand


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestConfigStaysValid:
    """Конфиг разбирается моделями приложения: пропущенное поле — падение."""

    def test_app_section_binds(self, raw_config: DictConfig) -> None:
        bind(raw_config, path="app", model=AppConfig)


class TestAppsSandbox:
    """Песочница приложений сервера не делит источник с чатом: секция apps,
    чей порт — порт страницы чата в браузере или порт, который слушает сам
    чат, — отказ конфига."""

    CHAT: str = "http://chat.invalid:8123/aid/"

    @staticmethod
    def _app(raw_config: DictConfig, apps: dict[str, Any]) -> dict[str, Any]:
        app = OmegaConf.to_container(OmegaConf.select(raw_config, "app"), resolve=True)
        assert isinstance(app, dict)
        servers = app["mcp"]["servers"]
        name = sorted(servers)[0]
        servers[name]["apps"] = apps

        return {str(key): value for key, value in app.items()}

    def test_sandbox_on_the_chat_origin_is_refused(
        self, raw_config: DictConfig
    ) -> None:
        app = self._app(raw_config, {"chat_url": self.CHAT, "port": 8123})

        with pytest.raises(ValidationError) as refused:
            AppConfig.model_validate(app)

        assert "of the chat page itself" in str(refused.value)
        assert self.CHAT in str(refused.value)

    def test_sandbox_on_the_listening_port_of_the_chat_is_refused(
        self, raw_config: DictConfig
    ) -> None:
        port = int(str(OmegaConf.select(raw_config, "app.aid.port")))
        app = self._app(raw_config, {"chat_url": self.CHAT, "port": port})

        with pytest.raises(ValidationError) as refused:
            AppConfig.model_validate(app)

        assert "the chat itself listens on" in str(refused.value)

    def test_sandbox_on_its_own_port_binds(self, raw_config: DictConfig) -> None:
        app = self._app(raw_config, {"chat_url": self.CHAT, "port": 8124})

        config = AppConfig.model_validate(app)

        sandboxes: list[str] = []
        for server in config.mcp.servers.values():
            if server.apps is not None:
                sandboxes.append(server.apps.sandbox_url())

        assert sandboxes == ["http://chat.invalid:8124/"]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "no server of profile general has a section [mcp.servers.<name>.apps]: "
            "the deployment gives the sandboxes of boba_general no port of the "
            "chat yet, so the chat draws no apps of it (plan item 5)"
        ),
    )
    def test_prod_profile_general_serves_apps_from_their_own_origin(
        self, raw_config: DictConfig
    ) -> None:
        """Профиль general: каждый его сервер рисует приложения из песочницы
        на своём источнике, отличном от источника чата."""
        config = bind(raw_config, path="app", model=AppConfig)
        general = config.profiles[SignInStand.PROFILE]

        assert general.mcp, "profile general names its mcp servers"
        for name in general.mcp:
            apps = config.mcp.servers[name].apps
            assert apps is not None, f"[mcp.servers.{name}] has a section apps"
            assert apps.origin() != apps.chat_origin(), (name, apps.origin())


class TestSandboxCors:
    """Старт с секцией apps отказывает, если CORS chainlit открыт песочнице:
    allow_origins с "*", с непрозрачным "null" (так представляется iframe
    приложения) или с её источником; без секции apps список не проверяется."""

    SANDBOX: dict[str, Any] = {"chat_url": "http://chat.invalid/aid/", "port": 8123}

    @pytest.mark.parametrize("opened", ["*", "null", "http://chat.invalid:8123"])
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
