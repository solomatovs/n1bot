"""Проверка внешнего вида страницы: токены стилей, вычисленный CSS, геометрия.

Ожидания берутся из tokens.css сборки: тест сверяет DOM с теми же токенами,
что и стили, а не с числами, переписанными руками.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from playwright.sync_api import Locator, Page

from boba.stand.ui.stand import REPO_ROOT

__all__ = ["Box", "Css", "Tokens", "close", "fluid", "no_horizontal_scroll"]


class Tokens:
    """Токены tokens.css по темам; значения цветов — как их отдаёт getComputedStyle."""

    PATH: ClassVar[Path] = (
        REPO_ROOT / "packages/agents/boba-studio/web/workflow/src/styles/tokens.css"
    )
    BLOCK: ClassVar[re.Pattern[str]] = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
    TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"--([a-z0-9-]+):\s*([^;]+);")
    LIGHT: ClassVar[str] = 'data-theme="light"'

    def __init__(self, dark: dict[str, str], light: dict[str, str]) -> None:
        self._dark = dark
        self._light = light

    @classmethod
    def load(cls) -> Tokens:
        dark: dict[str, str] = {}
        light: dict[str, str] = {}
        for selector, body in cls.BLOCK.findall(cls.PATH.read_text(encoding="utf-8")):
            target = light if cls.LIGHT in selector else dark
            for name, value in cls.TOKEN.findall(body):
                target[name] = value.strip()

        return cls(dark, light)

    REF: ClassVar[re.Pattern[str]] = re.compile(r"^var\(--([a-z0-9-]+)\)$")

    def raw(self, name: str, theme: str = "dark") -> str:
        """Значение токена; ссылка var(--x) раскрывается до самого значения."""
        values = self._dark
        if theme == "light":
            values = {**self._dark, **self._light}

        value = values.get(name)
        if value is None:
            raise KeyError(f"no token --{name} in tokens.css")

        reference = self.REF.match(value)
        if reference is None:
            return value

        return self.raw(reference.group(1), theme)

    def rgb(self, name: str, theme: str = "dark") -> str:
        """Цвет токена в записи getComputedStyle: rgb(r, g, b)."""
        return hex_to_rgb(self.raw(name, theme))

    def px(self, name: str) -> float:
        return float(self.raw(name).removesuffix("px"))


def hex_to_rgb(value: str) -> str:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a #rrggbb color: {value}")

    r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgb({r}, {g}, {b})"


@dataclass(frozen=True)
class Box:
    """Прямоугольник элемента в CSS-пикселях."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def contains(self, other: Box, slack: float = 1.0) -> bool:
        return (
            other.x >= self.x - slack
            and other.y >= self.y - slack
            and other.right <= self.right + slack
            and other.bottom <= self.bottom + slack
        )

    def contains_y(self, other: Box, slack: float = 1.0) -> bool:
        """Вертикально внутри: для handle, торчащих за левый край узла."""
        return other.y >= self.y - slack and other.bottom <= self.bottom + slack


class Css:
    """Вычисленный стиль и геометрия элемента."""

    @staticmethod
    def of(target: Locator, prop: str, pseudo: str = "") -> str:
        script = (
            "(el, [prop, pseudo]) => "
            "getComputedStyle(el, pseudo || null).getPropertyValue(prop).trim()"
        )
        return str(target.evaluate(script, [prop, pseudo]))

    @staticmethod
    def box(target: Locator) -> Box:
        found = target.bounding_box()
        if found is None:
            raise AssertionError("element has no box: it is not rendered")

        return Box(found["x"], found["y"], found["width"], found["height"])

    @staticmethod
    def scale(target: Locator) -> float:
        """Масштаб из matrix(a, b, c, d, e, f) — коэффициент a."""
        transform = Css.of(target, "transform")
        match = re.match(r"matrix\(([^,]+),", transform)
        if match is None:
            return 1.0

        return float(match.group(1))


def close(actual: float, expected: float, slack: float = 0.5) -> bool:
    """Сравнение CSS-пикселей: браузер отдаёт дробные значения раскладки."""
    return abs(actual - expected) <= slack


def fluid(minimum: float, share: float, viewport: float, maximum: float) -> float:
    """Значение clamp(min, share*vw, max) в CSS-пикселях для данного viewport."""
    return min(max(minimum, share * viewport), maximum)


def no_horizontal_scroll(page: Page) -> bool:
    return bool(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= "
            "document.documentElement.clientWidth + 1"
        )
    )
