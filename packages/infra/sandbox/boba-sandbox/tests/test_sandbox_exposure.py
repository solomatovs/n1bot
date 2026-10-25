"""Что тело инструмента видит и может в боевых профилях песочницы.

Профили берутся из конфига приложения целиком, а не списком в тесте: новый
профиль или новый бинд проверяется этими же тестами сам собой. Каждый тест
воспроизводит найденную утечку на живой цепочке запуска — зигота секции,
исполнитель вызова, тело инструмента.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import ClassVar, TypeAlias
from uuid import uuid4

import pytest
from omegaconf import DictConfig, OmegaConf
from test_sandbox_disguise import HostOwner

from boba.config import bind
from boba.runtime.launchers import EnvPaths
from boba.sandbox import SandboxProfile
from boba.sandbox.zygote import (
    ZygoteState,
    ZygoteToolCaller,
)
from boba.stand.sandbox import section_profile
from boba.stand.shell import ShellRun
from boba.stand.zygote import SandboxStand, ZygoteStand
from boba.toolkit.launcher import ChannelOverflowError

needs_bwrap = pytest.mark.failif(shutil.which("bwrap") is None, reason="нет bubblewrap")
needs_userns = pytest.mark.failif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

pytestmark = [needs_bwrap, needs_userns]

USER = "3"
"""Пользователь вызова: его образ и есть единственный, что телу положен."""


class FileHunt:
    """Поиск файлов по всем точкам монтирования тела.

    Один `find / -xdev` не годится: он не заходит за точки монтирования, а
    именно точками бинды и приезжают — так утечка через отдельный бинд
    осталась бы незамеченной.
    """

    @staticmethod
    def script(pattern: str, predicates: str = "") -> str:
        return f"""
for mnt in $(awk '{{print $2}}' /proc/self/mounts); do
  case "$mnt" in
    /proc*|/sys*|/dev*) continue ;;
  esac
  find "$mnt" -xdev -type f -name '{pattern}' {predicates} 2>/dev/null
