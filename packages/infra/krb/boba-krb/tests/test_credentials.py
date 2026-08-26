"""Kerberos-креды: получение TGT из keytab и процессный лок вокруг KRB5*-окружения.

Живой KDC нужен только тестам с меткой integration; остальные работают на локальном
окружении и проверяют дисциплину локов при потоках и корутинах.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import krb5
import pytest
from stand_site import Stand

from boba.connections.kerberos import DelegationMode, KeytabAuth
from boba.krb import (
    CcacheRegistry,
    KerberosEnv,
    KerberosWorkspace,
    KeytabCredentials,
    RefreshWaiters,
    UserCcache,
)

STAND = Stand.required()
KEYTAB = Path(STAND.krb_pg_keytab)
KRB5_CONF = Path(STAND.krb_config)
PRINCIPAL = STAND.service_principal
OTHER_PRINCIPAL = f"other@{STAND.krb_realm}"

live_kdc = pytest.mark.skipif(
    not STAND.live(),
    reason="нет keytab/krb5.conf локального AD",
)


@pytest.fixture
def clean_env() -> Iterator[None]:
    names = (KerberosEnv.CCACHE, KerberosEnv.CLIENT_KEYTAB, KerberosEnv.CONFIG)
    saved = {name: os.environ.get(name) for name in names}

    for name in names:
        os.environ.pop(name, None)

    yield

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
            continue
        os.environ[name] = value


@pytest.fixture
def keytab_copy(tmp_path: Path) -> Path:
    """Тот же ключ другим файлом: источник кредов различается, принципал — нет."""
    copy = tmp_path / "copy.keytab"
    copy.write_bytes(KEYTAB.read_bytes())
    return copy


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Рабочий каталог kerberos теста: кэши раскладывает приложение."""
    cache = tmp_path / "cache"
    KerberosWorkspace.configure(str(KRB5_CONF), str(cache))
    return cache


def auth(principal: str = PRINCIPAL) -> KeytabAuth:
    return KeytabAuth(
        method="kerberos_keytab",
        principal=principal,
        keytab=str(KEYTAB),
    )


def credentials(principal: str = PRINCIPAL) -> KeytabCredentials:
    return KeytabCredentials.of(auth(principal))


class TestKerberosWorkspace:
    """Кэш выделяет приложение: на принципал и источник — свой файл."""

    def test_cache_is_a_file_named_after_the_principal(self, workspace: Path) -> None:
        ccache = KerberosWorkspace.ccache_for(auth())
        if not ccache.startswith(f"FILE:{workspace}/"):
            raise AssertionError(ccache)
        if PRINCIPAL not in ccache:
            raise AssertionError(f"principal must be visible in {ccache}")

    def test_same_credentials_get_the_same_cache(self, workspace: Path) -> None:
        if KerberosWorkspace.ccache_for(auth()) != KerberosWorkspace.ccache_for(auth()):
            raise AssertionError("one keytab and principal — one cache")

    def test_other_principal_gets_another_cache(self, workspace: Path) -> None:
        if KerberosWorkspace.ccache_for(auth()) == KerberosWorkspace.ccache_for(
            auth(OTHER_PRINCIPAL)
        ):
            raise AssertionError("principals must not share a cache")

    def test_other_source_gets_another_cache(self, workspace: Path) -> None:
        other = KeytabAuth(
            method="kerberos_keytab", principal=PRINCIPAL, keytab="/other.keytab"
        )
        if KerberosWorkspace.ccache_for(auth()) == KerberosWorkspace.ccache_for(other):
            raise AssertionError("keytabs must not share a cache")

    def test_directory_is_private(self, workspace: Path) -> None:
        if workspace.stat().st_mode & 0o777 != 0o700:
            raise AssertionError(oct(workspace.stat().st_mode))

    def test_shared_instance_per_cache(self, workspace: Path) -> None:
        if KeytabCredentials.of(auth()) is not KeytabCredentials.of(auth()):
            raise AssertionError("one cache — one instance in the process")


