"""Провайдеры LLM: конфиг провайдера по kind, реестр установленных пакетов
и ресурсы процесса, отдающие модели по конфигу использования.

Провайдер — «где и как»: endpoint с транспортом либо каталог локальной
модели; его секция `[llm.<имя>]` разбирается моделью пакета-реализации, а
пакет находится по kind через entry point группы boba.llm — так же, как типы
соединений через boba.connections. Использование — «что»: имя модели и
сэмплинг (ChatModelConfig) либо имя, размерность и батч (EmbeddingModelConfig)
со ссылкой на провайдера. LlmProviders держит по одному LlmBackend на
провайдера (транспорт, загруженные веса) и раздаёт модели.

Ошибки:
LlmProvidersError — entry point не отдал манифест, kind не установлен или
    провайдер не поддерживает запрошенную способность.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SerializeAsAny

from boba.llm.chat import ChatModel
from boba.llm.embedding import EmbeddingModel

__all__ = [
    "ChatModelConfig",
    "EmbeddingModelConfig",
    "LlmBackend",
    "LlmProvider",
    "LlmProviderManifest",
    "LlmProviderTypes",
    "LlmProviders",
    "LlmProvidersError",
    "ProviderRef",
]


class LlmProvidersError(ValueError):
    """Реестр провайдеров не собрался или провайдер не даёт такой модели.

    Наследует ValueError: из валидатора конфига pydantic заворачивает её в
    ValidationError, и ошибка секции читается как ошибка конфига.
    """


class LlmProvider(BaseModel):
    """Секция `[llm.<имя>]`: общая часть — kind; остальное объявляет реализация.

    Поле конфига типа ProviderRef разбирает таблицу моделью пакета, который
    установлен под этим kind: ядро конкретных провайдеров не перечисляет.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str = Field(description="Вид провайдера: имя entry point группы boba.llm.")

    @classmethod
    def resolve(cls, value: object) -> object:
        """Таблица конфига -> модель провайдера установленного пакета.

        Готовый экземпляр и не-таблица уходят обычной валидации: первый
        принимается как есть, вторая даёт понятную ошибку pydantic.
        """
        if not isinstance(value, Mapping):
            return value

        raw = dict(value)
        kind = raw.get("kind")
        if not isinstance(kind, str):
            return value

        return LlmProviderTypes.installed().config_of(kind).model_validate(raw)


ProviderRef = Annotated[
    SerializeAsAny[LlmProvider], BeforeValidator(LlmProvider.resolve)
]
"""Поле-ссылка на провайдера: `provider = "${llm.<имя>}"` в конфиге.

SerializeAsAny: дамп поля идёт по модели пакета, а не по базе с одним kind,
и конфиг переживает дорогу в песочницу json'ом."""


class ChatModelConfig(BaseModel):
    """Использование чат-модели: провайдер, имя модели и сэмплинг."""

    model_config = ConfigDict(extra="ignore")

    provider: ProviderRef = Field(description="Провайдер ссылкой `${llm.<имя>}`.")

    model: str = Field(description="Имя модели у провайдера.")

    sampling: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Параметры запроса к провайдеру как есть: имена и значения уходят "
            "в тело запроса без проверок и переименований."
        ),
    )


class EmbeddingModelConfig(BaseModel):
    """Использование эмбеддинг-модели: провайдер, имя, размерность и батч.

    model и dim обязаны совпадать между записью и поиском одной коллекции,
    иначе размерности разъедутся молча.
    """

    model_config = ConfigDict(extra="ignore")

    provider: ProviderRef = Field(description="Провайдер ссылкой `${llm.<имя>}`.")

    model: str = Field(description="Имя эмбеддинг-модели у провайдера.")

    dim: int = Field(
        gt=0,
        description=(
            "Размерность вектора модели. Задаётся явно, чтобы процесс не грузил "
            "модель ради одного числа; расхождение ловится на первом эмбеддинге."
        ),
    )

    batch_size: int = Field(
        gt=0,
        description=(
            "Сколько текстов уходит в модель за один прогон. Локально активации "
            "ONNX растут линейно по батчу; удалённо это размер input запроса."
        ),
    )

    progress_every: int = Field(
        gt=0,
        description="Через сколько посчитанных векторов писать строку прогресса.",
    )


