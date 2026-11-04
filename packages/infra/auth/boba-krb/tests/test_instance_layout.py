"""InstanceLayout: пути Kerberos и проверка TLS-конфигурации."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.config import ConfigError
from boba.config.paths import PathsConfig, TlsConfig
from boba.krb.layout import InstanceLayout


def test_instance_data_layout_is_fixed(tmp_path: Path) -> None:
    instance = PathsConfig(data_dir=str(tmp_path))
    layout = InstanceLayout(tmp_path / "conf", instance, TlsConfig())

    assert instance.data("tool-logs") == tmp_path / "tool-logs"
    assert layout.krb5_conf() == tmp_path / "conf" / "krb" / "krb5.conf"
    assert layout.kerberos().ccache_dir == str(tmp_path / "krb" / "cache")


def test_missing_ca_reports_the_tls_section(tmp_path: Path) -> None:
    paths = PathsConfig(data_dir=str(tmp_path / "data"))
    tls = TlsConfig(ca_file=str(tmp_path / "missing.pem"))

    with pytest.raises(ConfigError, match=r"\[tls\]: ca_file"):
        InstanceLayout(tmp_path / "conf", paths, tls).apply()
