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

import pytest

from boba.krb import KerberosEnv, KeytabConfig, KeytabCredentials

_KRB = Path(__file__).resolve().parents[5] / "compose" / "conf" / "krb"
KEYTAB = _KRB / "boba-svc.keytab"
KRB5_CONF = _KRB / "krb5.conf"
PRINCIPAL = "boba-svc@LOSHARA.COM"

live_kdc = pytest.mark.skipif(
    not KEYTAB.is_file() or not KRB5_CONF.is_file(),
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


def config(ccache: Path) -> KeytabConfig:
    return KeytabConfig(
        keytab=str(KEYTAB),
        principal=PRINCIPAL,
        ccache=f"FILE:{ccache}",
        krb5_config=str(KRB5_CONF),
    )


class TestKeytabConfig:
    def test_ccache_without_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="без типа"):
            KeytabConfig(keytab="k", principal="p", ccache="./krb5cc")

    def test_ccache_unknown_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="need FILE"):
            KeytabConfig(keytab="k", principal="p", ccache="WAT:./krb5cc")

    def test_process_ccache_rejected(self) -> None:
        """MEMORY/KEYRING видны всему процессу: TGT keytab живёт только в файле."""
        with pytest.raises(ValueError, match="need FILE"):
            KeytabConfig(keytab="k", principal="p", ccache="MEMORY:shared")

    def test_ccache_with_type_accepted(self) -> None:
        cfg = KeytabConfig(keytab="k", principal="p", ccache="FILE:./krb5cc")
        if cfg.ccache != "FILE:./krb5cc":
            raise AssertionError('cfg.ccache == "FILE:./krb5cc"')


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
        self, tmp_path: Path, clean_env: None
    ) -> None:
        credentials = KeytabCredentials(config(tmp_path / "cc"))

        credentials.ensure()

        if not ((tmp_path / "cc").is_file()):
            raise AssertionError('(tmp_path / "cc").is_file()')

    def test_second_ensure_reuses_valid_ticket(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        credentials = KeytabCredentials(config(tmp_path / "cc"))

        credentials.ensure()
        stamp = (tmp_path / "cc").stat().st_mtime_ns
        credentials.ensure()

        if (tmp_path / "cc").stat().st_mtime_ns != stamp:
            raise AssertionError('(tmp_path / "cc").stat().st_mtime_ns == stamp')

    def test_applied_exposes_own_environment(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        credentials = KeytabCredentials(config(tmp_path / "cc"))

        with credentials.applied():
            if os.environ[KerberosEnv.CCACHE] != f"FILE:{tmp_path / 'cc'}":
                raise AssertionError('os.environ[KerberosEnv.CCACHE] == f"FILE:{tmp_p…')
            if os.environ[KerberosEnv.CLIENT_KEYTAB] != str(KEYTAB):
                raise AssertionError("os.environ[KerberosEnv.CLIENT_KEYTAB] == str(KE…")

        if KerberosEnv.CCACHE in os.environ:
            raise AssertionError("KerberosEnv.CCACHE not in os.environ")

    def test_concurrent_ensure_acquires_once(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        """Параллельные корутины не дублируют поход в KDC: ccache пишется один раз."""
        credentials = KeytabCredentials(config(tmp_path / "cc"))

        async def main() -> None:
            await asyncio.gather(*[credentials.ensure_async() for _ in range(8)])

        asyncio.run(main())
        stamp = (tmp_path / "cc").stat().st_mtime_ns

        asyncio.run(main())

        if (tmp_path / "cc").stat().st_mtime_ns != stamp:
            raise AssertionError('(tmp_path / "cc").stat().st_mtime_ns == stamp')

    def test_two_principals_keep_separate_ccaches(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        """Разные креды в одном процессе не мешают друг другу."""
        first = KeytabCredentials(config(tmp_path / "cc_a"))
        second = KeytabCredentials(config(tmp_path / "cc_b"))

        async def main() -> None:
            async with first.applied_async():
                if os.environ[KerberosEnv.CCACHE] != f"FILE:{tmp_path / 'cc_a'}":
                    raise AssertionError('os.environ[KerberosEnv.CCACHE] == f"FILE:{t…')

            async with second.applied_async():
                if os.environ[KerberosEnv.CCACHE] != f"FILE:{tmp_path / 'cc_b'}":
                    raise AssertionError('os.environ[KerberosEnv.CCACHE] == f"FILE:{t…')

        asyncio.run(main())

        if not ((tmp_path / "cc_a").is_file()):
            raise AssertionError('(tmp_path / "cc_a").is_file()')
        if not ((tmp_path / "cc_b").is_file()):
            raise AssertionError('(tmp_path / "cc_b").is_file()')
