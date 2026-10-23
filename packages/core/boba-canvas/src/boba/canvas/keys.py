"""Ключи хранилища: каталоги области вызывающего и рабочего каталога, ключ
объекта, свойства элемента.

Ошибки: своих не выпускает; неразборный ключ — ValueError.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from boba.identity.context import PathSegment
from boba.toolkit.failure import ValidationText

__all__ = [
    "DirKey",
    "KeyField",
    "KeySegment",
    "ObjectKey",
    "ScopeDir",
    "WorkspaceKey",
    "WorkspaceMount",
    "WorkspaceRoot",
]


class ScopeDir(StrEnum):
    """Каталоги области: вложения пользователя и спеки диаграмм."""

    UPLOAD = "upload"
    MERMAID = "mermaid"


class WorkspaceMount:
    """Точка рабочего каталога в песочнице процесса, который исполняет
    инструменты; значение приходит из его профиля песочницы.

    Настраивается один раз при загрузке инструментов процесса (сервис,
    тело инструмента); обращение до настройки — ошибка, а не тихий дефолт.
    Клиент сервиса её не настраивает: каталог workspace сервера он берёт из
    описания сервера, и ключ файла несёт его сам (WorkspaceKey).
    """

    _PATH: ClassVar[str] = ""

    @classmethod
    def configure(cls, path: str) -> None:
        if not path:
            msg = "workspace mount is empty: sandbox profile must declare it"
            raise RuntimeError(msg)

        cls._PATH = path

    @classmethod
    def path(cls) -> str:
        if not cls._PATH:
            msg = (
                "workspace mount is not configured: load_tools() sets it from "
                "the sandbox profile"
            )
            raise RuntimeError(msg)

        return cls._PATH


class WorkspaceRoot(BaseModel):
    """Корень workspace того запуска, которым идёт вызов: телу инструмента
    его подаёт хост injected-параметром, и тело настраивает по нему
    WorkspaceMount своего процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str

    @classmethod
    def current(cls) -> Self:
        return cls(path=WorkspaceMount.path())

    def apply(self) -> None:
        WorkspaceMount.configure(self.path)


class KeyField(StrEnum):
    """Поля ключа: ими же именуются сегменты при разборе строки."""

    USER_ID = "user_id"
    SCOPE_ID = "scope_id"
    DIR = "dir"
    NAME = "name"
    WORKSPACE = "workspace"


