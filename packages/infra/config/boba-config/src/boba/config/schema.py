"""Defaults типизированных секций до раскрытия ссылок OmegaConf."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from omegaconf import DictConfig, ListConfig, OmegaConf
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo

__all__ = ["ConfigSchema"]


class ConfigSchema:
    """Модель приложения и одноимённые секции его полей.

    Заполняет только пропущенные значения. Проверка типов и межполевых
    ограничений остаётся в bind после раскрытия ссылок.
    """

    def __init__(
        self,
        model: type[BaseModel],
        section: str = "app",
        *,
        values: Sequence[str] = (),
    ) -> None:
        self._model_type = model
        self._section = section
        self._values = frozenset(values)

    def apply(self, tree: DictConfig) -> None:
        sections = self._sections(tree)
        # Сначала literal-секции: ссылки на их defaults уже доступны
        # при последующем обходе секций, подключённых ссылками.
        for follow in (False, True):
            visited: set[tuple[int, type[BaseModel]]] = set()
            for key, annotation in sections.items():
                self._field(annotation, tree, key, follow, visited)

    def _sections(self, tree: DictConfig) -> dict[str, Any]:
        if self._section not in tree:
            tree[self._section] = {}
        app = tree[self._section]
        if not isinstance(app, DictConfig):
            return {}

        sections: dict[str, Any] = {self._section: self._model_type}
        for name, field in self._model_type.model_fields.items():
            key = field.alias or name
            if key not in tree and not field.is_required():
                tree[key] = self._default(field)
            if key not in tree:
                continue
            annotation = field.annotation
            reference = f"${{{key}}}"
            if key in self._values and isinstance(tree[key], DictConfig):
                annotation = dict[str, get_args(annotation)[0]]
                reference = f"${{oc.dict.values:{key}}}"
            sections[key] = annotation
            if key not in app:
                app[key] = reference

        return sections

    def _field(
        self,
        annotation: Any,
        parent: DictConfig | ListConfig,
        key: str | int,
        follow: bool,
        visited: set[tuple[int, type[BaseModel]]],
    ) -> None:
        annotation = self._unwrap(annotation)
        if not self._has_models(annotation):
            return
        if not follow and OmegaConf.is_interpolation(parent, key):
            return

        node = parent[key] if isinstance(parent, DictConfig) else parent[int(key)]
        self._node(annotation, node, follow, visited)

    def _node(
        self,
        annotation: Any,
        node: Any,
        follow: bool,
        visited: set[tuple[int, type[BaseModel]]],
    ) -> None:
        annotation = self._unwrap(annotation)
        model = self._model(annotation, node)
        if model is not None and model.__pydantic_root_model__:
            self._node(model.model_fields["root"].annotation, node, follow, visited)
            return
        if model is not None and isinstance(node, DictConfig):
            self._defaults(model, node, follow, visited)
            return

        args = get_args(annotation)
        if not args:
            return
        if isinstance(node, DictConfig) and get_origin(annotation) in (dict, Mapping):
            for name in node:
                self._field(args[-1], node, str(name), follow, visited)
        elif isinstance(node, ListConfig):
            for index in range(len(node)):
                self._field(args[0], node, index, follow, visited)

    def _defaults(
        self,
        model: type[BaseModel],
        node: DictConfig,
        follow: bool,
        visited: set[tuple[int, type[BaseModel]]],
    ) -> None:
        identity = (id(node), model)
        if identity in visited:
            return
        visited.add(identity)
        data = OmegaConf.to_container(node, resolve=False)
        assert isinstance(data, dict)  # noqa: S101 — DictConfig всегда таблица
        constructed = model.model_construct(
            **{str(key): value for key, value in data.items()}
        )
        supplied = constructed.model_fields_set
        for name, field in model.model_fields.items():
            key = field.alias or name
            if key not in node and name in node:
                key = name
            if name not in supplied:
                if field.is_required():
                    continue
                node[key] = TypeAdapter(field.annotation).dump_python(
                    getattr(constructed, name), mode="json", by_alias=True
                )
            if key in node:
                self._field(field.annotation, node, key, follow, visited)

    @staticmethod
    def _default(field: FieldInfo) -> Any:
        default = field.get_default(call_default_factory=True)
        return TypeAdapter(field.annotation).dump_python(
            default, mode="json", by_alias=True
        )

    @classmethod
    def _has_models(cls, annotation: Any) -> bool:
        annotation = cls._unwrap(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return True
        return any(cls._has_models(arg) for arg in get_args(annotation))

    @staticmethod
    def _unwrap(annotation: Any) -> Any:
        if get_origin(annotation) is Annotated:
            return get_args(annotation)[0]
        return annotation

    @classmethod
    def _model(cls, annotation: Any, node: Any) -> type[BaseModel] | None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        if get_origin(annotation) not in (Union, UnionType):
            return None
        variants = [cls._unwrap(arg) for arg in get_args(annotation)]
        models = [
            arg
            for arg in variants
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ]
        if len(models) == 1:
            return models[0]
        if not isinstance(node, DictConfig):
            return None
        matches = [model for model in models if cls._matches(model, node)]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _matches(model: type[BaseModel], node: DictConfig) -> bool:
        literals = {
            field.alias or name: get_args(field.annotation)
            for name, field in model.model_fields.items()
            if get_origin(field.annotation) is Literal
        }
        if not literals:
            required = {
                field.alias or name
                for name, field in model.model_fields.items()
                if field.is_required()
            }
            return bool(required) and required.issubset(node.keys())
        return all(
            key in node and node[key] in values for key, values in literals.items()
        )