done
"""


@dataclass(frozen=True)
class SandboxSection:
    """Секция инструментов боевого конфига и её профиль песочницы."""

    name: str
    profile: SandboxProfile

    def __str__(self) -> str:
        return self.name


class Sections:
    """Плагины конфига сервиса — секции [tool.<id>] — и их профили сборкой
    приложения по манифесту."""

    @classmethod
    def of(cls, raw_config: DictConfig) -> list[SandboxSection]:
        sections: list[SandboxSection] = []
        for key in OmegaConf.select(raw_config, "tool", default={}):
            name = str(key)
            profile = section_profile(raw_config, name)
            sections.append(SandboxSection(name=name, profile=profile))

        return sections


@pytest.fixture(scope="module")
def sections(raw_config: DictConfig) -> list[SandboxSection]:
    found = Sections.of(raw_config)
    if not found:
        pytest.fail("в конфиге нет секций с песочницей")

    return found


CallerFactory: TypeAlias = Callable[[SandboxSection], ZygoteToolCaller]


@pytest.fixture
def caller_of(
    zygote_stand: ZygoteStand, sections: list[SandboxSection]
) -> Iterator[CallerFactory]:
    """Фабрика вызывающих: зиготы теста гасятся после него."""
    made: dict[str, ZygoteToolCaller] = {}

    def factory(section: SandboxSection) -> ZygoteToolCaller:
        key = f"exposure-{section.name}-{uuid4().hex[:6]}"
        caller = zygote_stand.caller(
            key,
            section.profile,
            path_vars=lambda: {"user_id": USER, "scope_id": "exposure"},
        )
        made[key] = caller
        return caller

    yield factory

    zygote_stand.stop()


class TestImagesAreHidden:
    """Дефект: обвязка монтирования оставалась телу в секциях без образов.

    Каталог образов приезжает биндом ради монтирования workspace, и в
    профилях, которые его не монтируют, отцеплять было некому: тело секции
    kb или db видело образы всех пользователей и писало в них.
    """

    PATTERN: ClassVar[str] = "*.ext4"
    """Файл образа, видимый телу, — это чужой образ: свой ему дан точкой."""

    PREDICATES: ClassVar[str] = "-size +64M"
    """Отсечка от утилит вроде /sbin/mkfs.ext4: образ пользователя — гигабайт."""

    def test_no_section_sees_an_image_file(
        self, sections: list[SandboxSection], caller_of: CallerFactory
    ) -> None:
        exposed: list[str] = []
        for section in sections:
            caller = caller_of(section)
            outcome = ShellRun.call_text(
                caller, FileHunt.script(self.PATTERN, self.PREDICATES)
            )

            found = outcome.stdout.strip()
            if not found:
                continue

            exposed.append(f"{section.name}: {found[:200]}")

        if exposed:
            raise AssertionError("тело видит файлы образов:\n" + "\n".join(exposed))


class TestMountinfoHasNoHostPaths:
    """Дефект: таблица монтирования тела выдавала раскладку развёртывания.

    Источник fuse-корня был путём rootfs.ext4 на хосте, у биндов resolv.conf,
    hosts, krb5.conf и каталогов весов моделей поле root несло host-путь
    (${env.krb}/krb5.conf, ${env.models}/fastembed). Тело не должно видеть ни
    одного пути [env] развёртывания, ни uid, gid и имени пользователя хоста
    (их печатала tmpfs из userns). Исключение — бинд кода bash-тула, который
    кладёт сам стенд (SandboxStand.with_shell): в бою его нет.
    """

    def test_no_section_sees_a_deployment_path(
        self,
        raw_config: DictConfig,
        sections: list[SandboxSection],
        caller_of: CallerFactory,
    ) -> None:
        markers = self._deployment_paths(raw_config)
        owner = HostOwner().markers()

        exposed: list[str] = []
        for section in sections:
            caller = caller_of(section)
            outcome = ShellRun.call_text(
                caller,
                "cat /proc/self/mountinfo; cat /proc/mounts; cat /proc/self/status",
            )

            for line in outcome.stdout.splitlines():
                if self._stand_overlay(line):
                    continue

                for marker in markers:
                    if marker not in line:
                        continue

                    exposed.append(f"{section.name}: {line[:200]}")

                for pattern in owner:
                    if not pattern.search(line):
                        continue

                    exposed.append(f"{section.name}: {line[:200]}")

        if exposed:
            listed = "\n".join(exposed)
            raise AssertionError(f"тело видит пути развёртывания:\n{listed}")

    @staticmethod
    def _deployment_paths(raw_config: DictConfig) -> list[str]:
        env = bind(raw_config, "env", EnvPaths)
        values = (env.base, env.data, env.sandbox, env.third, env.models, env.krb)

        markers: list[str] = []
        for value in values:
            if value:
                markers.append(value)

        return markers

    @staticmethod
    def _stand_overlay(line: str) -> bool:
        """Строка бинда кода bash-тула стенда: точка монтирования — SHELL_GUEST."""
        fields = line.split()
        if len(fields) < 5:
            return False

        if fields[4] == SandboxStand.SHELL_GUEST:
            return True

        return fields[1] == SandboxStand.SHELL_GUEST


class TestKeytabStaysOutside:
    """Дефект: keytab сервисной учётки был примонтирован в секции db и kb.

    Тело получало вечный ключ принципала вместо тикета с ограниченным
    сроком: любое исполнение кода внутри секции означало кражу доменной
    учётной записи.
    """

    PATTERN: ClassVar[str] = "*.keytab"

    def test_no_section_reads_a_keytab(
        self, sections: list[SandboxSection], caller_of: CallerFactory
    ) -> None:
        exposed: list[str] = []
        for section in sections:
            caller = caller_of(section)
            outcome = ShellRun.call_text(caller, FileHunt.script(self.PATTERN))

            found = outcome.stdout.strip()
            if not found:
                continue

            exposed.append(f"{section.name}: {found[:200]}")

        if exposed:
            raise AssertionError("телу доступен keytab:\n" + "\n".join(exposed))


class TestServiceCcacheStaysOutside:
    """Ни одна секция не видит ccache сервисной учётки.

    Билет к соединению приезжает телу в stdin вызова; общий файл ccache
    с TGT дал бы любому вызову сходить куда угодно от имени приложения.
    """

    PATTERN: ClassVar[str] = "krb5cc_*"

    def test_no_section_sees_a_service_ccache(
        self, sections: list[SandboxSection], caller_of: CallerFactory
    ) -> None:
        exposed: list[str] = []
        for section in sections:
            caller = caller_of(section)
            outcome = ShellRun.call_text(caller, FileHunt.script(self.PATTERN))

            found = outcome.stdout.strip()
            if not found:
                continue

            exposed.append(f"{section.name}: {found[:200]}")

        if exposed:
            raise AssertionError("телу доступен ccache сервиса:\n" + "\n".join(exposed))


class TestBodyHasNoCapabilities:
    """Дефект: bounding set тела оставался от зиготы (CAP_SYS_ADMIN и прочее).

    Наборы процесса обнулял capset, а bounding set переживал его: по нему
    права возвращаются, как только тело получает исполняемый файл с
    capabilities или своё пространство пользователей.
    """

    BOUNDING: ClassVar[str] = "CapBnd:"

    def test_bounding_set_is_empty(
        self, sections: list[SandboxSection], caller_of: CallerFactory
    ) -> None:
        probe = "grep CapBnd /proc/self/status"

        kept: list[str] = []
        for section in sections:
            caller = caller_of(section)
            outcome = ShellRun.call_text(caller, probe)

            line = outcome.stdout.strip()
            mask = line.replace(self.BOUNDING, "").strip()
            if mask and int(mask, 16) == 0:
                continue

            kept.append(f"{section.name}: {line}")

        if kept:
            raise AssertionError("у тела остались capabilities:\n" + "\n".join(kept))


class TestForgedMarkerIsIgnored:
    """Дефект: печать метки хоста в stderr роняла зиготу всей секции.

    Метки `sandbox-chain-lost:` хост искал в канале вызова, куда пишет тело:
    любой инструмент мог объявить корень секции мёртвым и вызвать
    перезапуск — вместе с чужими вызовами, идущими в этот момент.
    """

    FORGERY: ClassVar[str] = (
        'echo "sandbox-chain-lost: forged by the tool body" >&2; '
        'echo "sandbox-mount-error: forged too" >&2; echo done'
    )

    def test_section_survives_a_forged_marker(
        self, sections: list[SandboxSection], caller_of: CallerFactory
    ) -> None:
        section = sections[0]
        caller = caller_of(section)

        forged = ShellRun.call_text(caller, self.FORGERY)
        if "done" not in forged.stdout:
            raise AssertionError(f"вызов не отработал: {forged.stdout!r}")

        state = caller.supervisor.state
        if state is not ZygoteState.READY:
            raise AssertionError(f"секция ушла в {state} из-за подделки")

        after = ShellRun.call_text(caller, "echo alive")
        if "alive" not in after.stdout:
            raise AssertionError("секция не обслуживает вызовы после подделки")


class TestChannelCapStopsTheFlood:
    """Дефект: вывод тела копился в памяти приложения без потолка.

    Лимиты песочницы на приложение не распространяются: тело под лимитом в
    гигабайт выливало в хост столько, сколько успевало, пока хост не падал
    по памяти.
    """

    LIMIT: ClassVar[int] = 1 << 20
    """Потолок теста: мегабайт хватает, чтобы отличить обрыв от прохода."""

    def test_flood_is_cut_by_the_limit(
        self, zygote_stand: ZygoteStand, sections: list[SandboxSection]
    ) -> None:
        section = sections[0]
        profile = self._with_limit(section.profile)

        caller = zygote_stand.caller(
            f"exposure-flood-{uuid4().hex[:6]}",
            profile,
            path_vars=lambda: {"user_id": USER, "scope_id": "exposure"},
        )

        flood = f"head -c {self.LIMIT * 8} /dev/zero | tr '\\0' 'A'"
        with pytest.raises(ChannelOverflowError) as failure:
            ShellRun.call_text(caller, flood)

        if "the limit is" not in str(failure.value):
            raise AssertionError(f"обрыв не по лимиту: {failure.value}")

    @classmethod
    def _with_limit(cls, profile: SandboxProfile) -> SandboxProfile:
        host = profile.host.model_copy(update={"channel_limit_bytes": cls.LIMIT})
        return profile.model_copy(update={"host": host})
