"""Рабочий каталог kerberos приложения: krb5.conf, keytab'ы и кэши билетов.

Модели способов аутентификации живут в boba.kerberos; здесь —
где лежат их кэши, чтобы две строки не поделили один ccache, и где
лежат keytab'ы, которые конфиг называет именем файла.

Ошибки:
KerberosError — рабочий каталог kerberos не настроен либо принципал непригоден
    как имя файла кэша.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.kerberos import (
    CcacheKind,
    KerberosError,
    KerberosPasswordAuth,
    KeytabAuth,
)

__all__ = [
    "KerberosWorkspace",
    "KerberosWorkspaceConfig",
]


class KerberosWorkspace:
    """Каталог кэшей и krb5.conf приложения; настраивается один раз на старте.

    Кэш выделяется на пару «принципал — источник кредов»: у двух строк с
    разными keytab один файл не появится даже при одинаковом принципале.
    keytab'ы лежат рядом с krb5.conf: имя файла из конфига разрешается от его
    каталога.
    """

    CCACHE_TYPE: ClassVar[str] = CcacheKind.FILE.value
    PREFIX: ClassVar[str] = "krb5cc"
    SERVICE_CCACHE: ClassVar[str] = "krb5cc_http"
    """Кэш собственного TGT сервиса для ограниченного делегирования."""
    TAG_LENGTH: ClassVar[int] = 12
    DIR_MODE: ClassVar[int] = 0o700

    _settings: ClassVar[dict[str, str]] = {}

    @classmethod
    def configure(cls, krb5_config: str, ccache_dir: str) -> None:
        """Ставит рабочий каталог процесса; каталог создаётся приватным."""
        os.makedirs(ccache_dir, mode=cls.DIR_MODE, exist_ok=True)
        os.chmod(ccache_dir, cls.DIR_MODE)
        cls._settings = {"krb5_config": krb5_config, "ccache_dir": ccache_dir}

    @classmethod
    @contextmanager
    def scoped(cls, krb5_config: str, ccache_dir: str) -> Generator[None, None, None]:
        """Временная настройка workspace с откатом прежней.

        Workspace глобален на процесс: тест или короткий прогон, сменивший
        его без отката, ломает kinit всем последующим — они уходят в чужой
        krb5.conf. Внутри блока действует переданная настройка, на выходе
        возвращается прежняя.
        """
        before = dict(cls._settings)
        cls.configure(krb5_config, ccache_dir)

        try:
            yield
        finally:
            cls._settings = before

    @classmethod
    def krb5_config(cls) -> str:
        return cls._setting("krb5_config")

    @classmethod
    def keytab(cls, name: str) -> str:
        """keytab по имени файла: каталог krb5.conf приложения."""
        return str(Path(cls.krb5_config()).parent / name)

    @classmethod
    def service_ccache(cls) -> str:
        """FILE-ccache собственного TGT сервиса в каталоге кэшей."""
        path = os.path.join(cls._setting("ccache_dir"), cls.SERVICE_CCACHE)

        return f"{cls.CCACHE_TYPE}:{path}"

    @classmethod
    def ccache_of(cls, principal: str, source: str) -> str:
        """Имя ccache этих кредов: принципал в имени, источник в хвосте."""
        digest = hashlib.sha256(f"{principal}|{source}".encode()).hexdigest()
        tag = digest[: cls.TAG_LENGTH]
        safe = re.sub(r"[^\w.@-]", "_", principal)
        path = os.path.join(cls._setting("ccache_dir"), f"{cls.PREFIX}_{safe}_{tag}")

        return f"{cls.CCACHE_TYPE}:{path}"

    @classmethod
    def ccache_for(cls, auth: KeytabAuth | KerberosPasswordAuth) -> str:
        """Свой кэш кредов строки: keytab выделяет по пути, пароль — по методу."""
        if isinstance(auth, KeytabAuth):
            return cls.ccache_of(auth.principal, auth.keytab)

        return cls.ccache_of(auth.principal, auth.method)

    @classmethod
    def _setting(cls, name: str) -> str:
        value = cls._settings.get(name)
        if value is None:
            msg = (
                "kerberos workspace is not configured: call "
                "KerberosWorkspace.configure(krb5_config, ccache_dir) on startup"
            )
            raise KerberosError(msg)

        return value


class KerberosWorkspaceConfig(BaseModel):
    """Секция [krb]: где приложение держит krb5.conf и кэши билетов."""

    model_config = ConfigDict(extra="ignore")

    CONFIG_ENV: ClassVar[str] = "KRB5_CONFIG"
    """Переменная, из которой libkrb5 берёт krb5.conf процесса."""

    config: str = Field(
        min_length=1,
        description="krb5.conf приложения; keytab'ы лежат в его каталоге.",
    )
    ccache_dir: str = Field(
        min_length=1,
        description=(
            "Каталог кэшей билетов приложения; создаётся приватным, имена "
            "выделяются по принципалу и источнику кредов."
        ),
    )

    def apply(self) -> None:
        """Ставит рабочий каталог процесса и KRB5_CONFIG для libkrb5, которая
        читает конфиг из окружения; зовётся один раз на старте."""
        KerberosWorkspace.configure(self.config, self.ccache_dir)
        os.environ[self.CONFIG_ENV] = self.config
