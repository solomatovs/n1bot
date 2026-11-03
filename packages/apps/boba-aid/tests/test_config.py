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
from pydantic import SecretStr, ValidationError

from boba.aid.agent.config import AgentCommandConfig, ProcessAgentConfig
from boba.aid.infra.bootstrap import SandboxCors
from boba.aid.infra.config import AppConfig
from boba.auth.config import ProxyAuthConfig
from boba.config import ConfigError, bind
from boba.identity.principal import IdentityRule


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


class TestProxyRealms:
    """Конфигурация AID исключает перенос HTTP-подписи в чужое пространство."""

    @staticmethod
    def _raw(raw_config: DictConfig) -> dict[str, Any]:
        raw = OmegaConf.to_container(OmegaConf.select(raw_config, "app"), resolve=True)
        assert isinstance(raw, dict)
        office = ProxyAuthConfig(
            identity=IdentityRule(realm="office"),
            path="/auth/office",
            secret=SecretStr("office-signing-key"),
            require_roles=False,
        )
        partner = office.model_copy(
            update={
                "identity": IdentityRule(realm="partner"),
                "path": "/auth/partner",
                "secret": SecretStr("partner-signing-key"),
            }
        )
        app = {str(key): value for key, value in raw.items()}
        app["auth"] = [office, partner]

        return app

    def test_distinct_http_proxy_routes_and_keys_bind(
        self, raw_config: DictConfig
    ) -> None:
        configured = AppConfig.model_validate(self._raw(raw_config))
        assert len(configured.proxy_entries()) == 2

    @pytest.mark.parametrize("field", ["path", "secret"])
    def test_shared_http_proxy_route_or_key_is_refused(
        self,
        raw_config: DictConfig,
        field: str,
    ) -> None:
        raw = self._raw(raw_config)
        first, second = raw["auth"]
        raw["auth"] = [first, second.model_copy(update={field: getattr(first, field)})]
        with pytest.raises(ValidationError, match=r"\[auth\]"):
            AppConfig.model_validate(raw)
