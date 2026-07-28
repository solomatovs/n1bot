import logging
import re
from typing import ClassVar, TypeVar
from uuid import UUID

from psycopg import AsyncCursor, sql
from psycopg_pool import AsyncConnectionPool

from boba.hermes.chat.data.models import HermesProfile

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class HermesProfileEncoder:
    """
    Переводит login в имя профиля hermes
    hermes profile требует отчистки от неподдерживаемых символов
    """

    # формат hermes (hermes_cli.profiles._PROFILE_ID_RE): ^[a-z0-9][a-z0-9_-]{0,63}$
    _PLAIN_RE: ClassVar[re.Pattern[str]] = re.compile(r"[a-z0-9_]")
    _LEADING_RE: ClassVar[re.Pattern[str]] = re.compile(r"[a-z0-9]")
    _ESCAPE: ClassVar[str] = "-"
    # чем начинается имя, если после очистки первый символ не подходит
    _LEADING: ClassVar[str] = "u"
    _MAX_LEN: ClassVar[int] = 64

    def encode(self, username: str) -> str:
        """Логин -> имя профиля, пригодное для hermes."""
        source = self._normalized(username)
        escaped = self._escaped(source)
        started = self._started(escaped)
        return started[: self._MAX_LEN]

    @staticmethod
    def _normalized(username: str) -> str:
        """Обрезка пробелов и нижний регистр: другого hermes не принимает."""
        return username.strip().lower()

    @classmethod
    def _escaped(cls, source: str) -> str:
        """Символы алфавита оставляем, остальные заменяем escape-последовательностью."""
        parts: list[str] = []
        for char in source:
            if cls._PLAIN_RE.fullmatch(char):
                parts.append(char)
            else:
                parts.append(cls._escape(char))

        return "".join(parts)

    @classmethod
    def _escape(cls, char: str) -> str:
        """Символ вне алфавита -> по паре hex-разрядов на каждый байт utf-8."""
        parts: list[str] = []
        for byte in char.encode():
            parts.append(f"{cls._ESCAPE}{byte:02x}")

        return "".join(parts)

    @classmethod
    def _started(cls, escaped: str) -> str:
        """hermes требует первым символом букву или цифру, а escape даёт дефис."""
        if escaped and cls._LEADING_RE.fullmatch(escaped[0]):
            return escaped

        return cls._LEADING + escaped


class HermesProfileRepository:
    """Связка пользователя chainlit с профилем hermes.

    Имя профиля выбирается один раз при первом входе и дальше не пересчитывается:
    логин может смениться, а история пользователя должна остаться на месте.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        schema: str,
    ) -> None:
        self._pool = pool
        self._schema = schema
        self._name = HermesProfileEncoder()

    async def get(self, user_id: UUID) -> str | None:
        """
        Профиль пользователя, либо None если связки нет
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                sql.SQL("SELECT profile FROM {table} WHERE user_id = %s").format(
                    table=HermesProfile.get_table_name(self._schema)
                ),
                (user_id,),
            )

            return await self._single(cur, str, f"hermes profile ({user_id})")

    async def user_of(self, profile: str) -> UUID | None:
        """
        Владелец профиля
        обратное направление той же связки
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                sql.SQL("SELECT user_id FROM {table} WHERE profile = %s").format(
                    table=HermesProfile.get_table_name(self._schema)
                ),
                (profile,),
            )

            return await self._single(cur, UUID, f"hermes profile owner ({profile})")

    async def ensure(self, user_id: UUID, username: str) -> str:
        """Профиль пользователя, при первом обращении — закрепить имя за ним."""
        if profile := await self.get(user_id):
            return profile

        name = self._name.encode(username)
        if await self._insert_or_nothing(user_id, name):
            logger.info("hermes profile %s bound to %s", name, username)
            return name

        # параллельная сессия того же пользователя успела закрепить имя раньше
        if profile := await self.get(user_id):
            return profile

        raise RuntimeError(
            f"profile name {name!r} for {username!r} is taken by another user"
        )

    async def _insert_or_nothing(self, user_id: UUID, profile: str) -> bool:
        """
        Добавить профиль.
        Если профиль уже добавлен для этого user_id, то возвращаем False
        """
        row = HermesProfile(user_id=user_id, profile=profile)

        async with self._pool.connection() as conn:
            cur = await conn.execute(
                sql.SQL(
                    "insert into {table} ({columns}) values ({values}) "
                    "ON conflict DO NOTHING RETURNING profile"
                ).format(
                    table=HermesProfile.get_table_name(self._schema),
                    columns=HermesProfile.all_columns(),
                    values=HermesProfile.all_placeholders(),
                ),
                row.all_params(),
            )

            # пустой, значит имя занято
            claimed = await self._single(cur, str, f"claim hermes profile ({profile})")

            return claimed is not None

    @staticmethod
    async def _single(cur: AsyncCursor, expected: type[_T], query: str) -> _T | None:
        """Единственное значение одной колонки; None, если строк нет.

        Запрос выбирает одну колонку по уникальному ключу, поэтому всё, кроме
        нуля или одной строки, означает разъехавшуюся схему, а не пустой ответ.
        """
        if cur.rowcount == 0:
            return None

        if cur.rowcount > 1:
            raise RuntimeError(f"query {query} returned {cur.rowcount} rows")

        if not cur.description:
            raise RuntimeError(f"query {query} returned no columns")

        if len(cur.description) > 1:
            raise RuntimeError(
                f"query {query} returned {len(cur.description)} columns"
            )

        row = await cur.fetchone()
        if row is None:
            return None

        column = row[0]
        if isinstance(column, expected):
            return column

        raise RuntimeError(
            f"query {query} returned {type(column).__name__}, "
            f"expected {expected.__name__}"
        )
