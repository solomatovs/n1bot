"""Регистрация моделей конфигурации и типизированных фабрик компонентов.

Ошибки:
ComponentConfigError — неверная регистрация, неизвестный вид или конфигурация.
Ошибки запуска определяет фабрика и контракт её слоя.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterable, Mapping
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

ConfigT = TypeVar("ConfigT", bound=BaseModel)
ContextT = TypeVar("ContextT")
ResultT = TypeVar("ResultT")
ContextT_contra = TypeVar("ContextT_contra", contravariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)


class ComponentConfigError(ValueError):
    """Регистрация или конфиг компонента не соответствует его контракту."""


class ComponentBuilder(Protocol[ContextT_contra, ResultT_co]):
    """Модель конфигурации и операция сборки, которые предоставляет реализация."""

    @property
    @abstractmethod
    def model(self) -> type[BaseModel]: ...

    @abstractmethod
    def build(self, config: BaseModel, context: ContextT_contra) -> ResultT_co: ...


class ComponentFactory(
    ComponentBuilder[ContextT, ResultT], Generic[ConfigT, ContextT, ResultT]
):
    """Связывает модель реализации и её фабрику без потери типа аргументов."""

    def __init__(
        self, model: type[ConfigT], create: Callable[[ConfigT, ContextT], ResultT]
    ) -> None:
        self._model = model
        self._create = create

    @property
    def model(self) -> type[ConfigT]:
        return self._model

    def build(self, config: BaseModel, context: ContextT) -> ResultT:
        parsed = self._model.model_validate(config)
        return self._create(parsed, context)


class Components(Generic[ConfigT, ContextT, ResultT]):
    """Реестр моделей и фабрик, явно переданных приложением."""

    def __init__(
        self,
        base: type[ConfigT],
        discriminator: str,
        factories: Iterable[ComponentBuilder[ContextT, ResultT]],
    ) -> None:
        self._base = base
        self._discriminator = discriminator
        self._factories: dict[str, ComponentBuilder[ContextT, ResultT]] = {}
        for factory in factories:
            if not issubclass(factory.model, base):
                raise ComponentConfigError(
                    f"{factory.model.__name__} must extend {base.__name__}"
                )

            field = factory.model.model_fields.get(discriminator)
            if field is None:
                raise ComponentConfigError(
                    f"{factory.model.__name__} has no {discriminator!r} field"
                )

            kind = field.default
            if not isinstance(kind, str):
                raise ComponentConfigError(
                    f"{factory.model.__name__}.{discriminator} needs a string default"
                )

            if not kind:
                raise ComponentConfigError(
                    f"{factory.model.__name__}.{discriminator} must not be empty"
                )

            if kind in self._factories:
                raise ComponentConfigError(
                    f"{base.__name__}: duplicate registration {kind!r}"
                )

            self._factories[kind] = factory

    def _factory(
        self, raw: Mapping[str, object]
    ) -> ComponentBuilder[ContextT, ResultT]:
        kind = raw.get(self._discriminator)
        if not isinstance(kind, str):
            raise ComponentConfigError(
                f"{self._base.__name__}: expected string {self._discriminator!r}"
            )

        factory = self._factories.get(kind)
        if factory is None:
            raise ComponentConfigError(
                f"{self._base.__name__}: {kind!r} is not registered; "
                f"available: {sorted(self._factories)}"
            )

        return factory

    def model_for(self, raw: Mapping[str, object]) -> type[BaseModel]:
        """Выбирает модель без разбора значений: для defaults и JSON Schema."""
        return self._factory(raw).model

    def parse(self, raw: object) -> ConfigT:
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(mode="python")

        if not isinstance(raw, Mapping):
            raise ComponentConfigError(
                f"{self._base.__name__} expects a table, got {type(raw).__name__}"
            )

        factory = self._factory(raw)
        try:
            config = factory.model.model_validate(raw)
        except ValidationError as exc:
            # Конфиг может содержать секреты; значения входа не включаем в ошибку.
            errors = exc.errors(
                include_input=False, include_url=False, include_context=False
            )
            raise ComponentConfigError(f"{factory.model.__name__}: {errors}") from exc

        if not isinstance(config, self._base):
            raise ComponentConfigError(
                f"{factory.model.__name__} returned {type(config).__name__}"
            )

        return config

    def build(self, config: ConfigT, context: ContextT) -> ResultT:
        raw = config.model_dump(mode="python")
        factory = self._factory(raw)
        if type(config) is not factory.model:
            raise ComponentConfigError(
                f"{self._base.__name__}: expected {factory.model.__name__}, "
                f"got {type(config).__name__}; parse the configuration first"
            )

        return factory.build(config, context)
