"""Ключ шифрования значений таблиц: проверяется на
границе конфига, а не в момент использования."""

from __future__ import annotations

import base64
import secrets

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.runtime.config import ValueCipherConfig


class TestValueCipherConfig:
    def test_config_of_the_app_gives_a_32_byte_key(
        self, raw_config: DictConfig
    ) -> None:
        section = ValueCipherConfig.model_validate(
            {"cipher": OmegaConf.select(raw_config, "mcp_servers.cipher")}
        )

        assert len(section.cipher.get_secret_value()) == 32
        assert "**" in repr(section.cipher)

    def test_short_key_is_refused(self) -> None:
        short = base64.b64encode(secrets.token_bytes(16)).decode()

        with pytest.raises(ValidationError, match="at least 32 items") as refused:
            ValueCipherConfig.model_validate({"cipher": short})

        assert short not in str(refused.value)

    def test_key_not_in_base64_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="base64"):
            ValueCipherConfig.model_validate({"cipher": "не base64!"})
