"""Kerberos-креды процесса: TGT из keytab в свой ccache, делегированные креды
входа из билета, билет одного вызова и переключение KRB5*-окружения.

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
from typing import ClassVar

import krb5

from boba.kerberos import (
    CredentialsExpiredError,
    DelegatedAuth,
    DelegationMode,
    KerberosAuth,
    KerberosError,
    KerberosPasswordAuth,
    KeytabAuth,
    KeytabError,
    SignInTicket,
    TicketAuth,
)
from boba.krb.auth import KerberosWorkspace
from boba.toolkit.timing import Elapsed

__all__ = [
    "CcacheLifetime",
    "ClientCredentials",
    "DelegatedCredentials",
    "IssuedCredentials",
    "KerberosCredentials",
    "KerberosEnv",
    "KeytabCredentials",
    "PasswordCredentials",
    "TicketCredentials",
]


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
    def of(auth: KerberosAuth) -> KerberosCredentials:
        if isinstance(auth, TicketAuth):
            return TicketCredentials(auth)

        if isinstance(auth, DelegatedAuth):
            msg = (
                "delegated kerberos credentials are resolved by the application: "
                "the connection body expects a ticket"
            )
            raise KerberosError(msg)

        if isinstance(auth, KerberosPasswordAuth):
            return PasswordCredentials.of(auth)

        return KeytabCredentials.of(auth)


class TicketCredentials(KerberosCredentials):
    """Сервисный билет одного вызова: байты ccache в приватном файле.

    Файл существует только внутри applied(): создаётся в каталоге временных
    файлов (в песочнице — приватный tmpfs вызова) и убирается на выходе.
    TGT в ccache нет: выпустить билет к другому сервису тело не может.
    """

    FILE_TYPE: ClassVar[str] = "FILE"
    PREFIX: ClassVar[str] = "krb5cc_ticket_"

    def __init__(self, config: TicketAuth) -> None:
        self._config = config
        self._path: str | None = None
        self._logger = logging.getLogger(TicketCredentials.__name__)

    @property
    def principal(self) -> str:
        return self._config.principal

    @property
    def ccache(self) -> str:
        if self._path is None:
            msg = (
                f"ticket ccache for {self._config.principal} to "
                f"{self._config.service} exists only inside applied()"
            )
            raise KerberosError(msg)

        return f"{self.FILE_TYPE}:{self._path}"

    def env(self) -> Mapping[str, str]:
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


class IssuedCredentials(KerberosCredentials):
    """TGT, который приложение выпускает само: keytab или пароль учётки.

    Экземпляр один на процесс для каждого ccache: конкурирующих kinit в один
    файл не бывает, а чужой принципал в занятый кэш не попадёт.
    """

    _instances: ClassVar[dict[str, IssuedCredentials]] = {}
    _owners: ClassVar[dict[str, str]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, principal: str, ccache: str, min_lifetime: int) -> None:
        self._principal = principal
        self._ccache = ccache
        self._min_lifetime = min_lifetime
        self._lock = threading.Lock()
        self._logger = logging.getLogger(type(self).__name__)

    @classmethod
    def _shared(cls, built: IssuedCredentials) -> IssuedCredentials:
        """Общий экземпляр на ccache; тот же кэш под другим принципалом — отказ."""
        with cls._registry_lock:
            owner = cls._owners.get(built.ccache)
            if owner is None:
                cls._owners[built.ccache] = built.principal
            elif owner != built.principal:
                msg = (
                    f"ccache {built.ccache!r} already serves {owner}; "
                    f"{built.principal} may not share it"
                )
                raise KeytabError(msg)

            found = cls._instances.get(built.ccache)
            if found is None:
                cls._instances[built.ccache] = built
                return built

            return found

    @property
    def principal(self) -> str:
        return self._principal

    @property
    def ccache(self) -> str:
        return self._ccache

    def env(self) -> Mapping[str, str]:
        return {
            KerberosEnv.CCACHE: self._ccache,
            KerberosEnv.CONFIG: KerberosWorkspace.krb5_config(),
        }

    def ensure(self) -> None:
        with self._lock:
            if self._lifetime() >= self._min_lifetime:
                return

            # kinit читает realm/kdc из krb5.conf, поэтому идёт под тем же окружением
            with KerberosEnv.applied(self.env()):
                self._acquire()

    def _lifetime(self) -> int:
        return CcacheLifetime.tgt(self._ccache, self._principal)

    def _acquire(self) -> None:
        """Выпуск TGT в свой ccache; реализация знает, чем именно."""
        raise NotImplementedError

    def _store(self, context: krb5.Context, creds: krb5.Creds) -> None:
        principal = krb5.parse_name_flags(context, self._principal.encode())
        cache = krb5.cc_resolve(context, self._ccache.encode())
        krb5.cc_initialize(context, cache, principal)
        krb5.cc_store_cred(context, cache, creds)


class KeytabCredentials(IssuedCredentials):
    """TGT принципала из явного keytab в собственный ccache."""

    def __init__(self, auth: KeytabAuth) -> None:
        super().__init__(
            auth.principal, KerberosWorkspace.ccache_for(auth), auth.min_lifetime
        )
        self._auth = auth

    @classmethod
    def of(cls, auth: KeytabAuth) -> KeytabCredentials:
        shared = cls._shared(cls(auth))
        if not isinstance(shared, KeytabCredentials):
            msg = (
                f"ccache {shared.ccache!r} already serves {type(shared).__name__}, "
                f"KeytabCredentials for {auth.principal} may not share it"
            )
            raise KeytabError(msg)

        return shared

    def env(self) -> Mapping[str, str]:
        values = dict(super().env())
        values[KerberosEnv.CLIENT_KEYTAB] = self._auth.keytab
        return values

    def _acquire(self) -> None:
        """kinit из keytab: свежий TGT замещает содержимое ccache."""
        elapsed = Elapsed()
        try:
            context = krb5.init_context()
            principal = krb5.parse_name_flags(context, self._principal.encode())
            keytab = krb5.kt_resolve(context, self._auth.keytab.encode())

            options = krb5.get_init_creds_opt_alloc(context)
            krb5.get_init_creds_opt_set_forwardable(options, True)
            krb5.get_init_creds_opt_set_renew_life(options, self._auth.renew_lifetime)

            creds = krb5.get_init_creds_keytab(context, principal, options, keytab)
            self._store(context, creds)
        except krb5.Krb5Error as exc:
            msg = f"kinit {self._principal} from {self._auth.keytab} to {self._ccache}"
            raise KeytabError(f"{msg}: {exc}") from exc

        self._logger.info(
            "kerberos: TGT %s from %s -> %s in %dms",
            self._principal,
            self._auth.keytab,
            self._ccache,
            elapsed.ms(),
        )


class PasswordCredentials(IssuedCredentials):
    """TGT принципала по паролю учётной записи в собственный ccache."""

    def __init__(self, auth: KerberosPasswordAuth) -> None:
        super().__init__(
            auth.principal, KerberosWorkspace.ccache_for(auth), auth.min_lifetime
        )
        self._auth = auth

    @classmethod
    def of(cls, auth: KerberosPasswordAuth) -> PasswordCredentials:
        shared = cls._shared(cls(auth))
        if not isinstance(shared, PasswordCredentials):
            msg = (
                f"ccache {shared.ccache!r} already serves {type(shared).__name__}, "
                f"PasswordCredentials for {auth.principal} may not share it"
            )
            raise KeytabError(msg)

        return shared

    def _acquire(self) -> None:
        """kinit по паролю: свежий TGT замещает содержимое ccache."""
        elapsed = Elapsed()
        try:
            context = krb5.init_context()
            principal = krb5.parse_name_flags(context, self._principal.encode())

            options = krb5.get_init_creds_opt_alloc(context)
            krb5.get_init_creds_opt_set_forwardable(options, True)

            secret = self._auth.password.get_secret_value().encode()
            creds = krb5.get_init_creds_password(context, principal, options, secret)
            self._store(context, creds)
        except krb5.Krb5Error as exc:
            msg = f"kinit {self._principal} by password to {self._ccache}"
            raise KeytabError(f"{msg}: {exc}") from exc

        self._logger.info(
            "kerberos: TGT %s by password -> %s in %dms",
            self._principal,
            self._ccache,
            elapsed.ms(),
        )


class DelegatedCredentials(KerberosCredentials):
    """Делегированные креды входа из билета: ccache материализуется на время applied().

    Constrained: в ccache evidence-тикет пользователя и TGT сервиса, билеты к
    бэкендам KDC выдаёт по S4U2Proxy. Forwarded: TGT пользователя. Срок — из
    билета; истёкшие креды требуют повторного входа, продлевать нечего.
    """

    FILE_TYPE: ClassVar[str] = "FILE"
    PREFIX: ClassVar[str] = "krb5cc_signin_"

    def __init__(self, ticket: SignInTicket, krb5_config: str) -> None:
        self._ticket = ticket
        self._krb5_config = krb5_config
        self._path: str | None = None
        self._logger = logging.getLogger(DelegatedCredentials.__name__)

    @property
    def principal(self) -> str:
        return self._ticket.principal

    @property
    def ccache(self) -> str:
        if self._path is None:
            msg = (
                f"sign-in ccache of {self._ticket.principal} "
                "exists only inside applied()"
            )
            raise KerberosError(msg)

        return f"{self.FILE_TYPE}:{self._path}"

    @property
    def mode(self) -> DelegationMode:
        return self._ticket.mode

    def env(self) -> Mapping[str, str]:
        return {
            KerberosEnv.CCACHE: self.ccache,
            KerberosEnv.CONFIG: self._krb5_config,
        }

    def lifetime(self) -> int:
        """Остаток делегированных кредов, сек; 0 — истекли."""
        return self._ticket.lifetime()

    def ensure(self) -> None:
        """Срок и принадлежность ccache билету; вне applied() файл живёт миг."""
        if self.lifetime() == 0:
            msg = (
                f"delegated ticket for {self._ticket.principal} expired: sign in again"
            )
            raise CredentialsExpiredError(msg)

        if self._path is not None:
            self._check_owner()
            return

        with self._materialized():
            self._check_owner()

    @contextmanager
    def applied(self) -> Generator[None, None, None]:
        with self._materialized():
            self.ensure()
            with KerberosEnv.applied(self.env()):
                yield

    @asynccontextmanager
    async def applied_async(self) -> AsyncGenerator[None, None]:
        with self._materialized():
            await asyncio.to_thread(self.ensure)
            async with KerberosEnv.applied_async(self.env()):
                yield

    @contextmanager
    def _materialized(self) -> Generator[None, None, None]:
        descriptor, path = tempfile.mkstemp(prefix=self.PREFIX)
        try:
            os.write(descriptor, self._ticket.ccache)
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
                self._logger.warning("sign-in ccache %s vanished before cleanup", path)

    def _check_owner(self) -> None:
        """Ccache билета принадлежит его принципалу; чужой билетов не выпускает."""
        try:
            context = krb5.init_context()
            cache = krb5.cc_resolve(context, self.ccache.encode())
            owner = krb5.unparse_name_flags(
                context, krb5.cc_get_principal(context, cache)
            ).decode()
        except krb5.Krb5Error as exc:
            msg = f"sign-in ccache of {self._ticket.principal} is unreadable: {exc}"
            raise KerberosError(msg) from exc

        if owner != self._ticket.principal:
            msg = (
                f"sign-in ccache {self.ccache} belongs to {owner}, "
                f"the ticket names {self._ticket.principal}"
            )
            raise KerberosError(msg)
