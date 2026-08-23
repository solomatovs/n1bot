"""Kerberos-креды процесса: TGT из keytab в свой ccache, делегированные тикеты
входов, билет одного вызова и переключение KRB5*-окружения.

Ошибки:
KeytabError — keytab недоступен, не содержит принципала или ccache уже занят
    другим принципалом.
CredentialsExpiredError — тикет истёк и не продлевается.
KerberosError — прочие сбои GSSAPI/krb5; билет вызова вне applied().
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Generator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import ClassVar

import krb5

from boba.krb.config import (
    DelegatedConfig,
    DelegationMode,
    KeytabConfig,
    TicketConfig,
)
from boba.krb.errors import CredentialsExpiredError, KerberosError, KeytabError
from boba.toolkit.timing import Elapsed

__all__ = [
    "CcacheLifetime",
    "CcacheRegistry",
    "ClientCredentials",
    "DelegatedCredentials",
    "KerberosCredentials",
    "KerberosEnv",
    "KeytabCredentials",
    "TicketCredentials",
    "UserCcache",
]


@dataclass(frozen=True)
class UserCcache:
    """Делегированный тикет одного SSO-входа: принципал, ccache и метка входа.

    login — случайная метка входа; она же лежит в подписанном JWT сессии,
    так что тикет достаётся только сессии, созданной этим входом.
    """

    principal: str
    ccache: str
    login: str


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
        """То же для корутин: ожидание лока уходит в поток, loop не блокируется.

        Отмена во время ожидания не теряет лок: поток всё равно его захватит,
        и колбэк тут же отпустит.
        """
        loop = asyncio.get_running_loop()
        waiter = loop.run_in_executor(None, cls._lock.acquire)
        try:
            await waiter
        except asyncio.CancelledError:
            waiter.add_done_callback(cls._release_abandoned)
            raise

        try:
            with cls._swapped(values):
                yield
        finally:
            cls._lock.release()

    @classmethod
    def _release_abandoned(cls, waiter: asyncio.Future[bool]) -> None:
        """Лок, захваченный уже после отмены ожидающего, отпускается сразу."""
        if waiter.cancelled():
            return

        if waiter.exception() is not None:
            return

        if waiter.result():
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


class CcacheLifetime:
    """Остаток кредов принципала в ccache по krb5 API: окружение не читается.

    gssapi для той же проверки сам бы сделал kinit из KRB5_CLIENT_KTNAME
    чужого потока; krb5 только читает кэш. TGT и evidence-тикет (билет
    пользователя к самому сервису, основа S4U2Proxy) считаются раздельно.
    """

    TGT_PREFIX: ClassVar[str] = "krbtgt/"
    CONFIG_MARK: ClassVar[str] = "X-CACHECONF:"

    @classmethod
    def tgt(cls, ccache: str, principal: str) -> int:
        """Секунды до конца TGT принципала; 0 — нет, чужой, пуст или просрочен."""
        return cls._remaining(ccache, principal, tgt=True)

    @classmethod
    def evidence(cls, ccache: str, principal: str) -> int:
        """Секунды до конца сервисных билетов принципала без учёта TGT."""
        return cls._remaining(ccache, principal, tgt=False)

    @classmethod
    def _remaining(cls, ccache: str, principal: str, *, tgt: bool) -> int:
        try:
            context = krb5.init_context()
            cache = krb5.cc_resolve(context, ccache.encode())
            endtimes = list(cls._endtimes(context, cache, principal, tgt))
        except krb5.Krb5Error:
            return 0

        if not endtimes:
            return 0

        remaining = max(endtimes) - int(time.time())
        if remaining < 0:
            return 0

        return remaining

    @classmethod
    def _endtimes(
        cls, context: krb5.Context, cache: krb5.CCache, principal: str, tgt: bool
    ) -> Iterator[int]:
        for cred in cache:
            server = krb5.unparse_name_flags(context, cred.server).decode()
            if cls.CONFIG_MARK in server:
                continue

            if server.startswith(cls.TGT_PREFIX) != tgt:
                continue

            client = krb5.unparse_name_flags(context, cred.client).decode()
            if client != principal:
                continue

            yield cred.times.endtime


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


class ClientCredentials:
    """Выбор реализации клиентских кредов по их конфигу.

    Приложение работает keytab'ом, тело инструмента — готовым билетом
    вызова; делегированную секцию разрешает только приложение.
    """

    @staticmethod
    def of(
        config: KeytabConfig | DelegatedConfig | TicketConfig,
    ) -> KerberosCredentials:
        if isinstance(config, TicketConfig):
            return TicketCredentials(config)

        if isinstance(config, DelegatedConfig):
            msg = (
                "delegated kerberos credentials are resolved by the application: "
                "the connection body expects a ticket"
            )
            raise KerberosError(msg)

        return KeytabCredentials.of(config)


class TicketCredentials(KerberosCredentials):
    """Сервисный билет одного вызова: байты ccache в приватном файле.

    Файл существует только внутри applied(): создаётся в каталоге временных
    файлов (в песочнице — приватный tmpfs вызова) и убирается на выходе.
    TGT в ccache нет: выпустить билет к другому сервису тело не может.
    """

    FILE_TYPE: ClassVar[str] = "FILE"
    PREFIX: ClassVar[str] = "krb5cc_ticket_"

    def __init__(self, config: TicketConfig) -> None:
        self._config = config
        self._path: str | None = None
        self._logger = logging.getLogger(TicketCredentials.__name__)

    @property
    def principal(self) -> str:
        return self._config.principal

    @property
    def ccache(self) -> str:
        if self._path is None:
            msg = "ticket ccache exists only inside applied()"
            raise KerberosError(msg)

        return f"{self.FILE_TYPE}:{self._path}"

    def env(self) -> Mapping[str, str]:
        """Только ccache: krb5.conf лежит в песочнице по стандартному пути."""
        return {KerberosEnv.CCACHE: self.ccache}

    def ensure(self) -> None:
        """Проверка билета вне applied(): файл живёт только на время проверки."""
        if self._path is not None:
            self._check()
            return

        with self._materialized():
            self._check()

    @contextmanager
    def applied(self) -> Generator[None, None, None]:
        with self._materialized():
            self._check()
            with KerberosEnv.applied(self.env()):
                yield

    @asynccontextmanager
    async def applied_async(self) -> AsyncGenerator[None, None]:
        with self._materialized():
            await asyncio.to_thread(self._check)
            async with KerberosEnv.applied_async(self.env()):
                yield

    @contextmanager
    def _materialized(self) -> Generator[None, None, None]:
        descriptor, path = tempfile.mkstemp(prefix=self.PREFIX)
        try:
            os.write(descriptor, self._config.ccache_bytes())
        finally:
            os.close(descriptor)

        self._path = path
        try:
            yield
        finally:
            self._path = None
            try:
                os.unlink(path)
            except FileNotFoundError:
                self._logger.warning("ticket file %s vanished before cleanup", path)

    def _check(self) -> None:
        lifetime = self._lifetime()
        if lifetime >= self._config.min_lifetime:
            return

        msg = (
            f"ticket for {self._config.principal} to {self._config.service} "
            f"has {lifetime}s left; the app must issue a new one"
        )
        raise CredentialsExpiredError(msg)

    def _lifetime(self) -> int:
        """Остаток сервисного билета, сек; 0 — файл пуст, битый или просрочен."""
        try:
            context = krb5.init_context()
            cache = krb5.cc_resolve(context, self.ccache.encode())
            endtimes = [cred.times.endtime for cred in cache]
        except krb5.Krb5Error:
            return 0

        if not endtimes:
            return 0

        remaining = min(endtimes) - int(time.time())
        if remaining < 0:
            return 0

        return remaining


class KeytabCredentials(KerberosCredentials):
    """TGT принципала из явного keytab в собственный ccache.

    Соединение не рассчитывает на чужой krb5-кэш: тикета нет или он истекает —
    выпускается новый прямо из keytab. Экземпляр один на процесс для каждого
    (keytab, principal, ccache): конкурирующих kinit в один файл не бывает.
    """

    _instances: ClassVar[dict[tuple[str, str, str], KeytabCredentials]] = {}
    _owners: ClassVar[dict[str, str]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, config: KeytabConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._logger = logging.getLogger(KeytabCredentials.__name__)

    @classmethod
    def of(cls, config: KeytabConfig) -> KeytabCredentials:
        """Общий экземпляр кредов конфига; ccache под другим принципалом — отказ."""
        key = (config.keytab, config.principal, config.ccache)

        with cls._registry_lock:
            owner = cls._owners.get(config.ccache)
            if owner is None:
                cls._owners[config.ccache] = config.principal
            elif owner != config.principal:
                msg = (
                    f"ccache {config.ccache!r} already serves {owner}; "
                    f"{config.principal} may not share it"
                )
                raise KeytabError(msg)

            found = cls._instances.get(key)
            if found is None:
                found = cls(config)
                cls._instances[key] = found

            return found

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
        return CcacheLifetime.tgt(self._config.ccache, self._config.principal)

    def _acquire(self) -> None:
        """kinit из keytab: свежий TGT замещает содержимое ccache."""
        elapsed = Elapsed()
        try:
            context = krb5.init_context()
            principal = krb5.parse_name_flags(context, self._config.principal.encode())
            keytab = krb5.kt_resolve(context, self._config.keytab.encode())

            options = krb5.get_init_creds_opt_alloc(context)
            krb5.get_init_creds_opt_set_forwardable(options, True)
            krb5.get_init_creds_opt_set_renew_life(options, self._config.renew_lifetime)

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
            "kerberos: TGT %s from %s -> %s in %dms",
            self._config.principal,
            self._config.keytab,
            self._config.ccache,
            elapsed.ms(),
        )


class DelegatedCredentials(KerberosCredentials):
    """Делегированные при логине креды пользователя в ccache входа.

    Forwarded: в ccache TGT пользователя, пока жив — продлевается заранее.
    Constrained: в ccache evidence-тикет пользователя и TGT сервиса; билеты
    к бэкендам KDC выдаёт по S4U2Proxy, продлевать нечего. Истёкшие креды
    требуют повторного логина.
    """

    RENEW_BELOW: ClassVar[int] = 300
    """Остаток TGT (сек), ниже которого запрашивается продление."""

    def __init__(
        self,
        user: UserCcache,
        *,
        mode: DelegationMode,
        renew: bool,
        krb5_config: str,
    ) -> None:
        self._user = user
        self._mode = mode
        self._renew = renew
        self._krb5_config = krb5_config
        self._lock = threading.Lock()
        self._logger = logging.getLogger(DelegatedCredentials.__name__)

    @property
    def principal(self) -> str:
        return self._user.principal

    @property
    def ccache(self) -> str:
        return self._user.ccache

    @property
    def login(self) -> str:
        return self._user.login

    @property
    def mode(self) -> DelegationMode:
        return self._mode

    def env(self) -> Mapping[str, str]:
        return {
            KerberosEnv.CCACHE: self._user.ccache,
            KerberosEnv.CONFIG: self._krb5_config,
        }

    def lifetime(self) -> int:
        """Остаток делегированных кредов, сек; 0 — истекли или не читаются."""
        if self._mode is DelegationMode.FORWARDED:
            return CcacheLifetime.tgt(self._user.ccache, self._user.principal)

        return CcacheLifetime.evidence(self._user.ccache, self._user.principal)

    def ensure(self) -> None:
        with self._lock:
            lifetime = self.lifetime()
            if lifetime == 0:
                msg = (
                    f"delegated ticket for {self._user.principal} expired: "
                    "sign in again"
                )
                raise CredentialsExpiredError(msg)

            if self._mode is not DelegationMode.FORWARDED:
                return

            if lifetime >= self.RENEW_BELOW:
                return

            if not self._renew:
                return

            with KerberosEnv.applied(self.env()):
                self._renew_ticket()

    def _renew_ticket(self) -> None:
        """Продлевает ещё живой renewable-TGT в ccache пользователя."""
        elapsed = Elapsed()
        try:
            context = krb5.init_context()
            cache = krb5.cc_resolve(context, self._user.ccache.encode())
            principal = krb5.cc_get_principal(context, cache)
            creds = krb5.get_renewed_creds(context, principal, cache)
            krb5.cc_initialize(context, cache, principal)
            krb5.cc_store_cred(context, cache, creds)
        except krb5.Krb5Error as exc:
            msg = f"renew delegated ticket for {self._user.principal}: sign in again"
            raise CredentialsExpiredError(f"{msg}: {exc}") from exc

        self._logger.info(
            "kerberos: renewed ticket for %s in %dms",
            self._user.principal,
            elapsed.ms(),
        )


class CcacheRegistry:
    """Реестр делегированных тикетов по метке входа.

    Метка входа — ключ сессии. Истёкшие входы выметаются при каждой новой
    регистрации: тикет без logout'а не живёт в процессе дольше своего срока.
    """

    def __init__(
        self, *, mode: DelegationMode, renew: bool, krb5_config: str
    ) -> None:
        self._mode = mode
        self._renew = renew
        self._krb5_config = krb5_config
        self._by_login: dict[str, DelegatedCredentials] = {}
        self._lock = threading.Lock()

    @property
    def mode(self) -> DelegationMode:
        return self._mode

    def register(self, user: UserCcache) -> DelegatedCredentials:
        """Сохраняет креды входа, попутно забывая истёкшие."""
        credentials = DelegatedCredentials(
            user,
            mode=self._mode,
            renew=self._renew,
            krb5_config=self._krb5_config,
        )

        with self._lock:
            for login in list(self._expired()):
                del self._by_login[login]

            self._by_login[user.login] = credentials

        return credentials

    def of_login(self, login: str) -> DelegatedCredentials | None:
        with self._lock:
            return self._by_login.get(login)

    def drop(self, login: str) -> None:
        """Забывает тикет входа; неизвестная метка — не ошибка."""
        with self._lock:
            self._by_login.pop(login, None)

    def _expired(self) -> Iterator[str]:
        for login, credentials in self._by_login.items():
            if credentials.lifetime() == 0:
                yield login
