"""Промпты описания: что владелец поверхности говорит модели о её объектах.

Материал объекта даёт объявление аспекта класса describer_input, а роль модели и
правила разбора этого материала знает владелец поверхности: структура таблицы и текст
статьи объясняются по-разному. Правило живёт строкой в {schema}.surface_prompt на пару
«поверхность, аспект»; описатель читает реестр (IxRegistry) при старте и применяет,
не зная ни происхождений, ни поверхностей.

В шаблоне запроса стоит подстановка {input}: вместо неё подставляется материал объекта.
Пара без строки не описывается — общего промпта на все поверхности нет намеренно.

Ошибки:
SurfacePromptError — строка реестра не годится: пустой системный промпт, шаблон без
    подстановки материала или запрошена пара, которой в реестре нет.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SurfacePrompt", "SurfacePromptError"]


class SurfacePromptError(Exception):
    """Промпт поверхности не годится для описания."""


@dataclass(frozen=True, kw_only=True)
class SurfacePrompt:
    """Одна строка {schema}.surface_prompt: как объяснять модели объекты этой пары."""

    surface: str
    aspect: str
    system_prompt: str
    user_template: str
    owner: str

    def key(self) -> tuple[str, str]:
        return (self.surface, self.aspect)

    def user(self, material: str) -> str:
        """Запрос к модели: материал объекта вместо подстановки."""
        return self.user_template.replace("{input}", material)

    def check(self) -> None:
        """Строка годится: есть роль модели и место под материал."""
        where = f"prompt for {self.surface}/{self.aspect} (owner {self.owner})"

        if not self.system_prompt.strip():
            raise SurfacePromptError(
                f"{where}: expected a system prompt, got an empty string"
            )

        if "{input}" not in self.user_template:
            raise SurfacePromptError(
                f"{where}: expected the placeholder {'{input}'} in the user template, "
                f"got {self.user_template[:80]!r}"
            )