class TestKerberosEnv:
    def test_restores_previous_values(self, clean_env: None) -> None:
        os.environ[KerberosEnv.CCACHE] = "FILE:/tmp/outer"

        with KerberosEnv.applied({KerberosEnv.CCACHE: "FILE:/tmp/inner"}):
            if os.environ[KerberosEnv.CCACHE] != "FILE:/tmp/inner":
                raise AssertionError('os.environ[KerberosEnv.CCACHE] == "FILE:/tmp/in…')

        if os.environ[KerberosEnv.CCACHE] != "FILE:/tmp/outer":
            raise AssertionError('os.environ[KerberosEnv.CCACHE] == "FILE:/tmp/outer"')

    def test_removes_variables_absent_before(self, clean_env: None) -> None:
        with KerberosEnv.applied({KerberosEnv.CCACHE: "FILE:/tmp/inner"}):
            if KerberosEnv.CCACHE not in os.environ:
                raise AssertionError("KerberosEnv.CCACHE in os.environ")

        if KerberosEnv.CCACHE in os.environ:
            raise AssertionError("KerberosEnv.CCACHE not in os.environ")

    def test_threads_never_observe_foreign_value(self, clean_env: None) -> None:
        """Два потока со своими ccache: внутри лока значение всегда своё."""
        errors: list[str] = []
        barrier = threading.Barrier(2)

        def worker(tag: str) -> None:
            barrier.wait()
            for _ in range(50):
                with KerberosEnv.applied({KerberosEnv.CCACHE: f"FILE:/tmp/{tag}"}):
                    observed = os.environ[KerberosEnv.CCACHE]
                    if observed != f"FILE:/tmp/{tag}":
                        errors.append(f"{tag} видит {observed}")

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if errors:
            raise AssertionError("not errors")

    def test_coroutines_never_observe_foreign_value(self, clean_env: None) -> None:
        """То же для корутин: applied_async держит лок на весь блок."""
        errors: list[str] = []

        async def worker(tag: str) -> None:
            for _ in range(50):
                values = {KerberosEnv.CCACHE: f"FILE:/tmp/{tag}"}
                async with KerberosEnv.applied_async(values):
                    await asyncio.sleep(0)
                    observed = os.environ[KerberosEnv.CCACHE]
                    if observed != f"FILE:/tmp/{tag}":
                        errors.append(f"{tag} видит {observed}")

        async def main() -> None:
            await asyncio.gather(worker("a"), worker("b"), worker("c"))

        asyncio.run(main())

        if errors:
            raise AssertionError("not errors")

    def test_async_wait_does_not_block_event_loop(self, clean_env: None) -> None:
        """Пока корутина ждёт лок, занятый потоком, event loop продолжает крутиться."""
        released = threading.Event()
        ticks = 0

        def holder() -> None:
            with KerberosEnv.applied({KerberosEnv.CCACHE: "FILE:/tmp/holder"}):
                released.wait(2.0)

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.005)
                ticks += 1
            released.set()

        async def waiter() -> None:
            values = {KerberosEnv.CCACHE: "FILE:/tmp/waiter"}
            async with KerberosEnv.applied_async(values):
                if os.environ[KerberosEnv.CCACHE] != "FILE:/tmp/waiter":
                    raise AssertionError('os.environ[KerberosEnv.CCACHE] == "FILE:/tm…')

        async def main() -> None:
            thread = threading.Thread(target=holder)
            thread.start()
            await asyncio.sleep(0.02)
            await asyncio.gather(waiter(), ticker())
            thread.join()

        asyncio.run(main())

        if ticks != 20:
            raise AssertionError("ticks == 20")


@live_kdc
class TestKeytabCredentials:
    def test_acquires_ticket_into_own_ccache(
        self, workspace: Path, clean_env: None
    ) -> None:
        creds = credentials()

        creds.ensure()

        if not Path(creds.ccache.removeprefix("FILE:")).is_file():
            raise AssertionError(f"ticket was not written to {creds.ccache}")

    def test_second_ensure_reuses_valid_ticket(
        self, workspace: Path, clean_env: None
    ) -> None:
        creds = credentials()

        creds.ensure()
        cache = Path(creds.ccache.removeprefix("FILE:"))
        stamp = cache.stat().st_mtime_ns
        creds.ensure()

        if cache.stat().st_mtime_ns != stamp:
            raise AssertionError("a valid ticket must not be reacquired")

    def test_applied_exposes_own_environment(
        self, workspace: Path, clean_env: None
    ) -> None:
        creds = credentials()

        with creds.applied():
            if os.environ[KerberosEnv.CCACHE] != creds.ccache:
                raise AssertionError(os.environ[KerberosEnv.CCACHE])
            if os.environ[KerberosEnv.CLIENT_KEYTAB] != str(KEYTAB):
                raise AssertionError("os.environ[KerberosEnv.CLIENT_KEYTAB] == str(KE…")

        if KerberosEnv.CCACHE in os.environ:
            raise AssertionError("KerberosEnv.CCACHE not in os.environ")

    def test_concurrent_ensure_acquires_once(
        self, workspace: Path, clean_env: None
    ) -> None:
        """Параллельные корутины не дублируют поход в KDC: ccache пишется один раз."""
        creds = credentials()

        async def main() -> None:
            await asyncio.gather(*[creds.ensure_async() for _ in range(8)])

        asyncio.run(main())
        cache = Path(creds.ccache.removeprefix("FILE:"))
        stamp = cache.stat().st_mtime_ns

        asyncio.run(main())

        if cache.stat().st_mtime_ns != stamp:
            raise AssertionError("a valid ticket must not be reacquired")

    def test_two_principals_keep_separate_ccaches(
        self, workspace: Path, keytab_copy: Path, clean_env: None
    ) -> None:
        """Разные креды в одном процессе не мешают друг другу."""
        first = credentials()
        second = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=PRINCIPAL,
                keytab=str(keytab_copy),
            )
        )

        async def main() -> None:
            async with first.applied_async():
                if os.environ[KerberosEnv.CCACHE] != first.ccache:
                    raise AssertionError(os.environ[KerberosEnv.CCACHE])

            async with second.applied_async():
                if os.environ[KerberosEnv.CCACHE] != second.ccache:
                    raise AssertionError(os.environ[KerberosEnv.CCACHE])

        asyncio.run(main())

        if first.ccache == second.ccache:
            raise AssertionError("different keytabs must not share a cache")

        for creds in (first, second):
            if not Path(creds.ccache.removeprefix("FILE:")).is_file():
                raise AssertionError(f"ticket was not written to {creds.ccache}")


