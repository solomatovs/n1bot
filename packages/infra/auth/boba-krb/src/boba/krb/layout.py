"""Окружение Kerberos и CA на границе запуска процесса.

Ошибки:
ConfigError — настроенный CA-файл отсутствует.
KerberosError — рабочие каталоги Kerberos не удалось подготовить."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from boba.config import ConfigError
from boba.krb import KerberosWorkspaceConfig


@dataclass(frozen=True)
class InstanceLayout:
    """Каталог экземпляра целиком: conf/ (где лежит config.toml) и данные.

    Собирает его вход процесса из каталога главного файла конфига и секции
    data; apply() задаёт процессу окружение Kerberos и CA из модели.
    """

    conf_dir: Path
    data_dir: Path
    ca_file: Path

    def krb5_conf(self) -> Path:
        return self.conf_dir / InstanceConf.KRB5_CONF.value

    def kerberos(self) -> KerberosWorkspaceConfig:
        return KerberosWorkspaceConfig(
            config=str(self.krb5_conf()),
            ccache_dir=str(self.data_dir / "krb/cache"),
        )

    def apply(self) -> None:
        """Рабочий каталог kerberos и KRB5_CONFIG, CA-файл контура в
        SSL_CERT_FILE; CA-файла нет — ConfigError."""
        ca_file = self.ca_file
        if not ca_file.is_file():
            msg = (
                f"[tls]: ca_file must name the CA file of the network "
                f"(PEM), {ca_file} is not a file"
            )
            raise ConfigError(msg)

        self.kerberos().apply()
        os.environ["SSL_CERT_FILE"] = str(ca_file)


class InstanceConf(StrEnum):
    """Файлы каталога conf/ экземпляра — того, где лежит его config.toml."""

    KRB5_CONF = "krb/krb5.conf"
    """krb5.conf; keytab'ы лежат рядом и называются в конфиге именем файла."""
