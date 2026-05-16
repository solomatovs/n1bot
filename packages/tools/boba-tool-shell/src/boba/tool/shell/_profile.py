"""SandboxProfile: декларативное описание песочницы под bubblewrap.

Профиль не зависит от рантайма (bwrap, subprocess). Это чистый DTO,
который `_sandbox.py` превращает в argv, а `_runner.py` — в дочерний
процесс. Хранится в `ShellPluginConfig.profiles`.

Поля по умолчанию выбраны под кейс «LLM выполняет bash в workspace
без сети»: read-only системные директории + RW workspace + tmpfs /tmp,
network unshared, 30s timeout, 256 KiB на каждый поток.

Env-переменные в песочнице приходят **только** через `env_set` — этот
словарь пишется явно в конфиге. Host-environment (включая API-ключи и
прочие секреты процесса агента) не наследуется намеренно. Минимальный
PATH (например, `/usr/bin:/bin`) тоже нужно прописать в `env_set`,
иначе bash не найдёт стандартные утилиты.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["SandboxProfile"]


def _canonicalize_paths(value: object) -> tuple[str, ...]:
    """Резолв каждого пути в абсолютный/каноничный (strict=False).

    `strict=False` — не падать на несуществующих путях: они будут
    отсеяны `--ro-bind-try`/`--bind-try` на стороне bwrap. Цель —
    только защита от symlink-обмана и относительных путей в конфиге.
    """
    if not isinstance(value, (list, tuple)):
        msg = f"expected list/tuple of paths, got {type(value).__name__}"
        raise TypeError(msg)
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, (str, Path)):
            msg = f"path must be str/Path, got {type(raw).__name__}"
            raise TypeError(msg)
        resolved = Path(raw).expanduser().resolve(strict=False)
        out.append(str(resolved))
    return tuple(out)


_DEFAULT_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/etc/resolv.conf",
)


class SandboxProfile(BaseModel):
    """
    Параметры одной песочницы.

    LLM выбирает профиль по имени из `ShellPluginConfig.profiles`
    сами поля LLM править не может
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ro_binds: tuple[str, ...] = Field(
        default=_DEFAULT_RO_BINDS,
        description=(
            "Host-пути, монтируемые read-only внутрь песочницы. "
            "Несуществующие пути пропускаются на build-time."
        ),
    )
    rw_binds: tuple[str, ...] = Field(
        default=(),
        description=(
            "Host-пути, монтируемые read-write. Workspace-root "
            "добавляется автоматически на build-time."
        ),
    )
    tmpfs: tuple[str, ...] = Field(
        default=("/tmp",),  # noqa: S108 — tmpfs внутри песочницы, не host
        description="Mountpoints под tmpfs (in-memory).",
    )
    network: bool = Field(
        default=False,
        description=(
            "Если False — `--unshare-net` (нет сети). True — сеть хоста."
        ),
    )
    env_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Env-переменные внутри песочницы. Задаются явно — host-env "
            "не наследуется (защита от утечки секретов агента). Чтобы "
            "bash нашёл стандартные утилиты, обычно нужен 'PATH'."
        ),
    )
    timeout_sec: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Жёсткий таймаут выполнения процесса.",
    )
    max_output_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        description=(
            "Лимит размера stdout И stderr по отдельности. Превышение → "
            "обрезка и `truncated=True` в результате."
        ),
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Рабочая директория внутри песочницы. None = workspace-root."
        ),
    )

    @field_validator("ro_binds", "rw_binds", "tmpfs", mode="before")
    @classmethod
    def _canonicalize(cls, v: object) -> tuple[str, ...]:
        return _canonicalize_paths(v)
