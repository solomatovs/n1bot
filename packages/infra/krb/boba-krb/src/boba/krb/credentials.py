"""Kerberos-креды процесса: TGT из keytab в свой ccache и переключение KRB5*-окружения.

Ошибки:
KeytabError — keytab недоступен или не содержит принципала.
CredentialsExpiredError — тикет истёк и не продлевается.
KerberosError — прочие сбои GSSAPI/krb5.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import ClassVar

import krb5
from gssapi import Credentials
from gssapi.raw.misc import GSSError

from boba.krb.config import KeytabConfig
from boba.krb.errors import CredentialsExpiredError, KerberosError, KeytabError

__all__ = [
    "CcacheRegistry",
    "DelegatedCredentials",
    "KerberosCredentials",
    "KerberosEnv",
    "KeytabCredentials",
    "UserCcache",
]


@dataclass(frozen=True)
class UserCcache:
    """Принципал пользователя и его ccache (значение KRB5CCNAME) — для tools."""

    principal: str
    ccache: str


class KerberosEnv:
    """Процессный лок вокруг KRB5*-переменных: активна одна конфигурация за раз.

    libkrb5 читает KRB5CCNAME/KRB5_CLIENT_KTNAME/KRB5_CONFIG из окружения процесса
    на каждом gss_acquire_cred, а окружение общее на процесс. Смена под этим локом —
    единственный способ дать разным соединениям разные keytab в одном процессе.
    Лок процессный (threading), поэтому виден и потокам, и любым event loop'ам.
    """

    CCACHE: ClassVar[str] = "KRB5CCNAME"
    CLIENT_KEYTAB: ClassVar[str] = "KRB5_CLIENT_KTNAME"
    CONFIG: ClassVar[str] = "KRB5_CONFIG"

    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    @contextmanager
    def applied(cls, values: Mapping[str, str]) -> Generator[None, None, None]:
        """Ставит переменные на время блока; поток ждёт освобождения лока."""
        cls._lock.acquire()
        try:
            with cls._swapped(values):
                yield
        finally:
            cls._lock.release()

    @classmethod
    @asynccontextmanager
    async def applied_async(
        cls, values: Mapping[str, str]
    ) -> AsyncGenerator[None, None]:
        "То же для корутин: ожидание лока уходит в поток, event loop не блокируется."
        await asyncio.to_thread(cls._lock.acquire)
        try:
            with cls._swapped(values):
                yield
        finally:
            cls._lock.release()

    @classmethod
    @contextmanager
    def _swapped(cls, values: Mapping[str, str]) -> Generator[None, None, None]:
        previous: dict[str, str | None] = {}
        for name, value in values.items():
            previous[name] = os.environ.get(name)
            os.environ[name] = value

        try:
            yield
        finally:
            for name, old in previous.items():
                if old is None:
                    os.environ.pop(name, None)
                    continue
                os.environ[name] = old


class KerberosCredentials(ABC):
    """Единый интерфейс kerberos-кредов: свой ccache, обновление и своё окружение.

    Порядок захвата локов у всех реализаций одинаков (лок кредов, затем лок
    KerberosEnv), поэтому взаимной блокировки между ними не возникает.
    """

    @property
    @abstractmethod
    def principal(self) -> str:
        """Принципал, под которым устанавливается соединение."""

    @property
    @abstractmethod
    def ccache(self) -> str:
        """Собственный ccache этих кредов (значение KRB5CCNAME)."""

    @abstractmethod
    def env(self) -> Mapping[str, str]:
        """KRB5*-переменные, описывающие эти креды."""

    @abstractmethod
    def ensure(self) -> None:
        """Гарантирует пригодный тикет в своём ccache."""

    async def ensure_async(self) -> None:
        """ensure() без блокировки event loop."""
        await asyncio.to_thread(self.ensure)

    @contextmanager
    def applied(self) -> Generator[None, None, None]:
        """Пригодный тикет и KRB5*-окружение этих кредов на время блока."""
        self.ensure()
        with KerberosEnv.applied(self.env()):
            yield

    @asynccontextmanager
    async def applied_async(self) -> AsyncGenerator[None, None]:
        """applied() для корутин: ни поход в KDC, ни ожидание лока не блокируют loop."""
        await self.ensure_async()
        async with KerberosEnv.applied_async(self.env()):
            yield


class KeytabCredentials(KerberosCredentials):
    """TGT принципала из явного keytab в собственный ccache.

    Соединение не рассчитывает на чужой krb5-кэш: тикета нет или он истекает —
    выпускается новый прямо из keytab.
    """

    def __init__(self, config: KeytabConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._logger = logging.getLogger(KeytabCredentials.__name__)

    @property
    def principal(self) -> str:
        return self._config.principal

    @property
    def ccache(self) -> str:
        return self._config.ccache

    def env(self) -> Mapping[str, str]:
        values = {
            KerberosEnv.CCACHE: self._config.ccache,
            KerberosEnv.CLIENT_KEYTAB: self._config.keytab,
        }

        if self._config.krb5_config is not None:
            values[KerberosEnv.CONFIG] = self._config.krb5_config

        return values

    def ensure(self) -> None:
        with self._lock:
            if self._lifetime() >= self._config.min_lifetime:
                return

            # kinit читает realm/kdc из krb5.conf, поэтому идёт под тем же окружением
            with KerberosEnv.applied(self.env()):
                self._acquire()

    def _lifetime(self) -> int:
        """Остаток TGT в своём ccache, сек; 0 — кэша нет, он пуст или просрочен."""
        try:
            creds = Credentials(
                usage="initiate",
                store={b"ccache": self._config.ccache.encode()},
            )
            lifetime = creds.lifetime
        except GSSError:
            # пустой или отсутствующий ccache — штатное состояние до первого kinit
            return 0

        if lifetime is None:
            return 0

        return int(lifetime)

    def _acquire(self) -> None:
        """kinit из keytab: свежий TGT замещает содержимое ccache."""
        try:
            context = krb5.init_context()
            principal = krb5.parse_name_flags(context, self._config.principal.encode())
            keytab = krb5.kt_resolve(context, self._config.keytab.encode())

            options = krb5.get_init_creds_opt_alloc(context)
            krb5.get_init_creds_opt_set_forwardable(options, True)
            krb5.get_init_creds_opt_set_renew_life(
                options, self._config.renew_lifetime
            )

            creds = krb5.get_init_creds_keytab(context, principal, options, keytab)

            cache = krb5.cc_resolve(context, self._config.ccache.encode())
            krb5.cc_initialize(context, cache, principal)
            krb5.cc_store_cred(context, cache, creds)
        except krb5.Krb5Error as exc:
            msg = (
                f"kinit {self._config.principal} from {self._config.keytab} "
                f"to {self._config.ccache}"
            )
            raise KeytabError(f"{msg}: {exc}") from exc

        self._logger.info(
            "kerberos: TGT %s from %s -> %s",
            self._config.principal,
            self._config.keytab,
            self._config.ccache,
        )


class DelegatedCredentials(KerberosCredentials):
    """Делегированный при логине тикет пользователя в его ccache.

    Из keytab не выпускается: тикет приходит от клиента, при истечении продлевается,
    а непродлеваемый требует повторного логина.
    """

    def __init__(self, user: UserCcache, *, renew: bool) -> None:
        self._user = user
        self._renew = renew
        self._lock = threading.Lock()
        self._logger = logging.getLogger(DelegatedCredentials.__name__)

    @property
    def principal(self) -> str:
        return self._user.principal

    @property
    def ccache(self) -> str:
        return self._user.ccache

    def env(self) -> Mapping[str, str]:
        return {KerberosEnv.CCACHE: self._user.ccache}

    def ensure(self) -> None:
        with self._lock:
            if self._lifetime() > 0:
                return

            if not self._renew:
                msg = f"delegated ticket for {self._user.principal} expired"
                raise CredentialsExpiredError(msg)

            self._renew_ticket()

    def _lifetime(self) -> int:
        try:
            creds = Credentials(
                usage="initiate",
                store={b"ccache": self._user.ccache.encode()},
            )
            lifetime = creds.lifetime
        except GSSError:
            return 0

        if lifetime is None:
            return 0

        return int(lifetime)

    def _renew_ticket(self) -> None:
        """Продлевает renewable-TGT в ccache пользователя."""
        try:
            context = krb5.init_context()
            cache = krb5.cc_resolve(context, self._user.ccache.encode())
            principal = krb5.cc_get_principal(context, cache)
            creds = krb5.get_renewed_creds(context, principal, cache)
            krb5.cc_initialize(context, cache, principal)
            krb5.cc_store_cred(context, cache, creds)
        except krb5.Krb5Error as exc:
            msg = f"renew delegated ticket for {self._user.principal}"
            raise CredentialsExpiredError(f"{msg}: {exc}") from exc

        self._logger.info("kerberos: renewed ticket for %s", self._user.principal)


class CcacheRegistry:
    """Реестр делегированных тикетов пользователей: принципал - его креды."""

    def __init__(self, *, renew: bool) -> None:
        self._renew = renew
        self._entries: dict[str, DelegatedCredentials] = {}
        self._lock = threading.Lock()

    def register(self, user: UserCcache) -> DelegatedCredentials:
        """Сохраняет тикет принципала, замещая прежний."""
        credentials = DelegatedCredentials(user, renew=self._renew)

        with self._lock:
            self._entries[user.principal] = credentials

        return credentials

    def of(self, principal: str) -> DelegatedCredentials | None:
        """Креды принципала или None, если тикет не захватывался."""
        with self._lock:
            return self._entries.get(principal)

    def drop(self, principal: str) -> None:
        """Убирает тикет принципала."""
        with self._lock:
            self._entries.pop(principal, None)

    async def renew(self, principal: str) -> bool:
        """Продлевает тикет принципала; False — не удалось, запись снята."""
        credentials = self.of(principal)
        if credentials is None:
            return False

        try:
            await credentials.ensure_async()
        except KerberosError:
            self.drop(principal)
            return False

        return True