class LlmBackend(ABC):
    """Ресурсы одного провайдера на процесс: транспорт к endpoint'у либо
    загруженная модель. Собирается из секции провайдера, раздаёт модели по
    конфигу использования; провайдер без такой способности отказывает
    LlmProvidersError.
    """

    @abstractmethod
    def __init__(self, provider: LlmProvider) -> None: ...

    @abstractmethod
    def chat(self, cfg: ChatModelConfig) -> ChatModel: ...

    @abstractmethod
    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel: ...

    @abstractmethod
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class LlmProviderManifest:
    """Пакет-реализация описывает провайдера: kind, модель секции и бэкенд.

    backend получает разобранную секцию своей моделью (ядро проверяет kind,
    реализация — тип) и владеет ресурсами провайдера до aclose().
    """

    kind: str
    config: type[LlmProvider]
    backend: type[LlmBackend]


class LlmProviderTypes:
    """Реестр установленных провайдеров: kind -> манифест.

    installed() держит один разбор entry points на процесс: его зовёт
    валидатор конфига, у которого нет своего экземпляра реестра.
    """

    GROUP: ClassVar[str] = "boba.llm"

    _installed: ClassVar[LlmProviderTypes | None] = None

    def __init__(self, table: Mapping[str, LlmProviderManifest]) -> None:
        self._table = dict(table)

    @classmethod
    def discover(cls) -> LlmProviderTypes:
        """Реестр из entry points установленных пакетов."""
        table: dict[str, LlmProviderManifest] = {}
        for entry in entry_points(group=cls.GROUP):
            manifest = entry.load()
            if not isinstance(manifest, LlmProviderManifest):
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected an LlmProviderManifest, "
                    f"got {type(manifest).__name__}"
                )
                raise LlmProvidersError(msg)

            if manifest.kind != entry.name:
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}) declares kind {manifest.kind!r}: "
                    "the entry point name must equal the kind"
                )
                raise LlmProvidersError(msg)

            table[manifest.kind] = manifest

        return cls(table)

    @classmethod
    def installed(cls) -> LlmProviderTypes:
        if cls._installed is None:
            cls._installed = cls.discover()

        return cls._installed

    def kinds(self) -> Sequence[str]:
        return tuple(sorted(self._table))

    def manifest_of(self, kind: str) -> LlmProviderManifest:
        found = self._table.get(kind)
        if found is None:
            msg = (
                f"llm provider kind {kind!r} is not installed, "
                f"installed kinds: {list(self.kinds())}"
            )
            raise LlmProvidersError(msg)

        return found

    def config_of(self, kind: str) -> type[LlmProvider]:
        return self.manifest_of(kind).config


class LlmProviders:
    """Модели процесса: по одному бэкенду на провайдера, живут до aclose().

    Два использования с равными секциями провайдера делят бэкенд: один
    транспорт к endpoint'у, одна загруженная локальная модель.
    """

    def __init__(self, types: LlmProviderTypes) -> None:
        self._types = types
        self._backends: list[tuple[LlmProvider, LlmBackend]] = []

    def chat(self, cfg: ChatModelConfig) -> ChatModel:
        return self._backend(cfg.provider).chat(cfg)

    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel:
        return self._backend(cfg.provider).embedding(cfg)

    async def aclose(self) -> None:
        for _, backend in self._backends:
            await backend.aclose()

        self._backends.clear()

    def _backend(self, provider: LlmProvider) -> LlmBackend:
        for known, backend in self._backends:
            if known == provider:
                return backend

        manifest = self._types.manifest_of(provider.kind)
        backend = manifest.backend(provider)
        self._backends.append((provider, backend))

        return backend
