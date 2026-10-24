"""Тесты вида aid: кадр области страницы против эталона из git, дерево
доступности рядом с ним, ключевые вычисленные стили и отчёт о разнице.

Эталоны лежат в tests/look/baseline/<группа>/: <состояние>@<тема>-<ширина>.png
и <состояние>@<ширина>.aria.yml, окружение снимков — в env.json. Сравнение —
Pillow: пиксель другой, если хоть один канал разошёлся больше порога; по
умолчанию другим не может быть ни один. Переснять эталоны можно только
опцией --look-update, и такой прогон всё равно красный: новые эталоны
пользователь принимает через git. Отчёт — index.html в каталоге --look-out.

Ошибки:
LookError — кадр, дерево или стиль не совпали с эталоном, эталона нет,
    эталоны сняты в другом окружении или фронт собран из других исходников.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import html
import io
import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageChops
from playwright.sync_api import Browser, BrowserContext, Locator, Page, ViewportSize
from pydantic import BaseModel, ConfigDict, TypeAdapter

from boba.stand.ui.stand import (
    StandApp,
    StandAuth,
    StandConfig,
    StandPorts,
    StandProcess,
)

__all__ = [
    "FrontBuild",
    "LookAxes",
    "LookCheck",
    "LookEnvironment",
    "LookError",
    "LookFix",
    "LookFixes",
    "LookFrame",
    "LookLanguage",
    "LookOutcome",
    "LookPages",
    "LookPaths",
    "LookReport",
    "LookSettings",
    "LookSize",
    "LookStand",
    "LookState",
    "LookStyles",
    "LookTheme",
]


class LookError(AssertionError):
    """Вид разошёлся с эталоном или снимать его нельзя."""


class LookPaths:
    """Каталоги и файлы, на которые опираются тесты вида: эталоны, сборка
    фронта и её исходники в дереве репозитория."""

    LOOK: ClassVar[Path] = Path(__file__).resolve().parent
    PACKAGE: ClassVar[Path] = LOOK.parents[1]
    REPO: ClassVar[Path] = PACKAGE.parents[2]
    BASELINE: ClassVar[Path] = LOOK / "baseline"
    ENV_FILE: ClassVar[Path] = BASELINE / "env.json"
    ASSETS: ClassVar[Path] = PACKAGE / "assets"
    UI_DIGEST: ClassVar[Path] = ASSETS / "ui" / ".source-digest"
    ELEMENTS: ClassVar[Path] = ASSETS / "public" / "elements"
    THEME: ClassVar[Path] = ASSETS / "public" / "theme.json"
    CHAINLIT_UI: ClassVar[Path] = PACKAGE / "web" / "chainlit-ui"
    BUILD_SCRIPT: ClassVar[Path] = REPO / "build" / "scripts" / "chainlit_ui.sh"


class LookTheme(StrEnum):
    """Тема интерфейса: значение localStorage["vite-ui-theme"] chainlit."""

    DARK = "dark"
    LIGHT = "light"


class LookSize(StrEnum):
    """Размер окна снимка: ноутбук и телефон."""

    WIDE = "1280"
    NARROW = "420"

    @property
    def viewport(self) -> ViewportSize:
        if self is LookSize.WIDE:
            return {"width": 1280, "height": 900}

        return {"width": 420, "height": 800}


class LookAxes:
    """Сочетания осей кадра для прохода теста: размер окна снаружи, тема
    внутри — тема переключается без нового входа в другом размере."""

    def every(self) -> list[tuple[LookTheme, LookSize]]:
        frames: list[tuple[LookTheme, LookSize]] = []
        for size in LookSize:
            for theme in LookTheme:
                frames.append((theme, size))

        return frames


class LookFix(BaseModel):
    """Правка вида, которую показывают кадры: пункт перечня правок вида или
    решение по редизайну. Состояние называет свои правки, а страница отчёта
    перечисляет их наверху со ссылками на кадры, где правку видно."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    summary: str


class LookFixes:
    """Правки вида, которые показывают кадры тестов вида: пункты перечня
    правок вида (N…) и решения по редизайну (R…). Состояние называет их в
    LookState.fixes, отчёт собирает наверху страницы утверждения."""

    N3: ClassVar[LookFix] = LookFix(
        code="N3",
        summary=(
            "один язык в окне: подсказки полей входа «Введите логин» и «Введите "
            "пароль» вместо login и password, профили чата названы и описаны "
            "по-русски"
        ),
    )
    N33: ClassVar[LookFix] = LookFix(
        code="N33",
        summary=(
            "страница входа без картинки справа при любом наборе входов, в том "
            "числе только SSO: одна колонка, логотип по центру, кнопка SSO "
            "размера M Consta; в тёмной теме логотип белый"
        ),
    )
    FOCUS: ClassVar[LookFix] = LookFix(
        code="R1",
        summary=(
            "кольцо клавиатурного фокуса: Tab рисует кольцо 2 px цвета --ring "
            "темы (в светлой — синий Consta #0078d2) у кнопок и ссылок, поле "
            "ввода — рамкой того же цвета; мышью колец нет"
        ),
    )
    NAME: ClassVar[LookFix] = LookFix(
        code="R2",
        summary=(
            "имя ассистента «AID» целиком кириллицей; «Описание» "
            "называет его так же, а не прежним AID"
        ),
    )
    APP_CARD: ClassVar[LookFix] = LookFix(
        code="A1",
        summary=(
            "приложение MCP Apps — карточкой основной ленты под ходом, а не в "
            "шаге вызова: рамка с края текста ответа до края вопроса, шапка "
            "«сервер · инструмент» с кнопками «на весь экран» и «свернуть», "
            "цвета и шрифт чата у приложения; шаг — только аргументы и итог"
        ),
    )


@dataclass(frozen=True)
class LookState:
    """Состояние интерфейса из описи плана тестов вида: группа, номер, подпись
    и что проверить глазами человеку, утверждающему эталон, и правки вида,
    которые видно в его кадрах."""

    group: str
    name: str
    caption: str
    check: str
    fixes: Sequence[LookFix]


