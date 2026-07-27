import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import ClassVar

from boba.hermes.errors import InternalServiceError
from boba.hermes.infra.config import HermesConfig

logger = logging.getLogger(__name__)


class HermesProfileName:
    """username <-> имя профиля hermes.

    Работает с именем, которое вернул провайдер авторизации
    Прямое преобразование — кодек: алфавит профиля у hermes [a-z0-9_-], всё
    остальное уходит в escape -XX (байт utf-8 в hex).
    Обратное берётся из файла .username в каталоге профиля
    там логин лежит ровно в том виде, в каком
    пришёл, включая регистр, который кодек не сохраняет.
    """

    # тот же формат, что проверяет hermes (hermes_cli.profiles._PROFILE_ID_RE)
    _PROFILE_ID_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^[a-z0-9][a-z0-9_-]{0,63}$"
    )
    _PLAIN_RE: ClassVar[re.Pattern[str]] = re.compile(r"[a-z0-9_]")
    _ESCAPE: ClassVar[str] = "-"
    # escape-последовательность: маркер и два hex-разряда байта
    _ESCAPE_DIGITS: ClassVar[int] = 2
    _MAX_LEN: ClassVar[int] = 64
    _USERNAME_FILE: ClassVar[str] = ".username"

    def __init__(self, profiles_root: Path | None = None) -> None:
        self._profiles_root = profiles_root

    def remember(self, profile_dir: Path, username: str) -> None:
        """Сохранить исходный логин рядом с профилем."""
        (profile_dir / self._USERNAME_FILE).write_text(username, encoding="utf-8")

    def encode(self, username: str) -> str:
        """Логин -> имя профиля."""
        source = username.strip().lower()
        name = "".join(
            char
            if self._PLAIN_RE.fullmatch(char)
            else "".join(f"{self._ESCAPE}{byte:02x}" for byte in char.encode())
            for char in source
        )
        if not self._PROFILE_ID_RE.match(name):
            raise InternalServiceError(
                internal_detail=(
                    f"username {username!r} даёт недопустимое имя профиля {name!r}"
                    f" (длина {len(name)})"
                ),
                user_detail="Не удалось определить профиль пользователя",
            )
        return name

    def decode(self, profile: str) -> str:
        """Имя профиля -> логин; из .username, если профиль уже на диске."""
        if self._profiles_root is not None:
            stored = self._profiles_root / profile / self._USERNAME_FILE
            if stored.is_file():
                return stored.read_text(encoding="utf-8").strip()
        return self.decode_name(profile)

    def decode_name(self, profile: str) -> str:
        """Имя профиля -> логин по одному лишь кодеку (в нижнем регистре)."""
        raw = bytearray()
        position = 0
        while position < len(profile):
            char = profile[position]
            if char != self._ESCAPE:
                raw.extend(char.encode())
                position += 1
                continue
            digits = profile[position + 1 : position + 1 + self._ESCAPE_DIGITS]
            if len(digits) != self._ESCAPE_DIGITS:
                raise self._not_decodable(
                    profile, f"обрыв escape на позиции {position}"
                )
            try:
                raw.append(int(digits, 16))
            except ValueError as exc:
                raise self._not_decodable(profile, f"{digits!r} не hex") from exc
            position += 1 + self._ESCAPE_DIGITS

        try:
            return raw.decode()
        except UnicodeDecodeError as exc:
            raise self._not_decodable(
                profile, "escape-байты не образуют utf-8"
            ) from exc

    @staticmethod
    def _not_decodable(profile: str, reason: str) -> InternalServiceError:
        return InternalServiceError(
            internal_detail=f"имя профиля {profile!r} не декодируется: {reason}",
            user_detail="Не удалось определить пользователя профиля",
        )


class HermesProfileProvisioner:
    """Заводит профиль hermes под пользователя на смонтированном HERMES_HOME.

    Профиль hermes — это каталог profiles/<имя> со своим state.db: истории
    пользователей не смешиваются. gateway перебирает каталоги на каждом
    запросе, поэтому новый профиль доступен по /p/<имя>/ сразу.
    """

    def __init__(
        self,
        config: HermesConfig,
        name: HermesProfileName | None = None,
    ) -> None:
        self._data_dir = Path(config.data_dir)
        self._default_profile = config.default_profile
        self._seed_files = tuple(config.profile_seed_files)
        self._name = name or HermesProfileName(self._data_dir / "profiles")

    async def ensure(self, username: str) -> str:
        """Создать профиль пользователя, если его ещё нет; вернуть имя профиля."""
        name = self._name.encode(username)
        return await asyncio.to_thread(self._ensure_dir, name, username)

    async def username(self, profile: str) -> str:
        """Логин владельца профиля."""
        return await asyncio.to_thread(self._name.decode, profile)

    def _ensure_dir(self, name: str, username: str) -> str:
        target = self._profiles_root() / name
        if target.is_dir():
            return name

        # каталог собирается под временным именем и переносится готовым:
        # gateway сканирует profiles/ на каждом запросе и подхватил бы
        # полупустой профиль без config.yaml/.env. точка в начале имени
        # выводит каталог из-под _PROFILE_ID_RE, поэтому он невидим для gateway
        staging = self._profiles_root() / f".staging-{name}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

        for filename in self._seed_files:
            source = self._data_dir / filename
            if not source.is_file():
                raise InternalServiceError(
                    internal_detail=(
                        f"донор {self._default_profile} не содержит "
                        f"{filename}: {source}"
                    ),
                    user_detail="Профиль агента не настроен",
                )
            shutil.copy2(source, staging / filename)

        # логин кладётся рядом с профилем: кодек не хранит регистр, а
        # провайдеры авторизации возвращают имя как есть
        self._name.remember(staging, username)

        try:
            staging.rename(target)
        except OSError:
            # проиграли гонку параллельной сессии того же пользователя
            shutil.rmtree(staging, ignore_errors=True)
            if not target.is_dir():
                raise

        logger.info("создан профиль hermes %s (%s)", name, target)
        return name

    def _profiles_root(self) -> Path:
        root = self._data_dir / "profiles"
        if not self._data_dir.is_dir():
            raise InternalServiceError(
                internal_detail=f"HERMES_HOME не смонтирован: {self._data_dir}",
                user_detail="Бэкенд агента недоступен",
            )
        root.mkdir(exist_ok=True)
        return root