class TestRefreshWaiters:
    """Ожидание повторного входа: будит регистрация кредов, иначе таймаут."""

    def test_notify_wakes_the_waiter(self) -> None:
        waiters = RefreshWaiters()

        async def main() -> bool:
            with waiters.arm("login-1") as waiting:
                waiters.notify("login-1")
                return await waiting.wait(5.0)

        if not asyncio.run(main()):
            raise AssertionError("notify must wake the waiter")

    def test_notify_before_the_wait_is_not_lost(self) -> None:
        """Обмен успел раньше ожидания: заведённое заранее событие его удержит."""
        waiters = RefreshWaiters()

        async def main() -> bool:
            with waiters.arm("login-early") as waiting:
                waiters.notify("login-early")
                await asyncio.sleep(0.01)
                return await waiting.wait(0.05)

        if not asyncio.run(main()):
            raise AssertionError("an early refresh must still end the wait")

    def test_timeout_reports_no_refresh(self) -> None:
        waiters = RefreshWaiters()

        async def main() -> bool:
            with waiters.arm("login-2") as waiting:
                return await waiting.wait(0.05)

        if asyncio.run(main()):
            raise AssertionError("nobody refreshed: wait must report a timeout")

    def test_another_login_does_not_wake(self) -> None:
        waiters = RefreshWaiters()

        async def main() -> bool:
            with waiters.arm("login-3") as waiting:
                waiters.notify("login-4")
                return await waiting.wait(0.05)

        if asyncio.run(main()):
            raise AssertionError("a foreign login must not wake the waiter")

    def test_waiting_is_forgotten_when_released(self) -> None:
        """Вкладку закрыли без logout: ожидание не должно копиться в процессе."""
        waiters = RefreshWaiters()

        async def main() -> None:
            with waiters.arm("login-gone") as waiting:
                await waiting.wait(0.01)

        asyncio.run(main())

        if waiters._events:
            raise AssertionError(f"released waiting must be gone: {waiters._events}")

    def test_registering_credentials_wakes_the_waiter(self, tmp_path: Path) -> None:
        """Ожидание снимает сам факт регистрации: реестр будит без посредников."""
        registry = CcacheRegistry(
            mode=DelegationMode.FORWARDED,
            renew=False,
            krb5_config=str(KRB5_CONF),
        )
        ccache = f"FILE:{tmp_path / 'login'}"

        async def main() -> bool:
            with registry.arm_refresh("login-5") as waiting:
                registry.register(UserCcache(PRINCIPAL, ccache, "login-5"))
                return await waiting.wait(5.0)

        if not asyncio.run(main()):
            raise AssertionError("registration must wake the waiter of that login")


@pytest.mark.integration
@live_kdc
class TestRegistryForgetsTickets:
    """Тикет входа не переживает свой вход: ccache стирается, а не забывается."""

    def test_drop_destroys_the_ccache(self, workspace: Path, tmp_path: Path) -> None:
        registry = CcacheRegistry(
            mode=DelegationMode.FORWARDED,
            renew=False,
            krb5_config=str(KRB5_CONF),
        )
        source = credentials()
        source.ensure()

        ccache = f"FILE:{tmp_path / 'login'}"
        _copy_ccache(source.ccache, ccache)
        path = Path(ccache.removeprefix("FILE:"))
        if not path.is_file():
            raise AssertionError("the sign-in ccache must exist before the drop")

        registry.register(UserCcache(PRINCIPAL, ccache, "login-drop"))
        registry.drop("login-drop")

        if path.exists():
            raise AssertionError("logout must destroy the ticket, not just forget it")


def _copy_ccache(source: str, target: str) -> None:
    """Тикет источника в свой ccache: так делегирование кладёт креды входа."""
    context = krb5.init_context()
    origin = krb5.cc_resolve(context, source.encode())
    principal = krb5.cc_get_principal(context, origin)
    cache = krb5.cc_resolve(context, target.encode())
    krb5.cc_initialize(context, cache, principal)
    for cred in origin:
        krb5.cc_store_cred(context, cache, cred)