@dataclass(frozen=True)
class LookFrame:
    """Кадр эталона: состояние в одной теме и одном размере окна."""

    state: LookState
    theme: LookTheme
    size: LookSize

    @property
    def stem(self) -> str:
        return f"{self.state.name}@{self.theme.value}-{self.size.value}"

    def png(self, root: Path) -> Path:
        return root / self.state.group / f"{self.stem}.png"

    def aria(self, root: Path) -> Path:
        """Дерево доступности от темы не зависит: одно на состояние и размер."""
        name = f"{self.state.name}@{self.size.value}.aria.yml"
        return root / self.state.group / name


class OutcomeKind(StrEnum):
    """Итог проверки кадра."""

    SAME = "same"
    MISSING = "missing"
    PIXELS = "pixels"
    ARIA = "aria"
    STYLE = "style"
    UPDATED = "updated"

    @property
    def failed(self) -> bool:
        return self is not OutcomeKind.SAME


@dataclass(frozen=True)
class LookOutcome:
    """Что показала проверка кадра: вид итога, объяснение и файлы отчёта."""

    frame: LookFrame
    kind: OutcomeKind
    detail: str
    test: str
    files: Mapping[str, str] = field(default_factory=dict)
    """Картинки и тексты отчёта по ролям: expected, actual, diff, aria."""

    def line(self) -> str:
        return f"{self.frame.state.group}/{self.frame.stem}: {self.kind}: {self.detail}"


class LookSettings(BaseModel):
    """Опции прогона вида: маска тестов, чьи эталоны пересняты, и каталог
    отчёта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    update: str
    """Маска имён тестов (fnmatch) для переснятия; пусто — эталоны не трогаются."""

    out: Path

    def updates(self, test: str) -> bool:
        if not self.update:
            return False

        return fnmatch.fnmatch(test, self.update)


class LookEnvironment(BaseModel):
    """Окружение, в котором сняты эталоны: другой браузер или шрифты дают
    другие пиксели, поэтому несовпадение отказывает всему набору сразу."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    playwright: str
    browser: str
    browser_build: str
    device_scale_factor: int
    locale: str
    timezone: str
    fonts: str
    """sha256 списка шрифтов fontconfig хоста: моноширинный и запасной шрифты
    берутся оттуда."""


