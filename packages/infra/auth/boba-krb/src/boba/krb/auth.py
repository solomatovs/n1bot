"""Рабочий каталог kerberos приложения: krb5.conf и кэши билетов.

Модели способов аутентификации живут в boba.kerberos; здесь —
где лежат их кэши, чтобы две строки не поделили один ccache.

Ошибки:
KerberosError — рабочий каталог kerberos не настроен либо принципал непригоден
    как имя файла кэша.
"""

from __future__ import annotations

import hashlib
import os
import re
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
    """

    CCACHE_TYPE: ClassVar[str] = CcacheKind.FILE.value
    PREFIX: ClassVar[str] = "krb5cc"
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
    def krb5_config(cls) -> str:
        return cls._setting("krb5_config")

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

    config: str = Field(
        min_length=1,
        description="krb5.conf приложения; тот же путь виден телу в песочнице.",
    )
    ccache_dir: str = Field(
        min_length=1,
        description=(
            "Каталог кэшей билетов приложения; создаётся приватным, имена "
            "выделяются по принципалу и источнику кредов."
        ),
    )

    def apply(self) -> None:
        """Ставит рабочий каталог процесса; зовётся один раз на старте."""
        KerberosWorkspace.configure(self.config, self.ccache_dir)
