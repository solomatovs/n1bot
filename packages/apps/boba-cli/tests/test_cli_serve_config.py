"""Общий конфиг службы агента `boba serve` (conf/config.toml пакета)
разбирается её моделями поверх site-файла со всеми ключами, на которые он
ссылается: провайдер модели и пределы — одними объектами для всех профилей,
история — своим подключением с пулом; слой настроек агента с историей,
профилями моделей или профилем по умолчанию — отказ на разборе."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.cli.serve import ServeConfig, ServeSection
from boba.config import bind
from boba.runtime.config import AppLayers, ConfigFiles

CONFIG = Path(__file__).resolve().parents[1] / "conf" / "config.toml"

SITE = """
[env]
    listen_host = "0.0.0.0"
    port        = 8660
    public_url  = "https://boba.example.com/boba-agent"
    data        = "${env.base}/data"

[site]
    krb_config          = "${env.base}/conf/krb/krb5.conf"
    auth_secret         = "site-auth-secret-of-the-stand"
    agent_proxy_secret  = "site-agent-proxy-secret-of-the-stand"
    ldap_url            = "ldaps://dc.example.com:3269"
    ldap_base_dn        = "DC=example,DC=com"
    ldap_bind_user      = "reader@example.com"
    ldap_bind_password  = "reader-password"
    ldap_netbios        = "EXAMPLE"
    pg_host             = "pg.example.com"
    pg_addr             = "10.0.0.5"
    pg_port             = 5432
    aid_database        = "aid"
    pg_sslmode          = "disable"
    history_auth_method = "password"
    llm_base_url        = "https://llm.example.com/v1"
    llm_token           = "llm-token-of-the-stand"
    llm_ssl_verify      = true
    [site.agent_client]
        secret = "agent-client-secret-of-the-stand"
    [site.models]
        general = "general-model"
        search  = "search-model"
    [site.history_auth.password]
        method   = "password"
        user     = "boba_history"
        password = "history-password"
    [site.ldap_roles.memberof]
        "CN=boba,DC=example,DC=com" = ["read"]
    [site.ldap_roles.dn]
    [site.local_auth.users]
        "admin" = "admin-password"
    [site.local_auth.roles]
        "admin" = ["read"]
"""


@pytest.fixture
def raw(tmp_path: Path) -> DictConfig:
    conf = tmp_path / "conf"
    conf.mkdir()
    site = conf / "site.toml"
    site.write_text(SITE, encoding="utf-8")

    return AppLayers.compose(ConfigFiles(config=CONFIG, site=site))


class TestConfig:
    PROFILES: ClassVar[list[str]] = ["general", "search"]

    def test_the_package_config_binds(self, raw: DictConfig) -> None:
        config = bind(raw, path=ServeConfig.SECTION, model=ServeConfig)

        agent = config.agent
        assert list(agent.profiles) == self.PROFILES
        assert agent.path == "/acp"
        history = agent.history
        assert history.db_schema == "boba"
        assert history.postgres.host == "pg.example.com"
        assert history.postgres.application_name == "boba-agent"
        assert (history.postgres.pool.min_size, history.postgres.pool.max_size) == (
            2,
            16,
        )
        general = agent.profiles["general"].model
        search = agent.profiles["search"].model
        assert general.provider == search.provider
        assert general.provider.base_url == "https://llm.example.com/v1"
        assert general.context_window == search.context_window == 128000
        assert general.model == "general-model"
        assert agent.settings.sessions is not None
        assert agent.settings.sessions.max_open == 256
        assert list(agent.clients) == ["boba-chat"]

    @pytest.mark.parametrize(
        ("key", "value", "refusal"),
        [
            ("history", {"kind": "memory"}, "the history is \\[agent.history\\]"),
            ("models", {"x": {}}, "model profiles live in"),
            ("model", "general", "the default model is the first profile"),
        ],
    )
    def test_settings_layer_refusals(
        self, raw: DictConfig, key: str, value: Any, refusal: str
    ) -> None:
        section = OmegaConf.to_container(OmegaConf.select(raw, "agent"), resolve=True)
        assert isinstance(section, dict)
        section["settings"][key] = value

        with pytest.raises(ValidationError, match=refusal):
            ServeSection.model_validate(section)