class LookEnvironments:
    """Отпечаток окружения снимков и сверка его с env.json эталонов."""

    DEVICE_SCALE: ClassVar[int] = 1
    LOCALE: ClassVar[str] = "ru-RU"
    TIMEZONE: ClassVar[str] = "Europe/Moscow"
    FONT_FORMAT: ClassVar[str] = "%{family}|%{style}|%{file}\\n"
    FC_LIST: ClassVar[str] = "fc-list"

    def current(self, browser: Browser) -> LookEnvironment:
        executable = Path(browser.browser_type.executable_path)
        build = ""
        for part in executable.parts:
            if part.startswith(browser.browser_type.name):
                build = part

        return LookEnvironment(
            playwright=version("playwright"),
            browser=browser.version,
            browser_build=build,
            device_scale_factor=self.DEVICE_SCALE,
            locale=self.LOCALE,
            timezone=self.TIMEZONE,
            fonts=self._fonts(),
        )

    def check(self, current: LookEnvironment, settings: LookSettings) -> None:
        """Ошибки:
        LookError — эталоны сняты в другом окружении.
        """
        path = LookPaths.ENV_FILE
        if settings.update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(current.model_dump_json(indent=2) + "\n", encoding="utf-8")
            return

        if not path.is_file():
            msg = (
                f"look baselines: no environment file {path}; take the baselines "
                "with --look-update and review the report"
            )
            raise LookError(msg)

        taken = LookEnvironment.model_validate_json(path.read_text(encoding="utf-8"))
        if taken == current:
            return

        msg = (
            f"look baselines were taken in another environment: {taken.model_dump()}, "
            f"this run has {current.model_dump()}; review the difference and retake "
            "them with --look-update"
        )
        raise LookError(msg)

    def _fonts(self) -> str:
        fc_list = shutil.which(self.FC_LIST)
        if fc_list is None:
            msg = f"look environment: {self.FC_LIST} is not on PATH, fonts unknown"
            raise LookError(msg)

        try:
            listed = subprocess.run(
                [fc_list, "--format", self.FONT_FORMAT],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            msg = f"look environment: listing fonts with fc-list failed: {exc}"
            raise LookError(msg) from exc

        lines = sorted(listed.stdout.splitlines())
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class FrontBuild:
    """Свежесть сборки UI chainlit: `.source-digest`, который пишет
    build/scripts/chainlit_ui.sh, против тех же хешей текущих исходников.
    Старая сборка дала бы ложную разницу с эталонами."""

    def check(self) -> None:
        """Ошибки:
        LookError — UI собран из других исходников или не собран вовсе.
        """
        path = LookPaths.UI_DIGEST
        if not path.is_file():
            msg = f"look tests: the chainlit UI has no {path}; build it with make web"
            raise LookError(msg)

        built = path.read_text(encoding="utf-8")
        expected = self.digest()
        if built == expected:
            return

        msg = (
            f"look tests: the chainlit UI in {path.parent} is built from other "
            f"sources: built {built!r}, the sources give {expected!r}; rebuild it "
            "with make web"
        )
        raise LookError(msg)

    def digest(self) -> str:
        """Те же строки, что пишет скрипт сборки: версия тега и хеши деревьев."""
        upstream = (LookPaths.CHAINLIT_UI / "UPSTREAM").read_text(encoding="utf-8")
        script = self._file(LookPaths.BUILD_SCRIPT)
        overlay = self._tree(LookPaths.CHAINLIT_UI / "overlay")
        elements = self._tree(LookPaths.ELEMENTS)
        return (
            f"chainlit {upstream.strip()}\n"
            f"overlay {overlay}\n"
            f"elements {elements}\n"
            f"script {script}\n"
        )

    def _tree(self, root: Path) -> str:
        """sha256 вывода `find . -type f | LC_ALL=C sort | xargs sha256sum`."""
        names: list[str] = []
        for path in root.rglob("*"):
            if path.is_file():
                names.append(f"./{path.relative_to(root).as_posix()}")

        lines: list[str] = []
        for name in sorted(names, key=lambda item: item.encode("utf-8")):
            lines.append(f"{self._file(root / name)}  {name}\n")

        return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()

    @staticmethod
    def _file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


class NormalRule(StrEnum):
    """Правила нормализации текста в области кадра перед снимком: значения,
    которые меняются от прогона к прогону. Правило — регулярное выражение
    JavaScript и подстановка того же вида; новое правило вводится только с
    объяснением в тесте."""

    DURATION = r"· \d+(?:[.,]\d+)? (ms|s|мс|с)(?![A-Za-zА-Яа-яЁё])"
    UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    LAST_SEEN = r"\d+s ago"
    LOCAL_PORT = r"127\.0\.0\.1:\d+"
    """Порт стенда в тексте сбоя: свободный порт выбирается на каждый прогон."""

    SERVER_ID = r"serverId '[A-Za-z0-9_-]+'"
    """serverId регистрации сервера MCP в тексте сбоя: новый на каждый запуск
    агента."""

    @property
    def replacement(self) -> str:
        if self is NormalRule.DURATION:
            return "· 0 $1"

        if self is NormalRule.UUID:
            return "00000000-0000-0000-0000-000000000000"

        if self is NormalRule.LOCAL_PORT:
            return "127.0.0.1:0"

        if self is NormalRule.SERVER_ID:
            return "serverId '0'"

        return "0s ago"


class TextNormalizer:
    """Подставляет правила NormalRule в текстовые узлы области и в адреса
    ссылок (их показывает дерево доступности: id треда в пути файла);
    отдаёт правила, которые что-то заменили, — их список уходит в отчёт."""

    SCRIPT: ClassVar[str] = """(root, rules) => {
      const applied = new Set();
      const normal = (text) => {
        for (const [name, pattern, replacement] of rules) {
          const next = text.replace(new RegExp(pattern, "g"), replacement);
          if (next !== text) applied.add(name);
          text = next;
        }
        return text;
      };
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = normal(node.nodeValue);
        if (text !== node.nodeValue) node.nodeValue = text;
      }
      for (const link of root.querySelectorAll("[href]")) {
        const href = link.getAttribute("href");
        const next = normal(href);
        if (next !== href) link.setAttribute("href", next);
      }
      return [...applied];
    }"""

    def apply(self, region: Locator) -> list[str]:
        rules: list[list[str]] = []
        for rule in NormalRule:
            rules.append([rule.name, rule.value, rule.replacement])

        applied = region.evaluate(self.SCRIPT, rules)
        return [str(name) for name in applied]


class StableShot:
    """Снимок области, повторённый до двух побайтно равных подряд: так же
    ждёт устойчивости toHaveScreenshot у Node-раннера playwright."""

    ATTEMPTS: ClassVar[int] = 10
    PAUSE_MS: ClassVar[int] = 100
    STYLE: ClassVar[str] = (
        "::-webkit-scrollbar { display: none !important; } "
        "* { scrollbar-width: none !important; }"
    )

    def take(self, region: Locator) -> bytes:
        """Ошибки:
        LookError — кадр не устоялся за ATTEMPTS снимков.
        """
        previous = self._shot(region)
        for _ in range(self.ATTEMPTS):
            region.page.wait_for_timeout(self.PAUSE_MS)
            current = self._shot(region)
            if current == previous:
                return current

            previous = current

        msg = (
            f"look shot of {region}: the frame did not settle within "
            f"{self.ATTEMPTS} shots {self.PAUSE_MS} ms apart"
        )
        raise LookError(msg)

    def _shot(self, region: Locator) -> bytes:
        return region.screenshot(
            animations="disabled", caret="hide", scale="css", style=self.STYLE
        )


@dataclass(frozen=True)
class PixelDiff:
    """Разница двух кадров: число разных пикселей и карта отличий."""

    pixels: int
    total: int
    map_png: bytes


class PixelCompare:
    """Сравнение кадров на Pillow: пиксель другой, если разница хоть одного
    канала больше THRESHOLD из 255 (сглаживание шрифтов даёт 1–10)."""

    THRESHOLD: ClassVar[int] = 16
    DIM: ClassVar[float] = 0.25
    MARK: ClassVar[tuple[int, int, int]] = (255, 0, 0)

    def compare(self, expected: bytes, actual: bytes) -> PixelDiff:
        """Ошибки:
        LookError — размеры кадров разные.
        """
        old = Image.open(io.BytesIO(expected)).convert("RGB")
        new = Image.open(io.BytesIO(actual)).convert("RGB")
        if old.size != new.size:
            msg = f"frame size changed: expected {old.size}, got {new.size}"
            raise LookError(msg)

        delta = ImageChops.difference(old, new)
        channels = delta.split()
        worst = ImageChops.lighter(
            ImageChops.lighter(channels[0], channels[1]), channels[2]
        )
        mask = worst.point(self._over)
        counted = mask.histogram()[255]

        faded = Image.blend(Image.new("RGB", new.size, (0, 0, 0)), new, self.DIM)
        marks = Image.new("RGB", new.size, self.MARK)
        faded.paste(marks, mask=mask)
        out = io.BytesIO()
        faded.save(out, format="PNG")

        return PixelDiff(
            pixels=counted, total=new.width * new.height, map_png=out.getvalue()
        )

    def _over(self, value: int) -> int:
        if value > self.THRESHOLD:
            return 255

        return 0


class LookStyles:
    """Вычисленные стили, которые проверяются на каждую тему: цвет primary из
    theme.json, шрифт ленты и отсутствие горизонтальной прокрутки."""

    FONT: ClassVar[str] = "Inter"
    PRIMARY: ClassVar[str] = "--primary"
    PROBE: ClassVar[str] = """([expected]) => {
      const probe = (color) => {
        const node = document.createElement("div");
        node.style.color = color;
        document.body.appendChild(node);
        const seen = getComputedStyle(node).color;
        node.remove();
        return seen;
      };
      return [probe("hsl(var(--primary))"), probe(`hsl(${expected})`)];
    }"""

    def __init__(self) -> None:
        self._theme = json.loads(LookPaths.THEME.read_text(encoding="utf-8"))

    def problems(self, page: Page, theme: LookTheme) -> Iterator[str]:
        expected = self._theme["variables"][theme.value][self.PRIMARY]
        shown, wanted = page.evaluate(self.PROBE, [expected])
        if shown != wanted:
            yield f"--primary is {shown}, theme.json gives {wanted} for {theme}"

        family = page.evaluate("() => getComputedStyle(document.body).fontFamily")
        if self.FONT not in str(family):
            yield f"body font is {family!r}, expected {self.FONT}"

        widths = page.evaluate(
            "() => [document.documentElement.scrollWidth, "
            "document.documentElement.clientWidth]"
        )
        if widths[0] > widths[1]:
            yield f"page scrolls sideways: scrollWidth {widths[0]} > {widths[1]}"


class ReportRow(BaseModel):
    """Строка отчёта: кадр, итог и файлы — то, что процесс xdist кладёт в свой
    список итогов, а страница отчёта читает из всех списков."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    state: str
    caption: str
    check: str
    fixes: Sequence[LookFix]
    theme: str
    size: str
    kind: OutcomeKind
    detail: str
    test: str
    files: Mapping[str, str]

    @property
    def failed(self) -> bool:
        return self.kind.failed

    def order(self) -> tuple[str, str, str, str]:
        return (self.group, self.state, self.size, self.theme)


class LookReport:
    """Отчёт прогона вида в каталоге --look-out: картинки кадров, разница
    дерева доступности и index.html со всеми кадрами, что видел процесс.

    Процессы xdist пишут каждый свой список итогов (<процесс>.json); index.html
    собирается из всех списков каталога, поэтому последний писатель видит всех.
    """

    MANIFESTS: ClassVar[str] = "manifests"
    INDEX: ClassVar[str] = "index.html"

    def __init__(self, out: Path, worker: str) -> None:
        self._out = out
        self._worker = worker
        self._outcomes: list[LookOutcome] = []
        self._rows = TypeAdapter(list[ReportRow])

    @property
    def out(self) -> Path:
        return self._out

    def add(self, outcome: LookOutcome) -> None:
        self._outcomes.append(outcome)

    def save(self, frame: LookFrame, role: str, data: bytes) -> str:
        """Файл отчёта кадра; путь — от каталога отчёта."""
        relative = Path(frame.state.group) / f"{frame.stem}.{role}"
        target = self._out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative.as_posix()

    def write(self) -> Path:
        manifests = self._out / self.MANIFESTS
        manifests.mkdir(parents=True, exist_ok=True)
        rows: list[ReportRow] = []
        for outcome in self._outcomes:
            rows.append(
                ReportRow(
                    group=outcome.frame.state.group,
                    state=outcome.frame.state.name,
                    caption=outcome.frame.state.caption,
                    check=outcome.frame.state.check,
                    fixes=outcome.frame.state.fixes,
                    theme=outcome.frame.theme.value,
                    size=outcome.frame.size.value,
                    kind=outcome.kind,
                    detail=outcome.detail,
                    test=outcome.test,
                    files=outcome.files,
                )
            )

        own = manifests / f"{self._worker}.json"
        own.write_bytes(self._rows.dump_json(rows, indent=1))

        everything: list[ReportRow] = []
        for manifest in sorted(manifests.glob("*.json")):
            everything.extend(self._rows.validate_json(manifest.read_bytes()))

        index = self._out / self.INDEX
        draft = self._out / f".{self.INDEX}.{self._worker}"
        draft.write_text(ReportPage().render(everything), encoding="utf-8")
        os.replace(draft, index)
        return index


class ReportPage:
    """Страница отчёта: наверху правки вида со ссылками на состояния, где их
    видно, ниже состояния по группам с подписью и тем, что проверить глазами,
    под каждым — его кадры во всех темах и размерах. У разошедшегося кадра —
    эталон, новый кадр, карта отличий, наложение ползунком и разница дерева
    доступности; переснятый кадр показан как есть: страница служит и для
    утверждения."""

    STYLE: ClassVar[str] = """
      :root { color-scheme: light; }
      body { font: 14px/1.45 system-ui, sans-serif; margin: 16px; background: #f4f4f5;
             color: #18181b; }
      h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 17px; margin-top: 32px; }
      .summary { color: #52525b; margin-bottom: 16px; }
      .state { background: #fff; border: 1px solid #d4d4d8; border-radius: 8px;
               padding: 12px; margin: 12px 0; }
      .state h3 { font-size: 15px; margin: 0 0 10px; }
      .state h3 code { color: #52525b; font-weight: 400; }
      .check { margin: -4px 0 10px; color: #3f3f46; }
      .tags { margin: -6px 0 10px; }
      .tag { display: inline-block; font-size: 12px; background: #e0f2fe;
             color: #075985; border-radius: 4px; padding: 0 6px; margin-right: 4px; }
      .fixes { background: #fff; border: 1px solid #d4d4d8; border-radius: 8px;
               padding: 4px 16px 8px; }
      .fixes li { margin: 6px 0; }
      .frames { display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }
      figure { margin: 0; }
      figure.wide { width: min(560px, 100%); }
      figure.narrow { width: min(260px, 100%); }
      figure img { width: 100%; height: auto; border: 1px solid #d4d4d8;
                   border-radius: 4px; }
      figcaption { font-size: 12px; color: #52525b; margin-top: 4px; }
      .failed { border-color: #dc2626; }
      .failed figcaption .kind { color: #dc2626; font-weight: 600; }
      .updated figcaption .kind { color: #b45309; font-weight: 600; }
      .detail { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px;
                padding: 8px; margin-top: 10px; }
      .detail .images { display: flex; gap: 12px; flex-wrap: wrap; }
      .detail img { max-width: min(420px, 100%); border: 1px solid #e4e4e7; }
      .overlay { position: relative; display: inline-block; }
      .overlay img.top { position: absolute; left: 0; top: 0; }
    """
    SLIDER: ClassVar[str] = (
        "<input type='range' min='0' max='100' value='50' aria-label='overlay' "
        "oninput=\"this.previousElementSibling.querySelector('.top').style.opacity"
        '=this.value/100">'
    )

    def render(self, rows: Sequence[ReportRow]) -> str:
        failed = 0
        retaken = 0
        for row in rows:
            if row.kind is OutcomeKind.UPDATED:
                retaken += 1
                continue

            if row.failed:
                failed += 1

        parts = [
            "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            f"<title>Look report</title><style>{self.STYLE}</style></head><body>",
            "<h1>Look report</h1>",
            f"<div class='summary'>{len(rows)} frames: {failed} differ, "
            f"{retaken} retaken</div>",
            self._fixes(rows),
        ]
        group = ""
        for state, frames in self._states(rows):
            first = frames[0]
            if first.group != group:
                group = first.group
                parts.append(f"<h2>{html.escape(group)}</h2>")

            parts.append(self._state(state, first, frames))

        parts.append("</body></html>")
        return "\n".join(parts)

    def _fixes(self, rows: Sequence[ReportRow]) -> str:
        """Правки вида прогона: у каждой — ссылки на состояния, где её видно,
        подписью состояния."""
        summaries: dict[str, str] = {}
        states: dict[str, dict[str, ReportRow]] = {}
        for row in sorted(rows, key=ReportRow.order):
            for fix in row.fixes:
                summaries[fix.code] = fix.summary
                shown = states.setdefault(fix.code, {})
                shown.setdefault(row.state, row)

        if not summaries:
            return ""

        items: list[str] = []
        for code, summary in summaries.items():
            links: list[str] = []
            for row in states[code].values():
                anchor = html.escape(self._anchor(row))
                links.append(f"<a href='#{anchor}'>{html.escape(row.caption)}</a>")

            items.append(
                f"<li><b>{html.escape(code)}</b> {html.escape(summary)} — "
                f"{', '.join(links)}</li>"
            )

        return (
            "<section class='fixes'><h2>Fixes shown in these frames</h2>"
            f"<ul>{''.join(items)}</ul></section>"
        )

    @staticmethod
    def _states(rows: Sequence[ReportRow]) -> list[tuple[str, list[ReportRow]]]:
        grouped: dict[tuple[str, str], list[ReportRow]] = {}
        for row in sorted(rows, key=ReportRow.order):
            grouped.setdefault((row.group, row.state), []).append(row)

        found: list[tuple[str, list[ReportRow]]] = []
        for (_, state), frames in grouped.items():
            found.append((state, frames))

        return found

    @staticmethod
    def _anchor(row: ReportRow) -> str:
        return f"{row.group}-{row.state}"

    def _state(self, state: str, first: ReportRow, frames: Sequence[ReportRow]) -> str:
        figures: list[str] = []
        details: list[str] = []
        for row in frames:
            figures.append(self._figure(row))
            if row.failed and row.kind is not OutcomeKind.UPDATED:
                details.append(self._detail(row))

        tags: list[str] = []
        for fix in first.fixes:
            tags.append(f"<span class='tag'>{html.escape(fix.code)}</span>")

        tagged = ""
        if tags:
            tagged = f"<div class='tags'>{''.join(tags)}</div>"

        return (
            f"<section class='state' id='{html.escape(self._anchor(first))}'>"
            f"<h3>{html.escape(first.caption)} <code>{html.escape(state)}</code></h3>"
            f"<p class='check'>Check: {html.escape(first.check)}</p>{tagged}"
            f"<div class='frames'>{''.join(figures)}</div>{''.join(details)}</section>"
        )

    def _figure(self, row: ReportRow) -> str:
        width = "wide"
        if row.size == LookSize.NARROW.value:
            width = "narrow"

        css = width
        if row.kind is OutcomeKind.UPDATED:
            css = f"{width} updated"
        elif row.failed:
            css = f"{width} failed"

        image = ""
        if actual := row.files.get("actual.png"):
            image = f"<img src='{html.escape(actual)}' alt='{html.escape(row.state)}'>"

        return (
            f"<figure class='{css}'>{image}<figcaption>{row.theme} · {row.size} px · "
            f"<span class='kind'>{html.escape(row.kind.value)}</span></figcaption>"
            "</figure>"
        )

    def _detail(self, row: ReportRow) -> str:
        images: list[str] = []
        for role in ("expected", "actual", "diff"):
            if name := row.files.get(f"{role}.png"):
                images.append(
                    f"<figure><img src='{html.escape(name)}' alt='{role}'>"
                    f"<figcaption>{role}</figcaption></figure>"
                )

        expected = row.files.get("expected.png")
        actual = row.files.get("actual.png")
        if expected and actual:
            images.append(
                "<figure><div class='overlay'>"
                f"<img src='{html.escape(expected)}' alt='expected'>"
                f"<img class='top' src='{html.escape(actual)}' alt='actual' "
                "style='opacity:.5'></div>"
                f"{self.SLIDER}<figcaption>overlay</figcaption></figure>"
            )

        aria = ""
        if diff := row.files.get("aria.diff"):
            link = html.escape(diff)
            aria = f"<div>aria: <a href='{link}'>{link}</a></div>"

        return (
            f"<div class='detail'><div>{row.theme} · {row.size} px: "
            f"{html.escape(row.detail)} · {html.escape(row.test)}</div>"
            f"<div class='images'>{''.join(images)}</div>{aria}</div>"
        )


class LookCheck:
    """Проверки вида одного теста: каждый кадр — снимок, дерево и стили против
    эталонов; все разницы собираются и роняют тест один раз в verify().

    Создаётся фикстурой теста на его имя; в режиме переснятия пишет эталоны
    на место и всё равно падает в verify().
    """

    def __init__(self, test: str, settings: LookSettings, report: LookReport) -> None:
        self._test = test
        self._update = settings.updates(test)
        self._report = report
        self._shots = StableShot()
        self._pixels = PixelCompare()
        self._normal = TextNormalizer()
        self._styles = LookStyles()
        self._outcomes: list[LookOutcome] = []
        self._taken: dict[str, bytes] = {}
        """Кадры этого прогона по stem: с ними сверяется тот же вид, снятый
        иначе (живая лента против истории)."""

    def frame(
        self,
        region: Locator,
        state: LookState,
        theme: LookTheme,
        size: LookSize,
        tolerance: int = 0,
    ) -> None:
        """Кадр области region в состоянии state; tolerance — сколько пикселей
        вправе отличаться (по умолчанию ни одного; допуск — с объяснением в
        тесте)."""
        frame = LookFrame(state=state, theme=theme, size=size)
        viewport = size.viewport
        region.page.mouse.move(viewport["width"] - 2, viewport["height"] // 2)
        applied = self._normal.apply(region)
        actual = self._shots.take(region)
        aria = region.aria_snapshot()

        self._taken[frame.stem] = actual
        files: dict[str, str] = {}
        files["actual.png"] = self._report.save(frame, "actual.png", actual)

        kind, detail = self._pixel_outcome(frame, actual, tolerance, files)
        aria_kind, aria_detail = self._aria_outcome(frame, aria, files)
        if kind is OutcomeKind.SAME:
            kind = aria_kind
            detail = aria_detail

        style = list(self._styles.problems(region.page, theme))
        if style and kind is OutcomeKind.SAME:
            kind = OutcomeKind.STYLE
            detail = "; ".join(style)

        if applied:
            detail = f"{detail} (normalized: {', '.join(applied)})"

        outcome = LookOutcome(
            frame=frame, kind=kind, detail=detail, test=self._test, files=files
        )
        self._outcomes.append(outcome)
        self._report.add(outcome)

    def same_as(
        self, state: LookState, live: LookState, theme: LookTheme, size: LookSize
    ) -> None:
        """Кадр state этого прогона обязан совпасть пиксель в пиксель с кадром
        live в той же теме и размере: один вид, полученный двумя путями (ход
        вживую и тот же ход после перезагрузки). Разница — разошедшийся кадр
        state с картой отличий в отчёте."""
        frame = LookFrame(state=state, theme=theme, size=size)
        other = LookFrame(state=live, theme=theme, size=size)
        shot = self._taken[frame.stem]
        original = self._taken[other.stem]
        files: dict[str, str] = {}
        files["expected.png"] = self._report.save(frame, "live.png", original)
        files["actual.png"] = self._report.save(frame, "actual.png", shot)
        try:
            diff = self._pixels.compare(original, shot)
        except LookError as exc:
            detail = f"differs from the live frame {other.stem}: {exc}"
            self._add(
                LookOutcome(
                    frame=frame,
                    kind=OutcomeKind.PIXELS,
                    detail=detail,
                    test=self._test,
                    files=files,
                )
            )
            return

        if diff.pixels == 0:
            return

        files["diff.png"] = self._report.save(frame, "live-diff.png", diff.map_png)
        detail = (
            f"{diff.pixels} of {diff.total} pixels differ from the live frame "
            f"{other.stem}"
        )
        self._add(
            LookOutcome(
                frame=frame,
                kind=OutcomeKind.PIXELS,
                detail=detail,
                test=self._test,
                files=files,
            )
        )

    def _add(self, outcome: LookOutcome) -> None:
        self._outcomes.append(outcome)
        self._report.add(outcome)

    def verify(self) -> None:
        """Ошибки:
        LookError — хоть один кадр разошёлся с эталоном, либо эталоны пересняты.
        """
        failed: list[str] = []
        for outcome in self._outcomes:
            if outcome.kind.failed:
                failed.append(outcome.line())

        report = self._report.out / LookReport.INDEX
        if self._update:
            msg = (
                f"{self._test}: look baselines retaken ({len(self._outcomes)} "
                f"frames, {len(failed)} changed); review {report} and accept them "
                "with git add or drop them with git checkout\n" + "\n".join(failed)
            )
            raise LookError(msg)

        if not failed:
            return

        msg = (
            f"{self._test}: {len(failed)} of {len(self._outcomes)} frames differ "
            f"from the baselines, see {report}\n" + "\n".join(failed)
        )
        raise LookError(msg)

    def _pixel_outcome(
        self, frame: LookFrame, actual: bytes, tolerance: int, files: dict[str, str]
    ) -> tuple[OutcomeKind, str]:
        path = frame.png(LookPaths.BASELINE)
        if not path.is_file():
            return self._retake(
                path, actual, OutcomeKind.MISSING, f"no baseline {path}"
            )

        expected = path.read_bytes()
        files["expected.png"] = self._report.save(frame, "expected.png", expected)
        try:
            diff = self._pixels.compare(expected, actual)
        except LookError as exc:
            return self._retake(path, actual, OutcomeKind.PIXELS, str(exc))

        files["diff.png"] = self._report.save(frame, "diff.png", diff.map_png)
        if diff.pixels <= tolerance:
            return OutcomeKind.SAME, f"{diff.pixels} pixels differ, allowed {tolerance}"

        detail = f"{diff.pixels} of {diff.total} pixels differ, allowed {tolerance}"
        return self._retake(path, actual, OutcomeKind.PIXELS, detail)

    def _aria_outcome(
        self, frame: LookFrame, aria: str, files: dict[str, str]
    ) -> tuple[OutcomeKind, str]:
        path = frame.aria(LookPaths.BASELINE)
        text = f"{aria}\n"
        if not path.is_file():
            files["aria.diff"] = self._report.save(frame, "aria.diff", text.encode())
            detail = f"no baseline {path}"
            return self._retake(path, text.encode(), OutcomeKind.MISSING, detail)

        expected = path.read_text(encoding="utf-8")
        if expected == text:
            return OutcomeKind.SAME, "same"

        lines = difflib.unified_diff(
            expected.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile="baseline",
            tofile="actual",
        )
        diff = "".join(lines)
        files["aria.diff"] = self._report.save(frame, "aria.diff", diff.encode())
        detail = "accessibility tree differs"
        return self._retake(path, text.encode(), OutcomeKind.ARIA, detail)

    def _retake(
        self, path: Path, data: bytes, kind: OutcomeKind, detail: str
    ) -> tuple[OutcomeKind, str]:
        if not self._update:
            return kind, detail

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return OutcomeKind.UPDATED, f"{kind}: {detail}; the baseline is written"


class LookPages:
    """Вкладки кадров: контекст браузера на тему и размер окна, с языком,
    часовым поясом, масштабом и без анимаций окружения снимков. Тема ставится
    в localStorage до загрузки страницы — так же, как её запоминает chainlit.
    Создаётся фикстурой теста и закрывает свои контексты в конце."""

    THEME_KEY: ClassVar[str] = "vite-ui-theme"
    LOGIN: ClassVar[str] = "#email"
    PASSWORD: ClassVar[str] = "#password"
    SUBMIT: ClassVar[str] = 'form button[type="submit"]'
    INPUT: ClassVar[str] = "#chat-input"
    FONTS: ClassVar[str] = "() => document.fonts.ready.then(() => document.fonts.size)"
    TOP: ClassVar[str] = """async (node) => {
      const frames = () => new Promise((done) =>
        requestAnimationFrame(() => requestAnimationFrame(done)));
      const pause = () => new Promise((done) => setTimeout(done, 250));
      let last = NaN;
      let same = 0;
      for (let round = 0; round < 40 && same < 3; round += 1) {
        node.scrollIntoView({block: 'start'});
        await frames();
        await pause();
        const top = node.getBoundingClientRect().top;
        same = top === last ? same + 1 : 0;
        last = top;
      }
    }"""
    """Сообщение наверх области прокрутки, пока место не устоится (одно и то
    же положение три раза подряд через 250 мс): спейсер chainlit под последним
    вопросом догоняет высоту ленты позже, и прокрутка до него упирается в
    прежний конец ленты. Тест вида зовёт его у сообщения,
    которое ставит наверх кадра."""
    FEED: ClassVar[str] = (
        "xpath=ancestor::div"
        "[contains(concat(' ', @class, ' '), ' overflow-y-auto ')][2]"
    )
    """Лента сообщений chainlit (ScrollContainer) от сообщения: внешний блок
    с областью прокрутки и кнопкой «↓»."""
    TABS: ClassVar[int] = 40
    FOCUSED: ClassVar[str] = "(node) => node === document.activeElement"
    ACTIVE: ClassVar[str] = (
        "() => { const node = document.activeElement; "
        "return node ? node.outerHTML.slice(0, 160) : ''; }"
    )
    OUTLINE: ClassVar[str] = (
        "(node) => { const style = getComputedStyle(node); "
        "if (style.outlineStyle === 'none') return 'none'; "
        "return `${style.outlineStyle} ${style.outlineWidth}`; }"
    )

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._environment = LookEnvironments()
        self._contexts: list[BrowserContext] = []

    def open(self, theme: LookTheme, size: LookSize) -> Page:
        context = self._browser.new_context(
            viewport=size.viewport,
            device_scale_factor=self._environment.DEVICE_SCALE,
            locale=self._environment.LOCALE,
            timezone_id=self._environment.TIMEZONE,
            color_scheme=theme.value,
            reduced_motion="reduce",
        )
        self._contexts.append(context)
        # скрипт идёт и во фреймы песочницы приложений, где localStorage нет
        context.add_init_script(
            f"if (window === window.top) window.localStorage.setItem("
            f"{json.dumps(self.THEME_KEY)}, {json.dumps(theme.value)})"
        )
        return context.new_page()

    def sign_in(self, page: Page, stand: StandProcess, login: str) -> None:
        """Вход формой стенда учёткой login: открыт пустой тред с полем ввода."""
        credential = stand.config.credential(login)
        page.goto(f"{stand.config.base_url}/login", wait_until="domcontentloaded")
        page.locator(self.LOGIN).fill(credential.login)
        page.locator(self.PASSWORD).fill(credential.password)
        page.locator(self.SUBMIT).click()
        page.locator(self.INPUT).wait_for(timeout=60_000)
        page.wait_for_load_state("networkidle")

    def settle(self, page: Page) -> None:
        """Шрифты загружены, фокус снят с поля: кадр не ловит мигание."""
        page.evaluate(self.FONTS)
        page.evaluate("() => document.activeElement && document.activeElement.blur()")

    def tab_to(self, page: Page, selector: str) -> None:
        """Клавиатурный фокус на selector: Tab, пока фокус не встанет на него, —
        так пользователь идёт по странице с клавиатуры.

        Ошибки:
        LookError — за TABS нажатий фокус не дошёл до selector.
        """
        target = page.locator(selector).first
        for _ in range(self.TABS):
            page.keyboard.press("Tab")
            if target.evaluate(self.FOCUSED):
                return

        active = page.evaluate(self.ACTIVE)
        msg = (
            f"look keyboard focus: {self.TABS} Tab presses did not reach "
            f"{selector}, the focus is on {active!r}"
        )
        raise LookError(msg)

    def outline(self, element: Locator) -> str:
        """Кольцо фокуса элемента: стиль и толщина outline, «none» — кольца нет."""
        return str(element.evaluate(self.OUTLINE))

    def close(self) -> None:
        for context in self._contexts:
            context.close()

        self._contexts.clear()


class LookLanguage(StrEnum):
    """Язык интерфейса стенда тестов вида: [UI].language конфига chainlit."""

    RU = "ru-RU"
    EN = "en-US"


class LookAppRoots:
    """Корни приложения chainlit под язык: язык конфига assets — сами assets,
    другой — копия assets в рабочем каталоге с этим языком в
    .chainlit/config.toml (ссылок нет, только копии)."""

    CONFIG: ClassVar[tuple[str, str]] = (".chainlit", "config.toml")
    LANGUAGE: ClassVar[str] = 'language = "{language}"'

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    def of(self, language: LookLanguage) -> str:
        """Корень для [env].app_root стенда; пусто — assets пакета.

        Ошибки:
        LookError — в конфиге chainlit assets нет строки языка.
        """
        config = LookPaths.ASSETS.joinpath(*self.CONFIG).read_text(encoding="utf-8")
        own = self._language_line(config)
        wanted = self.LANGUAGE.format(language=language.value)
        if own == wanted:
            return ""

        root = self._workdir / f"app-root-{language.value}"
        if not root.is_dir():
            shutil.copytree(LookPaths.ASSETS, root)
            root.joinpath(*self.CONFIG).write_text(
                config.replace(own, wanted), encoding="utf-8"
            )

        return str(root)

    def _language_line(self, config: str) -> str:
        for line in config.splitlines():
            if line.startswith("language = "):
                return line

        path = LookPaths.ASSETS.joinpath(*self.CONFIG)
        msg = f"look stand: {path} has no [UI] language line to replace"
        raise LookError(msg)


@dataclass(kw_only=True)
class WindowStandConfig(StandConfig):
    """Стенд aid с окном модели профилей context_window: малым окном тест
    вида доводит разговор до сжатия."""

    context_window: int

    def _use_fake_llm(self, doc: MutableMapping[str, Any]) -> None:
        super()._use_fake_llm(doc)
        doc["model_limits"]["fake"]["context_window"] = self.context_window


class LookStand:
    """Стенды aid тестов вида: на каждый набор входов и язык свой процесс,
    поднятый по первому запросу и остановленный в конце сессии. База и
    FakeLLM — общие."""

    BOOT_TIMEOUT_SEC: ClassVar[float] = 300.0
    FULL_WINDOW: ClassVar[int] = 200000
    """Окно модели стенда, при котором разговор теста не сжимается."""
    DATABASE: ClassVar[str] = "look_test"
    TOKEN_DELAY_SEC: ClassVar[float] = 0.0

    def __init__(self, workdir: Path, llm_port: int, database: str) -> None:
        self._workdir = workdir
        self._llm_port = llm_port
        self._database = database
        self._roots = LookAppRoots(workdir)
        self._running: dict[tuple[StandAuth, LookLanguage], StandProcess] = {}
        self._served: dict[str, StandProcess] = {}

    def of(
        self, auth: StandAuth, language: LookLanguage = LookLanguage.RU
    ) -> StandProcess:
        if running := self._running.get((auth, language)):
            return running

        name = f"{auth.value.replace('+', '-')}-{language.value}"
        config = StandConfig(
            workdir=self._workdir / name,
            app=StandApp.AID,
            app_port=StandPorts().free(),
            llm_port=self._llm_port,
            db_name=self._database,
            url_prefix="/boba-look",
            auth=auth,
            app_root=self._roots.of(language),
        )
        process = StandProcess(config=config, log_path=self._workdir / f"{name}.log")
        process.start(boot_timeout_sec=self.BOOT_TIMEOUT_SEC)
        self._running[(auth, language)] = process
        return process

    def served(
        self,
        name: str,
        servers: Mapping[str, Mapping[str, object]],
        context_window: int,
    ) -> StandProcess:
        """Стенд name с серверами MCP servers (секции [mcp.servers.<имя>]) и
        окном модели context_window: вход local, язык конфига assets; поднят
        по первому запросу."""
        shape = LookAgentShape(servers=servers, context_window=context_window)

        return self.shaped(name, shape)

    def shaped(self, name: str, shape: LookAgentShape) -> StandProcess:
        """Стенд name с серверами MCP, окном модели и агентом по shape: вход
        local, язык конфига assets; поднят по первому запросу."""
        if running := self._served.get(name):
            return running

        mcp_servers: dict[str, dict[str, Any]] = {}
        for server, section in shape.servers.items():
            mcp_servers[server] = dict(section)

        config = WindowStandConfig(
            workdir=self._workdir / name,
            app=StandApp.AID,
            app_port=StandPorts().free(),
            llm_port=self._llm_port,
            db_name=self._database,
            url_prefix="/boba-look",
            mcp_servers=mcp_servers,
            agent_service=dict(shape.service),
            agent_timeouts_sec=shape.timeouts_sec,
            context_window=shape.context_window,
        )
        if shape.process:
            config = replace(config, agent_process=dict(shape.process))

        log_path = self._workdir / f"{name}.log"
        process: StandProcess[StandConfig] = StandProcess(
            config=config, log_path=log_path
        )
        process.start(boot_timeout_sec=self.BOOT_TIMEOUT_SEC)
        self._served[name] = process
        return process

    def stop(self) -> None:
        for process in self._running.values():
            process.stop()

        for process in self._served.values():
            process.stop()

        self._running.clear()
        self._served.clear()


@dataclass(frozen=True)
class LookAgentShape:
    """Серверы MCP и агент стенда вида: process — [agent.process] агента
    процессом (пусто — агент стенда по умолчанию), service — служба агента
    (пусто — процесс), timeouts_sec — сроки рукопожатия и ответа агента,
    context_window — окно модели профилей."""

    servers: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    process: Mapping[str, Any] = field(default_factory=dict)
    service: Mapping[str, Any] = field(default_factory=dict)
    timeouts_sec: tuple[float, float] = (60.0, 60.0)
    context_window: int = LookStand.FULL_WINDOW