class KeySegment(BaseModel):
    """Общая валидация сегментов ключа: пустых и путей наружу здесь не бывает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SEPARATOR: ClassVar[str] = PathSegment.SEPARATOR
    SEGMENT: ClassVar[PathSegment] = PathSegment()
    PATHS: ClassVar[frozenset[str]] = frozenset()
    """Поля-пути, а не сегменты: их сегментом не проверяют."""

    @field_validator("*", mode="after")
    @classmethod
    def _segment_is_safe(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value

        if info.field_name in cls.PATHS:
            return value

        return cls.SEGMENT.checked(value, "key segment")


class ObjectKey(KeySegment):
    """Адрес вложения: ключ хранилища и путь того же файла в песочнице."""

    SEGMENTS: ClassVar[int] = 4
    SCOPE_SEGMENTS: ClassVar[int] = 3
    MAX_NAME_BYTES: ClassVar[int] = 255
    """Предел ext4 на длину имени файла."""

    user_id: str
    scope_id: str
    name: str
    dir: ScopeDir = ScopeDir.UPLOAD
    """Каталог области: upload — вложения, mermaid — спеки диаграмм."""

    @classmethod
    def build(
        cls,
        user_id: object,
        scope_id: object,
        name: object,
        element_id: object,
        *,
        scope_dir: ScopeDir = ScopeDir.UPLOAD,
    ) -> Self:
        return cls(
            user_id=str(user_id),
            scope_id=str(scope_id),
            name=cls.safe_name(name, element_id),
            dir=scope_dir,
        )

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Ключ хранилища -> модель; проверки полей живут в самой модели."""
        parts = raw.split(cls.SEPARATOR)
        if len(parts) != cls.SEGMENTS:
            msg = (
                f"invalid object_key {raw!r}: expected {cls.SEGMENTS} segments "
                f"user_id/scope_id/dir/name, got {len(parts)}"
            )
            raise ValueError(msg)

        order = (KeyField.USER_ID, KeyField.SCOPE_ID, KeyField.DIR, KeyField.NAME)
        fields = dict(zip(order, parts, strict=True))

        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            msg = f"invalid object_key {raw!r}: {ValidationText.of(e)}"
            raise ValueError(msg) from e

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_scope()))

    def in_scope(self) -> str:
        """Путь файла внутри образа пользователя."""
        return self.SEPARATOR.join((self.scope_id, self.dir, self.name))

    def in_workspace(self) -> str:
        """Путь файла так, как его видит песочница."""
        return self.SEPARATOR.join((WorkspaceMount.path(), self.in_scope()))

    def dir_key(self) -> DirKey:
        """Каталог, в котором лежит файл."""
        return DirKey(user_id=self.user_id, scope_id=self.scope_id, dir=self.dir)

    @classmethod
    def safe_name(cls, name: object, element_id: object) -> str:
        base = os.path.basename(str(name)).strip()
        kept: list[str] = []
        for char in base:
            if char in ("/", "\\"):
                continue
            if char.isprintable():
                kept.append(char)
        cleaned = "".join(kept).strip()
        if cleaned in ("", ".", ".."):
            return str(element_id)
        return cls._truncate(cleaned)

    @classmethod
    def _truncate(cls, name: str) -> str:
        encoded = name.encode("utf-8")
        if len(encoded) <= cls.MAX_NAME_BYTES:
            return name
        stem, dot, suffix = name.rpartition(".")
        tail = ""
        if dot:
            tail = "." + suffix[: cls.MAX_NAME_BYTES // 4]
        room = cls.MAX_NAME_BYTES - len(tail.encode("utf-8"))
        head = stem
        if not dot:
            head = name
        while len(head.encode("utf-8")) > room:
            head = head[:-1]
        return head + tail


class WorkspaceKey(ObjectKey):
    """Ключ файла области вместе с каталогом workspace, под которым его видит
    песочница сервера файлов области: путь показа (in_workspace) — под этим
    каталогом, а не под точкой процесса (WorkspaceMount).

    Строит его область канваса (CanvasScope) по пути в workspace либо по
    имени файла области; ключ хранилища (render) тот же, что у ObjectKey.
    """

    PATHS: ClassVar[frozenset[str]] = frozenset({KeyField.WORKSPACE.value})

    workspace: str = Field(min_length=1)

    @classmethod
    def located(
        cls, workspace: str, user_id: object, scope_id: object, path: str
    ) -> Self:
        """Ключ по пути в workspace области; вне каталогов области — ValueError."""
        rel = path.strip()

        mount = cls.SEPARATOR.join((workspace, ""))
        if rel.startswith(mount):
            rel = rel[len(mount) :]

        parts = rel.split(cls.SEPARATOR)
        if len(parts) != cls.SCOPE_SEGMENTS:
            raise ValueError(cls._outside(workspace, scope_id, path))

        order = (KeyField.SCOPE_ID, KeyField.DIR, KeyField.NAME)
        fields: dict[str, str] = dict(zip(order, parts, strict=True))
        fields[KeyField.USER_ID] = str(user_id)
        fields[KeyField.WORKSPACE] = workspace

        try:
            key = cls.model_validate(fields)
        except ValidationError as e:
            raise ValueError(cls._outside(workspace, scope_id, path)) from e

        if key.scope_id != str(scope_id):
            raise ValueError(cls._outside(workspace, scope_id, path))

        return key

    @classmethod
    def _outside(cls, workspace: str, scope_id: object, path: str) -> str:
        """Текст ошибки называет ожидаемый путь."""
        name = os.path.basename(path.strip())
        if not name:
            name = "<file name>"

        dirs = "|".join(sorted(ScopeDir))
        expected = cls.SEPARATOR.join((workspace, str(scope_id), f"{{{dirs}}}", name))
        return (
            f"file is outside the scope attachments dir: {path!r}; "
            f"expected {expected!r}"
        )

    def in_workspace(self) -> str:
        return self.SEPARATOR.join((self.workspace, self.in_scope()))


class DirKey(KeySegment):
    """Адрес каталога области: префикс в хранилище и путь того же каталога в
    песочнице."""

    SEGMENTS: ClassVar[int] = 3

    user_id: str
    scope_id: str
    dir: ScopeDir

    @classmethod
    def of(cls, user_id: object, scope_id: object, scope_dir: ScopeDir) -> Self:
        return cls(user_id=str(user_id), scope_id=str(scope_id), dir=scope_dir)

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Префикс каталога -> модель; проверки полей живут в самой модели."""
        parts = raw.split(cls.SEPARATOR)
        if len(parts) != cls.SEGMENTS:
            msg = (
                f"invalid dir key {raw!r}: expected {cls.SEGMENTS} segments "
                f"user_id/scope_id/dir, got {len(parts)}"
            )
            raise ValueError(msg)

        order = (KeyField.USER_ID, KeyField.SCOPE_ID, KeyField.DIR)
        fields = dict(zip(order, parts, strict=True))

        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            msg = f"invalid dir key {raw!r}: {ValidationText.of(e)}"
            raise ValueError(msg) from e

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_scope()))

    def in_scope(self) -> str:
        """Путь каталога внутри образа пользователя."""
        return self.SEPARATOR.join((self.scope_id, self.dir))

    def in_workspace(self) -> str:
        """Путь каталога так, как его видит песочница."""
        return self.SEPARATOR.join((WorkspaceMount.path(), self.in_scope()))

    def file(self, name: str) -> ObjectKey:
        """Ключ файла в этом каталоге; имя приходит из листинга, не от LLM."""
        return ObjectKey(
            user_id=self.user_id,
            scope_id=self.scope_id,
            name=name,
            dir=self.dir,
        )
