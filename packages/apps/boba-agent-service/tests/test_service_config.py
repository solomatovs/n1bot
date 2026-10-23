"""Общий конфиг службы агента (conf/config.toml пакета) разбирается её
моделями поверх site-файла со всеми ключами, на которые он ссылается; базовый
слой файла настроек без истории postgres, с владельцем или с профилями
моделей — отказ на разборе."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.agent.settings import PostgresHistorySettings
from boba.agent_service.app import AgentSection, AgentServiceConfig
from boba.config import bind
from boba.runtime.config import AppLayers, ConfigFiles

CONFIG = Path(__file__).resolve().parents[1] / "conf" / "config.toml"

SITE = """
[env]
    listen_host   = "0.0.0.0"
    port          = 8660
    public_url    = "https://boba.example.com/boba-agent"
    data          = "${env.base}/data"
    agent_command = "${env.base}/bin/boba"

[site]
    krb_config         = "${env.base}/conf/krb/krb5.conf"
    auth_secret        = "site-auth-secret-of-the-stand"
    proxy_secret       = "site-proxy-secret-of-the-stand"
    ldap_url           = "ldaps://dc.example.com:3269"
    ldap_base_dn       = "DC=example,DC=com"
    ldap_bind_user     = "reader@example.com"
    ldap_bind_password = "reader-password"
    ldap_netbios       = "EXAMPLE"
    pg_host            = "pg.example.com"
    pg_addr            = "10.0.0.5"
    pg_port            = 5432
    aid_database       = "aid"
    pg_sslmode         = "disable"
    llm_base_url       = "https://llm.example.com/v1"
    llm_token          = "llm-token-of-the-stand"
    llm_ssl_verify     = true
    [site.agent_client]
        secret = "agent-client-secret-of-the-stand"
    [site.models]
        general = "general-model"
        search  = "search-model"
    [site.pg_auth.password]
        method   = "password"
        user     = "boba-svc"
        password = "pg-password"
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
        config = bind(raw, path=AgentServiceConfig.SECTION, model=AgentServiceConfig)

        agent = config.agent
        assert list(agent.profiles) == self.PROFILES
        assert agent.path == "/acp"
        assert agent.process.args == ("acp", "--setting-sources", "")
        history = agent.settings.history
        assert isinstance(history, PostgresHistorySettings)
        assert history.owner is None
        assert history.db_schema == "boba"
        assert history.dsn is not None
        assert "host=pg.example.com" in history.dsn.get_secret_value()
        assert list(agent.clients) == ["boba-chat"]

    @pytest.mark.parametrize(
        ("key", "value", "refusal"),
        [
            ("history", {"kind": "memory"}, "expects history of kind 'postgres'"),
            ("models", {"x": {}}, "model profiles live in"),
            ("model", "general", "the default model is the first profile"),
        ],
    )
    def test_base_layer_refusals(
        self, raw: DictConfig, key: str, value: Any, refusal: str
    ) -> None:
        section = OmegaConf.to_container(OmegaConf.select(raw, "agent"), resolve=True)
        assert isinstance(section, dict)
        section["settings"][key] = value

        with pytest.raises(ValidationError, match=refusal):
            AgentSection.model_validate(section)

    def test_owner_is_set_only_by_the_service(self, raw: DictConfig) -> None:
        section = OmegaConf.to_container(OmegaConf.select(raw, "agent"), resolve=True)
        assert isinstance(section, dict)
        section["settings"]["history"]["owner"] = "alice"

        with pytest.raises(ValidationError, match="owner is set by the service"):
            AgentSection.model_validate(section)
