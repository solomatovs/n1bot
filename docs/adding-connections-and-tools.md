# Инструменты: конфиг, секреты, соединения пользователя и запуск

Инструкция для того, кто пишет tool-плагин впервые или хочет понять, что
происходит между «LLM вызвала pg_query» и «тело инструмента открыло базу».
Три сквозных примера от простого к сложному, потом внутренности launcher'а.

Содержание:

1. Как устроен вызов инструмента
2. Пример 1. Простой инструмент с injected-конфигом
3. Пример 2. Injected-конфиг с секретом
4. Пример 3. Инструмент с соединениями пользователя
5. Как это устроено внутри launcher'а
6. Потоковые инструменты: порты
7. Сборка образа песочницы: `[tool.boba.sandbox]`
8. Проверка и отладка
9. Куда смотреть, если что-то не так

Во всех примерах имена (`wordcount`, `weather`, `redis`) вымышленные:
подставьте своё. Живые аналоги указаны в конце каждого раздела.

---

## 1. Как устроен вызов инструмента

### 1.1. Три вида параметров

Инструмент — это обычная async-функция с декоратором `@tool` из
`boba.toolkit.facade`. Вид каждого параметра задаётся аннотацией:

| Вид | Как объявлен | Кто заполняет | Как едет в тело |
|---|---|---|---|
| LLM-аргумент | `sql: Annotated[str, Field(...)]` | модель | флаг argv `--sql "..."` |
| Injected-конфиг | `cfg: Annotated[MyConfig, Injected]` | приложение из toml | JSON отдельным дескриптором `--injected-fd` |
| Порт данных | `out: Annotated[Outbound[...], Injected]` | гость на вызове | канал кадров, см. раздел 6 |

Три правила, которые объясняют почти всё дальнейшее:

- LLM видит в схеме только LLM-аргументы. Injected-поля приложение снимает
  со схемы после установки обвязок, модель их не видит и подделать не может.
- Секреты никогда не попадают в argv: argv виден в `ps`, в журнале и в
  трейсбеке. Всё, что может быть секретом, идёт injected-каналом.
- Тело инструмента ничего не знает о пользователе, сессии и таблицах:
  оно получает готовый конфиг и работает с ним. Всю «политику» (кому что
  можно, чей билет) решает хост до запуска тела.

### 1.2. Что происходит при вызове

```
LLM  ──tool_call {sql: "...", connection_name: "main"}──▶  приложение (хост)
                                                              │
        обвязки на хосте, снаружи внутрь:                     │
        1. доступ по ролям, журнал, отмена                    │
        2. InjectedConfig   — статический конфиг секции в kwargs["cfg"]
        3. ServiceTickets   — keytab статического конфига → билет вызова
        4. UserConnections  — соединения пользователя → kwargs["cfg"]
        5. ToolProcessWrap  — kwargs → argv + JSON injected
                                                              │
                                        launcher ([tool_launcher] provider)
                                        process: субпроцесс python -m <модуль>
                                        sandbox: форк зиготы внутри bwrap
                                                              │
                                                              ▼
        тело: ToolMain.run(TOOLS)  ──▶  argv → kwargs, JSON → модели
                                        → await tool(**kwargs)
                                        → конверт ReplyOk|ReplyError в --fd-result
```

Каждая обвязка — это `CallHooks`, установленная `ToolBody.hook_all` поверх
предыдущей. Установленная последней оказывается снаружи и срабатывает
первой. Поэтому порядок в `ToolLoader._module_tools` важен и разбирается
в разделе 5.1.

### 1.3. Словарь

- **Плагин** — pip-пакет с инструментами. Объявляет манифест в entry point
  группы `boba.tools`; приложение находит установленные пакеты само.
- **Секция** — идентификатор плагина (`pg`, `doc`, `web`). Он же имя секции
  конфига `tool.<секция>` и имя файла `conf/plugins/<секция>.toml`.
- **`SECTION`** — `ClassVar[str]` на модели injected-конфига: полный путь
  секции в собранном конфиге (`"tool.pg"`). По нему хост находит, из какой
  таблицы toml собрать значение для параметра.
- **Файл плагина** — `compose/<app>/conf/plugins/<секция>.toml`. Его
  содержимое ложится в конфиг как секция `tool.<секция>`, интерполяции
  `${env.*}`, `${postgres}`, `${site.*}` резолвятся от корня.
- **Launcher / провайдер** — способ исполнения тела: секция
  `[tool_launcher]`, `provider = "process"` (используется для отладки, запускает обычный sub-process) или
  `"sandbox"` (используется в релиз, запуск процесса происходит внутри изолированного контейнера с использованием утилиты bwrap).
- **Зигота** — заранее подготовленный процесс (стартует вместе со стартом самого приложения), который готовиться для каждого отдельного плагина. Подготовка заключается в том, что процесс импортирует всё необходимое для работы (прогревает кэш), и соответственно когда нужно запустить инструмент приложение просто делает fork зиготы, что сокращает время на старта, по сравнению если бы мы поднимали процесс каждый раз заново.
- **Конверт** — JSON-ответ тела: `ReplyOk(content, artifact)` либо
  `ReplyError(kind, message)`. Уезжает дескриптором `--fd-result`.

---

## 2. Пример 1. Простой инструмент с injected-конфигом

Инструмент `wordcount`: считает слова в тексте, потолок длины текста
задаёт администратор в конфиге. Секретов нет, соединений нет.

### Шаг 1. Пакет

```
packages/tools/boba-tool-wordcount/
    pyproject.toml
    src/boba/tool/wordcount/
        __init__.py
        plugin.py      # манифест
        tools.py       # конфиг + инструменты + TOOLS
```

Зарегистрируйте пакет в `members` корневого `pyproject.toml` по образцу
соседей.

### Шаг 2. Модель конфига

Модель — обычный pydantic. Обязателен только `SECTION`; он говорит хосту,
из какой секции toml собирать значение. `extra = "ignore"` нужен потому,
что в той же таблице toml лежат служебные ключи `enable`, `tools`,
`sandbox` — их читает загрузчик, а не ваша модель.

```python
"""Инструмент wordcount: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.wordcount.tools wordcount --text "..."`.

Ошибки:
TextTooLongError — текст длиннее max_chars конфига.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult, ToolResult, pack_result


class WordCountConfig(BaseModel):
    """Лимиты wordcount; секция [tool.wordcount]."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.wordcount"

    max_chars: int = Field(gt=0, description="Потолок длины входного текста.")
    top: int = Field(default=10, ge=1, description="Сколько самых частых слов показать.")


class TextTooLongError(Exception):
    """Текст не помещается в лимит; текст готов для пользователя."""


class WordCountErrorKind(StrEnum):
    """Ожидаемые отказы wordcount."""

    TEXT_TOO_LONG = "text_too_long"
```

### Шаг 3. Тело

```python
# tools.py
@tool
async def wordcount(
    text: Annotated[str, Field(min_length=1, description="Текст для подсчёта")],
    cfg: Annotated[WordCountConfig, Injected],
) -> tuple[str, ToolResult]:
    """Считает частоту слов в тексте и показывает самые частые."""
    if len(text) > cfg.max_chars:
        msg = f"text is {len(text)} chars, limit is {cfg.max_chars}"
        raise TextTooLongError(msg)

    counts = Counter(text.lower().split())

    rows: list[dict[str, object]] = []
    for word, count in counts.most_common(cfg.top):
        rows.append({"word": word, "count": count})

    return pack_result(TableResult(rows=rows))


EXPECTED: Mapping[type[Exception], WordCountErrorKind] = {
    TextTooLongError: WordCountErrorKind.TEXT_TOO_LONG,
}

TOOLS: Final = ToolMain.toolset(wordcount)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
```

Что здесь за что отвечает:

- Докстринг обязателен: это описание инструмента для LLM. Без него `@tool`
  падает `ToolFacadeError`.
- `Field(description=...)` у LLM-аргумента — единственное, что модель
  прочитает про параметр. Пишите так, чтобы модель поняла формат.
- `cfg` в схему для LLM не попадает. В теле это уже провалидированная
  модель, не dict.
- Возврат всегда `(content, artifact)`: `content` — текст для LLM,
  `artifact` — модель семейства `ToolResult` (`TextResult`, `TableResult`,
  `ChartResult`, ...). `pack_result` собирает пару из одного артефакта.
- `EXPECTED` — карта «исключение → kind отказа». Такое исключение уезжает
  конвертом `ReplyError(kind, message)` и показывается пользователю текстом;
  всё прочее — дефект, трейсбек в stderr и код выхода не ноль.
- `TOOLS` и блок `__main__` делают модуль запускаемой программой: ту же
  команду `python -m ... <имя> --флаги` исполняет и launcher, и человек.

### Шаг 4. Манифест

```python
"""Манифест плагина wordcount: entry point группы boba.tools."""
# plugin.py
from typing import Final

from boba.tool.wordcount.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(section="wordcount", tools=tuple(TOOLS))
```

`section` — идентификатор плагина. Когда приложение стартует, оно загружает основной конфиг приложения `config.toml`, а дальше выполняет поиск плагинов по entrypoint'ам внутри установленных пакетов. Каждый плагин рассматривается как tool у которого есть свой собственный под конфиг, который ищется приложением в локации `conf/plugins/<section>.toml`. Далее загружает этот файл как если бы, содержимое этого файла находилось в основном конфиге приложения в секции `[tool.<section>]`. Для тебя это означает, что можно использовать интерполяцию и в конфиге plugin'а использовать ключи из основного конфига приложения, например вот так можно отрендерить порт приложения `${env.port}`

### Шаг 5. pyproject

```toml
# pyproject.toml
[project]
name = "boba-tool-wordcount"
version = "0.0.15.dev6"
dependencies = ["boba-toolkit==0.0.15.dev6"]

[project.entry-points."boba.tools"]
wordcount = "boba.tool.wordcount.plugin:MANIFEST"

[project.optional-dependencies]
payload = []

[tool.boba.sandbox]
imports = ["boba.tool.wordcount.tools"]
```

- `payload` — зависимости тела tools'а (различные клиенты, например psycopg2 или clickhouse драйверы, парсеры, например bs4, markdownlify и прочее). Основное приложение (его называют Хост) не знает об этих зависимостях ничего и никогда не ставит их у себя. Таким образом каждый tool это упакованный мини проект на python с pyproject.toml, entrypoint и функциями, со своими зависимостями и логикой, которую можно запустить и проверить без участия LLM.
- `[tool.boba.sandbox]` — это описание для release-сборщика. Ему необходимо знать какие python-пакеты нужно поставить внутрь изолированной песочницы, какие apt/yum зависимости (к примеру для распознавания текста используется tessdata, а для чтения pdf/xlsx/odt/docs и прочего нативный пакет liteparce); подробно в
  разделе 7. `imports` — смоук-проверка образа после сборки. Запускает release-сборщик косле того как sandbox твоего плагина будет собран. Позволяет тебе как разработчику плагина определить логику проверки работоспособности пакета после сборки. Например можно попробовать выполнить `import psycopg3` в плагине для работы с postgres и проверить таким образом, установился ли psycopg3 внутрь песочницы

### Шаг 6. Файл конфига

Плагин не стартует без файла конфигураций - `conf/plugins/wordcount.toml is missing`. Файл кладётся в каждое
развёртывание, где плагин установлен: `compose/chainlit/conf/plugins/` и
`compose/studio/conf/plugins/`.

```toml
enable    = true
tools     = ["wordcount"]
max_chars = 200000
top       = 20

[sandbox]
```

- `enable` — выключенная секция не загружается вовсе.
- `tools` — allowlist имён инструментов которые доступны llm. Инструменты, которых нет в списке, LLM не получит, даже если они есть в `TOOLS`. Так администратор контролирует список доступных инструментов.
- Остальные ключи конфига — это поля вашей модели (`WordCountConfig`).
- `[sandbox]` — особенности изоляции в sandbox-режиме запуска: по умолчанию нет сети,
- `tools` — allowlist имён инструментов которые доступны llm. Инструменты, которых нет в списке, LLM не получит, даже если они есть в `TOOLS`. Так администратор контролирует список доступных инструментов.
- Остальные ключи конфига — это поля вашей модели (`WordCountConfig`).
- `[sandbox]` — особенности изоляции в sandbox-режиме запуска: по умолчанию нет сети,
  нет воркспейса, 1 GiB памяти. Пустая таблица значит «дефолт». Все ключи
  в разделе 5.6.

### Шаг 7. Что находится в cfg и откуда берётся каждый параметр

Команда тела собирается из подписи функции. У `wordcount` два параметра,
и каждый приезжает своим путём:

```python
async def wordcount(
    text: Annotated[str, Field(...)],            # LLM-аргумент  → флаг argv  --text
    cfg:  Annotated[WordCountConfig, Injected],  # injected      → JSON-канал, ключ "cfg"
)
```

| Параметр подписи | Признак | Откуда значение | Как едет в тело |
|---|---|---|---|
| `text` | нет маркера `Injected` | его придумала LLM в tool_call (или разработчик при ручном запуске) | флаг `--text "..."`: имя параметра, `_` → `-` |
| `cfg` | есть маркер `Injected` | хост читает `SECTION` модели, зовёт `bind(config, "tool.wordcount", WordCountConfig)`, pydantic превращает toml в модель | один JSON-объект `{"cfg": {...}}`, ключ равен имени параметра, канал `--injected-fd` |

Вызов LLM `wordcount(text="a b a")` хост (`ToolArgv.render`) превращает в:
### Шаг 7. Что находится в cfg и откуда берётся каждый параметр

Команда тела собирается из подписи функции. У `wordcount` два параметра,
и каждый приезжает своим путём:

```python
async def wordcount(
    text: Annotated[str, Field(...)],            # LLM-аргумент  → флаг argv  --text
    cfg:  Annotated[WordCountConfig, Injected],  # injected      → JSON-канал, ключ "cfg"
)
```

| Параметр подписи | Признак | Откуда значение | Как едет в тело |
|---|---|---|---|
| `text` | нет маркера `Injected` | его придумала LLM в tool_call (или разработчик при ручном запуске) | флаг `--text "..."`: имя параметра, `_` → `-` |
| `cfg` | есть маркер `Injected` | хост читает `SECTION` модели, зовёт `bind(config, "tool.wordcount", WordCountConfig)`, pydantic превращает toml в модель | один JSON-объект `{"cfg": {...}}`, ключ равен имени параметра, канал `--injected-fd` |

Вызов LLM `wordcount(text="a b a")` хост (`ToolArgv.render`) превращает в:

```
argv:      python3 -m boba.tool.wordcount.tools wordcount --text "a b a"
                                                 ^^^^^^^^^ ^^^^^^^^^^^^^^
                                                 имя тула  параметр text
                                                 ^^^^^^^^^ ^^^^^^^^^^^^^^
                                                 имя тула  параметр text
injected:  {"cfg": {"max_chars": 200000, "top": 20}}
            ^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            параметр cfg   поля WordCountConfig из conf/plugins/wordcount.toml
```

Будь в подписи третий LLM-аргумент `top_n: int`, появился бы флаг
`--top-n 5`; будь второй injected-параметр `limits: Annotated[LimitsConfig,
Injected]`, в JSON появился бы второй ключ `"limits"` со своей секцией.

Тело делает обратное (`ToolArgv.parse`): флаг `--text` находит поле `text`
схемы и валидирует значение его типом; ключ `"cfg"` из JSON валидируется в
`WordCountConfig`. Дальше `await wordcount(text=..., cfg=...)`. Нет ключа
`cfg` или значение не проходит модель — `ReplyError(kind="invalid_request")`.
            ^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            параметр cfg   поля WordCountConfig из conf/plugins/wordcount.toml
```

Будь в подписи третий LLM-аргумент `top_n: int`, появился бы флаг
`--top-n 5`; будь второй injected-параметр `limits: Annotated[LimitsConfig,
Injected]`, в JSON появился бы второй ключ `"limits"` со своей секцией.

Тело делает обратное (`ToolArgv.parse`): флаг `--text` находит поле `text`
схемы и валидирует значение его типом; ключ `"cfg"` из JSON валидируется в
`WordCountConfig`. Дальше `await wordcount(text=..., cfg=...)`. Нет ключа
`cfg` или значение не проходит модель — `ReplyError(kind="invalid_request")`.

### Шаг 8. Проверка руками

Запуск руками — та же команда, только вместо канала `--injected-fd` от
launcher'а вы даёте файл `--injected`, а результат читаете из stdout.
Служебные флаги, которых нет в подписи функции:

| Флаг | Кто передаёт | Зачем |
|---|---|---|
| `--injected <файл>` | человек | JSON с injected-параметрами; тот же объект, что launcher шлёт по `--injected-fd` |
| `--injected-fd <n>` | launcher | номер дескриптора с тем же JSON |
| `--fd-result <n>` | launcher | куда писать конверт; без него `content` печатается в stdout |
| `--artifact` | человек | вдобавок к `content` напечатать JSON артефакта (`TableResult` и т.п.) |
Запуск руками — та же команда, только вместо канала `--injected-fd` от
launcher'а вы даёте файл `--injected`, а результат читаете из stdout.
Служебные флаги, которых нет в подписи функции:

| Флаг | Кто передаёт | Зачем |
|---|---|---|
| `--injected <файл>` | человек | JSON с injected-параметрами; тот же объект, что launcher шлёт по `--injected-fd` |
| `--injected-fd <n>` | launcher | номер дескриптора с тем же JSON |
| `--fd-result <n>` | launcher | куда писать конверт; без него `content` печатается в stdout |
| `--artifact` | человек | вдобавок к `content` напечатать JSON артефакта (`TableResult` и т.п.) |

```bash
cat > /tmp/wc.json <<'EOF'
{"cfg": {"max_chars": 1000, "top": 3}}
EOF


.venv/bin/python -m boba.tool.wordcount.tools wordcount \
    --text "a b a c" \
    --injected /tmp/wc.json \
    --artifact
```

Здесь `--text "a b a c"` — LLM-аргумент `text`; `--injected /tmp/wc.json` —
injected-параметр `cfg` (файл с ключом `"cfg"` и полями `WordCountConfig`);
`--artifact` — служебный флаг «показать ещё и артефакт».

Подсказку по флагам конкретного инструмента печатает
`python -m boba.tool.wordcount.tools wordcount --help`: в ней перечислены
только LLM-аргументы, injected-параметры в argv не принимаются.

Здесь `--text "a b a c"` — LLM-аргумент `text`; `--injected /tmp/wc.json` —
injected-параметр `cfg` (файл с ключом `"cfg"` и полями `WordCountConfig`);
`--artifact` — служебный флаг «показать ещё и артефакт».

Подсказку по флагам конкретного инструмента печатает
`python -m boba.tool.wordcount.tools wordcount --help`: в ней перечислены
только LLM-аргументы, injected-параметры в argv не принимаются.

По toml приложения, injected собирает CLI хоста:

```bash
.venv/bin/python -m boba.runtime.toolcli boba.tool.wordcount.tools wordcount \
    --text "a b a c" --config compose/chainlit/conf/config.toml
```

Через приложение: `uv sync --all-packages`, тест обнаружения
`packages/services/boba-runtime/tests/test_plugin_discovery.py`, дальше
интеграционный тест по образцу `packages/tools/boba-tool-doc/tests/test_run_doc.py`.

Живой пример: `packages/tools/boba-tool-doc/` — `DocToolSection` с
`SECTION = "tool.doc"`, файл `compose/chainlit/conf/plugins/doc.toml`.
Плагин без injected вовсе: `packages/tools/boba-tool-chart/`.

---

## 3. Пример 2. Injected-конфиг с секретом

Инструмент `weather`: ходит во внешний API по ключу. Ключ лежит в конфиге
и обязан доехать до тела, но не попасть ни в argv, ни в лог, ни в дамп.

### 3.1. Как секрет живёт в модели и что сделать, чтобы он доехал до тела

Секретное поле объявляется типом `SecretStr`. Сам по себе он только
маскирует: `repr`, `model_dump`, трейсбек показывают `**********`. Чтобы
секрет **раскрылся** при отправке в тело и **собрался обратно** в
`SecretStr` на его стороне, нужно ровно три шага:

1. **Объявить поле типом `SecretStr`**, а не `str`:
   `api_key: SecretStr = Field(min_length=1)`. На любой глубине: во
   вложенной модели, в `dict[str, SecretStr]`, в списке моделей.
2. **Унаследовать модель конфига от `SecretRevealing`**
   (`boba.toolkit.types`). Это даёт метод `revealed()`, который хост зовёт
   вместо обычного дампа: он дампит модель с контекстом
   `{"reveal_secrets": True}` и обходом заменяет каждый голый `SecretStr`
   открытой строкой (`SecretReveal`). Никаких сериализаторов писать не
   нужно.
3. **В теле читать секрет только через `get_secret_value()`** в момент
   использования (заголовок, параметр `connect`), не сохраняя строку в
   переменные и не подставляя её в сообщения об ошибках.

Так это выглядит целиком:
### 3.1. Как секрет живёт в модели и что сделать, чтобы он доехал до тела

Секретное поле объявляется типом `SecretStr`. Сам по себе он только
маскирует: `repr`, `model_dump`, трейсбек показывают `**********`. Чтобы
секрет **раскрылся** при отправке в тело и **собрался обратно** в
`SecretStr` на его стороне, нужно ровно три шага:

1. **Объявить поле типом `SecretStr`**, а не `str`:
   `api_key: SecretStr = Field(min_length=1)`. На любой глубине: во
   вложенной модели, в `dict[str, SecretStr]`, в списке моделей.
2. **Унаследовать модель конфига от `SecretRevealing`**
   (`boba.toolkit.types`). Это даёт метод `revealed()`, который хост зовёт
   вместо обычного дампа: он дампит модель с контекстом
   `{"reveal_secrets": True}` и обходом заменяет каждый голый `SecretStr`
   открытой строкой (`SecretReveal`). Никаких сериализаторов писать не
   нужно.
3. **В теле читать секрет только через `get_secret_value()`** в момент
   использования (заголовок, параметр `connect`), не сохраняя строку в
   переменные и не подставляя её в сообщения об ошибках.

Так это выглядит целиком:

```python
"""Инструмент weather: прогноз по внешнему API.

Ошибки:
WeatherRequestError — API недоступен или ответил статусом.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import httpx
from pydantic import ConfigDict, Field, SecretStr

from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TextResult, ToolResult, pack_result
from boba.toolkit.types import SecretRevealing


class WeatherRequestError(Exception):
    """API недоступен или ответил статусом; текст готов для пользователя."""


class WeatherConfig(SecretRevealing):
    """Адрес и ключ API; секция [tool.weather]."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.weather"

    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(min_length=1)
    timeout_sec: float = Field(default=10.0, gt=0)
```

Что происходит и что будет, если шаг пропустить:

- Хост на отправке (`ToolArgv.reveal`) смотрит, есть ли у значения метод
  `revealed()`. Есть — зовёт его. Нет — обычный `dump_python(mode="json")`,
  где pydantic дампит `SecretStr` как `**********`. Пропущен шаг 2 — тело
  соберёт модель успешно, получит звёздочки вместо ключа и упадёт
  непонятной 401 на первом запросе, далеко от причины.
- На стороне тела ничего делать не нужно: `ToolArgv.parse` валидирует JSON
  в ту же модель, и открытая строка снова становится `SecretStr`. Поэтому
  в теле секрет читается как обычно — `cfg.api_key.get_secret_value()`.
- Ключ контекста один на весь проект: `SecretRevealing.REVEAL_CONTEXT`
  (`"reveal_secrets"`). Свою строку не придумывайте.

Единственное исключение: поле, у которого объявлен свой
`@field_serializer`, обход не трогает — считается, что у поля своя
политика. Сериализатор нужен только там, где поле должно вести себя
иначе, чем «раскрыть при отправке в тело»: например, пароль
`KerberosPasswordAuth` маскируется всегда, потому что в песочницу едет
билет, а не он. Для обычного секрета сериализатор писать не надо.
```

Что происходит и что будет, если шаг пропустить:

- Хост на отправке (`ToolArgv.reveal`) смотрит, есть ли у значения метод
  `revealed()`. Есть — зовёт его. Нет — обычный `dump_python(mode="json")`,
  где pydantic дампит `SecretStr` как `**********`. Пропущен шаг 2 — тело
  соберёт модель успешно, получит звёздочки вместо ключа и упадёт
  непонятной 401 на первом запросе, далеко от причины.
- На стороне тела ничего делать не нужно: `ToolArgv.parse` валидирует JSON
  в ту же модель, и открытая строка снова становится `SecretStr`. Поэтому
  в теле секрет читается как обычно — `cfg.api_key.get_secret_value()`.
- Ключ контекста один на весь проект: `SecretRevealing.REVEAL_CONTEXT`
  (`"reveal_secrets"`). Свою строку не придумывайте.

Единственное исключение: поле, у которого объявлен свой
`@field_serializer`, обход не трогает — считается, что у поля своя
политика. Сериализатор нужен только там, где поле должно вести себя
иначе, чем «раскрыть при отправке в тело»: например, пароль
`KerberosPasswordAuth` маскируется всегда, потому что в песочницу едет
билет, а не он. Для обычного секрета сериализатор писать не надо.

### 3.2. Как секрет читается в теле

Только через `get_secret_value()` и только в момент, когда значение реально
уходит наружу. Хранить распакованную строку в переменной «на потом» не
надо: объект `SecretStr` маскируется в трейсбеке, строка — нет.

```python
@tool
async def weather(
    city: Annotated[str, Field(min_length=1, description="Город")],
    cfg: Annotated[WeatherConfig, Injected],
) -> tuple[str, ToolResult]:
    """Текущая погода в городе по внешнему API."""
    headers = {"Authorization": f"Bearer {cfg.api_key.get_secret_value()}"}

    url = f"{cfg.base_url}/now"

    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
            response = await client.get(url, params={"q": city}, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"weather request failed: {type(exc).__name__}: {exc}"
        raise WeatherRequestError(msg) from exc

    return pack_result(TextResult(text=response.text))
```

Обратите внимание на `except httpx.HTTPError` с `raise ... from exc`:
текст исключения httpx не содержит заголовков, поэтому ключ в сообщение
пользователю не попадает. Не форматируйте в сообщение ни `headers`, ни
сам `cfg`.

### 3.3. Секрет в toml

В файл плагина секрет напрямую писать не надо. Конфиги плагинов желательно делать одинаковые во всех местах, где работает приложение.
Секрет нужно положить в основной конфиг приложения - `config.toml`, например в секцию `[site]`, а уже в конфиге плагина ссылается на него интерполяцией:
В файл плагина секрет напрямую писать не надо. Конфиги плагинов желательно делать одинаковые во всех местах, где работает приложение.
Секрет нужно положить в основной конфиг приложения - `config.toml`, например в секцию `[site]`, а уже в конфиге плагина ссылается на него интерполяцией:

```toml
# compose/chainlit/conf/plugins/weather.toml
enable   = true
tools    = ["weather"]
base_url = "https://api.weather.example/v1"
api_key  = "${site.weather_api_key}"

[sandbox]
    network = true
    binds   = ["/etc/resolv.conf:/etc/resolv.conf", "/etc/hosts:/etc/hosts"]
```

`network = true` обязателен для того, что бы внутри sandbox был интернет.
Без него песочница поднимается с `--unshare-net`, и никакой API недоступен.
Бинды `resolv.conf` и `hosts` нужны для резолвинга dns имён внутри песочницы.
`network = true` обязателен для того, что бы внутри sandbox был интернет.
Без него песочница поднимается с `--unshare-net`, и никакой API недоступен.
Бинды `resolv.conf` и `hosts` нужны для резолвинга dns имён внутри песочницы.

Целая секция подключается ссылкой: `confluence = "${web.wiki}"` берёт
таблицу `[web.wiki]` из `config.toml` вместе с её auth-частью.
Целая секция подключается ссылкой: `confluence = "${web.wiki}"` берёт
таблицу `[web.wiki]` из `config.toml` вместе с её auth-частью.

### 3.4. Kerberos в статическом конфиге

Если в injected-конфиге лежит профиль с kerberos-секцией keytab (как
`connection = "${postgres}"` у `kb` и `ingest`), keytab в песочницу не
уезжает никогда. Работает так:

1. На загрузке `ServiceTickets.bind_all` смотрит, есть ли внутри
   статического значения профиль с `KeytabAuth`, `KerberosPasswordAuth`
   или `DelegatedAuth` (`ProfileSections.needs_arming`). Есть — ставит
   обвязку на этот параметр.
2. На вызове обвязка выпускает **один сервисный билет** к
   `profile.service_name()` из keytab (`ServiceTicketIssuer`) и подменяет
   секцию на `TicketAuth` (`profile.with_call_ticket(ticket)`).
   `DelegatedAuth` в статическом конфиге — ошибка `ToolConfigError`:
   делегировать тут некому, сессии пользователя нет.
3. Дальше обычный `revealed()`: `TicketAuth.ccache` (base64 FILE-ccache с
   одним билетом) раскрывается по контексту. Если бы до дампа дошёл
   keytab, `KerberosDump.json` упал бы с
   `credentials may not leave the application` — это fail-closed, а не баг.
4. В теле `ClientCredentials.of(auth)` даёт `TicketCredentials`;
   внутри `applied_async()` байты кладутся во временный файл (в песочнице
   это приватный tmpfs вызова), `KRB5CCNAME` указывает на него, по выходу
   файл удаляется. TGT в ccache нет: выпустить билет к другому сервису
   тело не может.

Для этого профиль обязан быть наследником `ConnectionProfileBase` с
реализованными `kerberos_section()`, `service_name()`, `with_call_ticket()`.
Как их писать — раздел 4.2.

Живые примеры: `packages/tools/boba-tool-knowledge/src/boba/tool/kb/confluence/tools.py`
(`ConfluenceToolsConfig` с `HttpConnection`), `kb/tools.py` (`KbToolConfig` с
`connection: PostgresConfig` и `@warmup`), стендовый
`packages/testing/boba-stand/src/boba/stand/fake_toolmod.py` (`FakeConfig`:
голый `SecretStr` под `SecretRevealing`, без сериализаторов). Тесты
раскрытия — `packages/core/boba-toolkit/tests/test_secret_reveal.py`.
`packages/testing/boba-stand/src/boba/stand/fake_toolmod.py` (`FakeConfig`:
голый `SecretStr` под `SecretRevealing`, без сериализаторов). Тесты
раскрытия — `packages/core/boba-toolkit/tests/test_secret_reveal.py`.

---

## 4. Пример 3. Инструмент с connection пользователя
## 4. Пример 3. Инструмент с connection пользователя

Connection лежат в таблице, которые выдаются пользователям или ролям,
и на каждый tool call, хост собирает whitelist доступных connection.
Connection лежат в таблице, которые выдаются пользователям или ролям,
и на каждый tool call, хост собирает whitelist доступных connection.

### 4.1. Откуда берутся connection пользователя
### 4.1. Откуда берутся connection пользователя

Таблицы `connections` (id, name, data jsonb с зашифрованным профилем),
`roles`, `grants` содержат логику доступных connection для пользователей и ролей.
Секретная часть connection шифруется `SecretCipher` ключом из конфига - 
`[connections] encryption_key`

Каждый connection должен иметь поле **kind** - дискриминатор типа connection'а.
По полю kind реестр типов (`ConnectionTypes`, entry points `boba.connections`) находит модель, которую натягивают на содержимое connection. 
Поэтому у tool с connection всегда две части:

- **тип connection**'а: модель профиля и проба, entry point `boba.connections`
  — раздел 4.2;
- **tool-плагин**: инструменты, которые на тип ссылаются, entry point
  `boba.tools` — разделы 4.3–4.6.

Части разные, а вот пакетов может быть один или два: это ваш выбор, и
следующий раздел объясняет, как выбрать.

### 4.1.1. Один пакет или два

Раскладка ни на что в рантайме не влияет: реестры типов и инструментов
независимы, и приложение находит оба entry point у любого установленного
пакета. Разница только в том, кто и что вынужден ставить себе.

**Вариант A: два пакета.** Тип живёт в инфра-пакете (`packages/infra/db/…`,
`packages/infra/transport/…`), инструменты — в `packages/tools/…`, второй
зависит от первого. Так сделаны postgres, clickhouse и web.

```
packages/infra/db/boba-db-redis/         # тип: профиль + клиент + проба
    pyproject.toml                       # [project.entry-points."boba.connections"]
    src/boba/db/redis/{profile,client,connection}.py

packages/tools/boba-tool-redis/          # инструменты поверх типа
    pyproject.toml                       # [project.entry-points."boba.tools"]
                                         # dependencies = ["boba-db-redis==…"]
    src/boba/tool/redis/{tools,plugin}.py
```

**Вариант B: один пакет.** Пакет объявляет оба entry point сразу:

```
packages/tools/boba-tool-redis/
    pyproject.toml
    src/boba/tool/redis/
        profile.py       # модель профиля
        client.py        # клиент
        connection.py    # MANIFEST типа   -> boba.connections
        tools.py         # инструменты
        plugin.py        # MANIFEST плагина -> boba.tools
```

```toml
[project]
name = "boba-tool-redis"
dependencies = [
    "boba-toolkit==0.0.15.dev6",
    "boba-connections==0.0.15.dev6",
    "redis>=5",
]

[project.entry-points."boba.connections"]
redis = "boba.tool.redis.connection:MANIFEST"

[project.entry-points."boba.tools"]
redis = "boba.tool.redis.plugin:MANIFEST"
```

Имя entry point в первой группе обязано совпадать с `kind` профиля, во
второй — быть идентификатором секции (`tool.redis`, файл
`conf/plugins/redis.toml`). Совпадение имён между группами — совпадение
случайное и ни на что не влияет.

**Что важно учесть в варианте B.** Модуль с манифестом типа импортируется
при старте приложения: `ConnectionTypes.discover()` зовёт `entry.load()` без
обработки ошибок, и неудачный импорт роняет запуск. Поэтому всё, что этот
модуль тянет на уровне модуля, обязано лежать в обычных зависимостях
пакета, а не в `payload`. Клиент базы сюда попадает неизбежно: проба ходит в
систему по-настоящему, значит библиотека нужна и приложению, а не только
телу в песочнице. Если клиент тяжёлый, это заметная плата за компактность.

Второе следствие: тип начинает жить и умирать вместе с плагином. Приложения
ставят tool-пакеты дополнительной группой `tools` (она объявлена и у
chainlit, и у studio), поэтому entry point виден. Но сборка без этой группы
останется и без типа, а строки таких соединений в списках получат пометку
«type not installed».

**Как выбрать.**

| Берите один пакет | Берите два пакета |
|---|---|
| типом пользуется только этот плагин | типом пользуется несколько плагинов |
| клиент лёгкий, приложению его не жалко | клиент тяжёлый и приложению не нужен |
| инфра-пакета для этой системы ещё нет | инфра-пакет уже есть — добавляйте тип туда |
| | тип нужен studio отдельно от инструментов |

Перейти с одного пакета на два потом несложно: код профиля и пробы
переезжает в новый пакет, entry point `boba.connections` — вместе с ним,
а tool-пакет получает зависимость. Значение `kind` при этом не меняется,
поэтому строки в базе править не нужно.

Дальше в примере показан вариант A как более общий; при варианте B меняются
только пути файлов и `pyproject`, содержимое остаётся тем же.
`roles`, `grants` содержат логику доступных connection для пользователей и ролей.
Секретная часть connection шифруется `SecretCipher` ключом из конфига - 
`[connections] encryption_key`

Каждый connection должен иметь поле **kind** - дискриминатор типа connection'а.
По полю kind реестр типов (`ConnectionTypes`, entry points `boba.connections`) находит модель, которую натягивают на содержимое connection. 
Поэтому у tool с connection всегда две части:

- **тип connection**'а: модель профиля и проба, entry point `boba.connections`
  — раздел 4.2;
- **tool-плагин**: инструменты, которые на тип ссылаются, entry point
  `boba.tools` — разделы 4.3–4.6.

Части разные, а вот пакетов может быть один или два: это ваш выбор, и
следующий раздел объясняет, как выбрать.

### 4.1.1. Один пакет или два

Раскладка ни на что в рантайме не влияет: реестры типов и инструментов
независимы, и приложение находит оба entry point у любого установленного
пакета. Разница только в том, кто и что вынужден ставить себе.

**Вариант A: два пакета.** Тип живёт в инфра-пакете (`packages/infra/db/…`,
`packages/infra/transport/…`), инструменты — в `packages/tools/…`, второй
зависит от первого. Так сделаны postgres, clickhouse и web.

```
packages/infra/db/boba-db-redis/         # тип: профиль + клиент + проба
    pyproject.toml                       # [project.entry-points."boba.connections"]
    src/boba/db/redis/{profile,client,connection}.py

packages/tools/boba-tool-redis/          # инструменты поверх типа
    pyproject.toml                       # [project.entry-points."boba.tools"]
                                         # dependencies = ["boba-db-redis==…"]
    src/boba/tool/redis/{tools,plugin}.py
```

**Вариант B: один пакет.** Пакет объявляет оба entry point сразу:

```
packages/tools/boba-tool-redis/
    pyproject.toml
    src/boba/tool/redis/
        profile.py       # модель профиля
        client.py        # клиент
        connection.py    # MANIFEST типа   -> boba.connections
        tools.py         # инструменты
        plugin.py        # MANIFEST плагина -> boba.tools
```

```toml
[project]
name = "boba-tool-redis"
dependencies = [
    "boba-toolkit==0.0.15.dev6",
    "boba-connections==0.0.15.dev6",
    "redis>=5",
]

[project.entry-points."boba.connections"]
redis = "boba.tool.redis.connection:MANIFEST"

[project.entry-points."boba.tools"]
redis = "boba.tool.redis.plugin:MANIFEST"
```

Имя entry point в первой группе обязано совпадать с `kind` профиля, во
второй — быть идентификатором секции (`tool.redis`, файл
`conf/plugins/redis.toml`). Совпадение имён между группами — совпадение
случайное и ни на что не влияет.

**Что важно учесть в варианте B.** Модуль с манифестом типа импортируется
при старте приложения: `ConnectionTypes.discover()` зовёт `entry.load()` без
обработки ошибок, и неудачный импорт роняет запуск. Поэтому всё, что этот
модуль тянет на уровне модуля, обязано лежать в обычных зависимостях
пакета, а не в `payload`. Клиент базы сюда попадает неизбежно: проба ходит в
систему по-настоящему, значит библиотека нужна и приложению, а не только
телу в песочнице. Если клиент тяжёлый, это заметная плата за компактность.

Второе следствие: тип начинает жить и умирать вместе с плагином. Приложения
ставят tool-пакеты дополнительной группой `tools` (она объявлена и у
chainlit, и у studio), поэтому entry point виден. Но сборка без этой группы
останется и без типа, а строки таких соединений в списках получат пометку
«type not installed».

**Как выбрать.**

| Берите один пакет | Берите два пакета |
|---|---|
| типом пользуется только этот плагин | типом пользуется несколько плагинов |
| клиент лёгкий, приложению его не жалко | клиент тяжёлый и приложению не нужен |
| инфра-пакета для этой системы ещё нет | инфра-пакет уже есть — добавляйте тип туда |
| | тип нужен studio отдельно от инструментов |

Перейти с одного пакета на два потом несложно: код профиля и пробы
переезжает в новый пакет, entry point `boba.connections` — вместе с ним,
а tool-пакет получает зависимость. Значение `kind` при этом не меняется,
поэтому строки в базе править не нужно.

Дальше в примере показан вариант A как более общий; при варианте B меняются
только пути файлов и `pyproject`, содержимое остаётся тем же.

### 4.2. Тип соединения: профиль, проба, entry point

Пакет `packages/infra/db/boba-db-redis`. Если инфра-пакет для системы уже
есть — добавляйте туда.

**Профиль** `src/boba/db/redis/profile.py`:

```python
"""Профиль соединения redis."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, SecretStr

from boba.connections.base import ConnectionProfileBase


class RedisConfig(ConnectionProfileBase):
    """Подключение к redis: адрес, база и пароль."""

    kind: Literal["redis"] = Field(
        default="redis",
        description="Дискриминатор соединения при хранении в базе.",
    )

    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1)
    db: int = Field(default=0, ge=0)
    password: SecretStr = Field(min_length=1)
    client_name: str = Field(default="")

    def trace(self) -> str:
        return f"auth=password host={self.host} db={self.db}"

    def labeled(self, label: str) -> Self:
        return self.model_copy(update={"client_name": label})
```

Что и зачем:

- `kind: Literal["redis"]` — по нему строка из базы находит свою модель.
  Хранится в jsonb, менять потом нельзя.
- `trace()` обязателен: строка журнала «под кем идём». Пишется по профилю,
  который реально уедет в тело, поэтому у kerberos-строк здесь виден билет
  вызова, а не keytab.
- `labeled(label)` — если сервер умеет подписывать сессию именем клиента
  (`application_name` у postgres, `CLIENT SETNAME` у redis). Хост подставит
  `boba:<логин>:<инструмент>`, чтобы DBA видел, кто пришёл.
- `password: SecretStr` — и больше ничего: в базе поле шифруется само
  (`SecretCipher` обходит значения), а в тело раскрывается обходом
  `SecretReveal` из конфига инструмента (раздел 3.1). Профиль наследовать
  от `SecretRevealing` не нужно: контекст приходит снаружи, от модели
  секции.
- `password: SecretStr` — и больше ничего: в базе поле шифруется само
  (`SecretCipher` обходит значения), а в тело раскрывается обходом
  `SecretReveal` из конфига инструмента (раздел 3.1). Профиль наследовать
  от `SecretRevealing` не нужно: контекст приходит снаружи, от модели
  секции.
- Kerberos. Если тип умеет kerberos, реализуйте ещё три метода базового
  класса. `kerberos_section()` — где в профиле лежит секция
  (`self.auth`, если это `KerberosAuthBase`); `service_name()` — SPN в
  форме `service@host`, к которому выпускать билет; `with_call_ticket(ticket)`
  — копия профиля с `TicketAuth` на месте секции. Без них хост при виде
  kerberos-секции упадёт `ConnectionTypeError`. Живой образец —
  `packages/infra/db/boba-db-postgres/src/boba/db/postgres/profile/config.py`.
  Обратная сторона в теле: `ClientCredentials.of(auth)` и `applied_async()`
  вокруг открытия соединения, как в `boba.db.postgres.payload`.

**Манифест с пробой** `src/boba/db/redis/connection.py`. Проба — это кнопка
«Check» на странице соединений:

```python
"""Тип соединения redis: манифест для реестра boba.connections."""

from boba.connections.base import ConnectionProfileBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.redis.client import open_redis
from boba.db.redis.profile import RedisConfig

__all__ = ["MANIFEST"]


async def _probe(profile: ConnectionProfileBase) -> str:
    if not isinstance(profile, RedisConfig):
        raise ConnectionTypeError(f"redis probe got a {profile.kind!r} profile")

    client = await open_redis(profile)
    try:
        pong = await client.ping()
    finally:
        await client.close()

    return f"PONG {pong}"


MANIFEST = ConnectionTypeManifest(kind="redis", profile=RedisConfig, probe=_probe)
```

Исключения пробы глотать не нужно: граница превратит их в
`ProbeResult(ok=False, message=...)`.

**Entry point** в `pyproject.toml` пакета-владельца; имя обязано совпадать
с `kind`, реестр проверяет это на старте:

```toml
[project.entry-points."boba.connections"]
redis = "boba.db.redis.connection:MANIFEST"
```

Проверка после `uv sync --all-packages`:

```bash
.venv/bin/python -c "from boba.connections.manifest import ConnectionTypes; print(ConnectionTypes.discover().kinds())"
```

Страница «Соединения» покажет тип сама: форма строится из json-schema
реестра. Если пакет типа потом удалить, строки его kind в списках
помечаются «type not installed», использование падает
`UnknownConnectionKindError`.

### 4.3. Конфиг инструмента: подкласс `SqlProfiles` или `WebConnection`

Сначала о том, зачем этот базовый класс вообще нужен, потому что вопрос
законный: секреты уже умеет `SecretStr`, конфиг уже собирается из toml, что
ещё?

`SecretStr` и `SqlProfiles` отвечают на разные вопросы. `SecretStr` — на
вопрос «как значение доедет до тела и не утечёт по дороге». `SqlProfiles` —
на вопрос «откуда вообще возьмётся профиль на этот конкретный вызов». В
обычном инструменте профиль берётся из toml и одинаков для всех
пользователей. В инструменте с соединениями пользователя его в toml нет
вовсе: он приходит из таблицы, зависит от того, кто сейчас пишет в чат, и
собирается заново на каждый вызов. `SqlProfiles` — это способ сказать хосту:
«вот сюда клади соединения пользователя».

```python
class RedisToolConfig(SecretRevealing, SqlProfiles[RedisConfig]):
    """Whitelist соединений и лимиты redis-инструментов; [tool.redis]."""

    SECTION: ClassVar[str] = "tool.redis"
```

Дальше — что конкретно даёт наследование, по кейсам.

**Кейс 1. Без него подстановка не включится вообще.** На загрузке хост
перебирает injected-параметры инструментов и вешает обвязку только на те,
чья модель прошла проверку:

```python
@staticmethod
def _accepts(base: object) -> bool:
    return isinstance(base, SqlProfiles | WebConnection)
```

Это `isinstance`, а не «есть ли поле profiles». Своя модель с точно таким же
полем проверку не пройдёт, обвязка не встанет, и инструмент будет запускаться
со статическим конфигом из toml. Диагностировать это неприятно: ошибок нет,
логов нет, просто `cfg.profiles` всегда пустой и любое имя соединения даёт
«unknown target». Наследование здесь — не украшение, а объявление намерения,
которое хост читает.

**Кейс 2. Фиксированный контракт полей.** Хост не вызывает у вашей модели
никаких методов, он делает копию с подменой полей по именам:

```python
update = {"profiles": shipped, "names": sorted(whitelist.profiles)}
if isinstance(self._base, WebConnection):
    update["hosts"] = self._hosts(whitelist.profiles)

return self._base.model_copy(update=update)
```

Имена `profiles`, `names`, `hosts` и их типы — часть контракта. Базовый класс
существует ровно для того, чтобы этот контракт был объявлен в одном месте, а
не переписывался в каждом плагине по памяти. Опечались в имени поля — и
подстановка снова «работает», но данные уходят в никуда.

**Кейс 3. Готовый разбор запроса в теле с внятной ошибкой.** Тело пишет одну
строку:

```python
connection = cfg.resolve(connection_name)
```

За ней стоит поведение, которое иначе пришлось бы повторять в каждом
инструменте: если имени нет, поднимается `UnknownConnectionError` с текстом
`connection_name 'cache' is not in the whitelist (allowed=['main', 'reports'])`.
Этот текст уезжает в чат как ожидаемый отказ, и модель видит список
допустимых имён — то есть у неё есть шанс исправиться и повторить вызов. Если
писать самому, получится `KeyError` без подсказки, а это уже дефект: модель
не поймёт, что делать.

**Кейс 4. Разделение «что видно» и «что дано».** Поля два, и они не
дублируют друг друга: в `profiles` лежит ровно одно соединение, которое вызов
назвал, с паролем внутри; в `names` — все имена, доступные пользователю, без
профилей. Метод `targets_table()` собирает из них выдачу для
`*_connection_list`, и модель видит полный список, хотя в песочницу уехал
единственный секрет. Это тот самый принцип наименьших привилегий, и он
реализован в базовом классе, а не в каждом плагине.

**Кейс 5. Типизация.** `SqlProfiles` параметризован типом профиля, поэтому
`cfg.resolve(name)` возвращает `RedisConfig`, а не `Any`. Дальше pyright
проверяет, что вы передаёте в клиент именно те поля, которые у профиля есть.
С самодельным `dict[str, Any]` проверка типов заканчивается на первой же
строке тела.

**Кейс 6. Специфика web.** У `WebConnection` та же схема плюс третье поле
`hosts` и метод `resolve_for(connection_name, url)`, который проверяет, что
хост запрошенного URL покрывается выбранным соединением. Проверка нужна с
двух сторон: хост делает её до выпуска билета, тело — повторно, уже перед
запросом. В базовом классе она написана один раз.

**Когда этот базовый класс не нужен.** Если соединение задаёт администратор в
конфиге и оно одинаково для всех, брать `SqlProfiles` не надо — это лишняя
сущность. Так устроен поиск по базе знаний: соединение приходит ссылкой на
секцию, а конфиг — обычная модель.

```toml
# conf/plugins/kb.toml
connection = "${postgres}"
```

```python
class KbToolConfig(SecretRevealing, PostgresKnowledgeBaseConfig):
    """Конфиг kb-поиска: подключение, таблицы, эмбеддер; секция [tool.kb]."""

    SECTION: ClassVar[str] = "tool.kb"
```

Здесь `SecretRevealing` есть (секреты профиля должны доехать), а `SqlProfiles`
нет (выбирать пользователю нечего). Аргумента `connection_name` у таких
инструментов тоже нет.

Коротко, в одной таблице:

| Задача | Кто решает |
|---|---|
| секрет доедет до тела и не утечёт в argv и логи | `SecretStr` + `SecretRevealing` |
| в конфиг попадут соединения именно этого пользователя | наследование `SqlProfiles` / `WebConnection` |
| тело достанет профиль по имени и внятно откажет | `resolve()` из базового класса |
| модель узнает список доступных имён | `names` + `targets_table()` |
| keytab не уедет наружу, вместо него поедет билет вызова | обвязка кредов (раздел 4.9) |

Наследование `SecretRevealing` идёт первым в списке баз: оно включает
раскрытие секретов при отправке в тело (раздел 3.1). Профили лежат внутри
как обычные поля, поэтому их пароли раскрываются тем же обходом на любой
глубине: `profiles["cache"].password`, `profiles["main"].auth.password`,
токен внутри `HttpConnection`.

Готовые профили других пакетов вкладываются так же и работают без правок:
`SqlProfiles[PostgresConfig]` у pg, `SqlProfiles[ClickHouseConfig]` у ch,
`WebConnection` с `HttpConnection` у web. Отдельный случай — kerberos: keytab
и пароль kerberos наружу не уезжают никогда, вместо них хост подставляет
билет вызова (раздел 4.9).

### 4.4. Аргумент выбора соединения

```python
RedisConnection = Annotated[ConnectionName, ConnectionArg(family="redis")]
```

- `ConnectionName` из `boba.toolkit.sql` — `Annotated[str, Field(min_length=1,
  description="Имя подключения")]`. Для LLM это просто обязательная строка
  `connection_name`. Список допустимых значений модель получает отдельным
  инструментом `*_connection_list`, а не из схемы.
- Имя параметра обязано быть `connection_name`: это значение
  `ConnectionKeying.NAME`, по нему хост читает kwargs.
- `ConnectionArg(family=...)` — метаданные для страниц studio (виджет
  выбора соединения в workflow). На LLM и валидацию не влияет.

### 4.5. Тела

```python
@tool
async def redis_connection_list(
    cfg: Annotated[RedisToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Список доступных значений connection_name для redis-инструментов."""
    return pack_result(cfg.targets_table())


@tool
async def redis_query(
    connection_name: RedisConnection,
    command: Annotated[str, Field(min_length=1, description="Команда redis, например GET key")],
    cfg: Annotated[RedisToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Выполнить команду redis на выбранном соединении."""
    connection = cfg.resolve(connection_name)

    client = await open_redis(connection)
    try:
        reply = await client.execute_command(*command.split())
    finally:
        await client.close()

    return pack_result(TextResult(text=str(reply)))


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    RedisError: SqlErrorKind.DATABASE_UNAVAILABLE,
    UnknownConnectionError: SqlErrorKind.UNKNOWN_TARGET,
}
```

- `cfg.resolve(connection_name)` — единственная валидация имени в теле.
  Профиль есть только у соединения, которое вызов назвал; чужое имя даёт
  `UnknownConnectionError` с перечнем допустимых.
- Инструменты с соединениями обязаны быть `async def`: обвязка ждёт
  таблицу и билет, синхронный вызов падает `InjectedAsyncOnlyError`.
- `*_connection_list` нужен всегда: без него модель не узнает имён.

### 4.6. Манифест и конфиг развёртывания

```python
"""Манифест плагина redis: entry point группы boba.tools."""

from typing import Final

from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.whitelist import ConnectionKeying
from boba.db.redis.connection import MANIFEST as REDIS_CONNECTION
from boba.tool.redis.tools import TOOLS

MANIFEST: Final = ConnectedToolManifest(
    section="redis",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(REDIS_CONNECTION.kind, ConnectionKeying.NAME),
)
```

- `ConnectedToolManifest` вместо `ToolPluginManifest` — признак, что секция
  берёт соединения из таблицы.
- `UserConnectionsSpec.kind` — какой kind строк запрашивать у хранилища.
  Берётся из манифеста типа, а не строкой: опечатка тогда не пройдёт.
- `ConnectionKeying.NAME` — единственный ключ адресации сегодня: аргумент
  `connection_name` сопоставляется с колонкой `name`. Enum существует под
  будущие ключи.

Файл `conf/plugins/redis.toml`:

```toml
enable   = true
tools    = ["redis_connection_list", "redis_query"]
max_rows = 200

[sandbox]
    network = true
    binds   = ["/etc/resolv.conf:/etc/resolv.conf", "/etc/hosts:/etc/hosts"]
```

В корне нет `profiles`: они приходят из таблицы на каждый вызов. Секция с
`ConnectedToolManifest` требует `[connections] enable = true` в
`config.toml`, иначе старт падает с понятным текстом. Для kerberos-типов
добавьте бинд `"${env.krb}/krb5.conf:/etc/krb5.conf"`: имена сервисов тело
разбирает конфигом своей песочницы.

### 4.7. Что происходит на вызове, по шагам

LLM вызвала `redis_query(connection_name="cache", command="GET x")`.

| # | Где | Что делает |
|---|---|---|
| 1 | `InjectedConfig._Partial.before` | `kwargs.setdefault("cfg", <статический RedisToolConfig из toml>)`: `profiles={}`, `names=[]`, `max_rows=200` |
| 2 | `ServiceTickets` | не установлена: в статическом значении kerberos-секций нет |
| 3 | `UserConnections._config` | `subject = CallContext.current().subject` (user_id, login, roles); вне контекста вызова — `RefusalError(no_context)` |
| 4 | `ConnectionStore.for_subject(subject, "redis")` | один SQL: гранты на пользователя ∪ гранты на его роли, фильтр `data->>'kind' = 'redis'`; профили расшифрованы и разобраны реестром |
| 5 | `ConnectionWhitelist.of(rows, NAME)` | группировка по `name`; имя, выданное дважды (лично и через роль, две роли), уходит в `ambiguous` и в whitelist не попадает |
| 6 | `keying.requested(kwargs)` | читает `kwargs["connection_name"]` → `"cache"` |
| 7 | `whitelist.pick("cache")` | профиль; `None`, если такого имени у субъекта нет; `AmbiguousConnectionError` → `RefusalError(ambiguous_connection)` |
| 8 | `_at_host` | только для `HttpConnection`: привязка к хосту URL, раздел 4.10 |
| 9 | `ClientLabel.of(login, "redis_query").applied(profile)` | `profile.labeled("boba:ivanov:redis_query")` |
| 10 | `_armed` | `credentials.for_connection(profile, credential)`: kerberos-секция → билет вызова (раздел 4.9); строка с уже готовым `TicketAuth` в таблице — `ToolConfigError` |
| 11 | сборка | `base.model_copy(update={"profiles": {"cache": armed}, "names": [все имена whitelist]})` → `kwargs["cfg"]` |
| 12 | `ToolProcessWrap` | `ToolArgv.render`: `--connection-name cache --command "GET x"` в argv, `cfg.revealed()` в JSON injected |
| 13 | launcher | процесс или форк зиготы, раздел 5 |
| 14 | тело | `ToolArgv.parse` → `RedisToolConfig`; `cfg.resolve("cache")` → `RedisConfig` с открытым паролем; `open_redis` |

Обратите внимание на шаг 7: неизвестное имя хост не отвергает. Он просто
отправляет пустой `profiles`, и уже тело отвечает `UnknownConnectionError`
со списком допустимых имён — так LLM получает подсказку, а не голый отказ.

### 4.8. `profiles` против `names`

Принцип наименьших привилегий:

- `profiles` — не больше одного элемента: профиль соединения, которое
  вызов назвал. Только он покидает процесс приложения.
- `names` — все имена, выданные субъекту, без профилей. Их видит
  `*_connection_list` через `targets_table()`.

Даже если у пользователя двадцать соединений, в песочницу уезжает пароль
одного.

### 4.9. Kerberos: как строка таблицы превращается в билет

Строка таблицы может нести kerberos-секцию двух видов:

- `{method = "kerberos_delegated"}` — в сервис идёт сам пользователь.
  Работает только если он вошёл через SSO и браузер делегировал креды:
  `CallContext.current().credential` тогда `DelegatedTicket`. Иначе
  `RefusalError(no_delegated_credentials)`.
- `{method = "kerberos_keytab", principal, keytab}` — сервисная учётка;
  keytab лежит на хосте приложения.

В обоих случаях `KerberosCredentialSource.for_connection`:

1. `profile.service_name()` → SPN, например `postgres@db01.corp`.
2. `ServiceTicketIssuer.issue_async(source, service)`: GSSAPI-шаг кладёт
   сервисный билет в ccache источника, затем ровно этот билет копируется
   в свежий FILE-ccache и читается байтами. Остаток жизни билета обязан
   быть ≥ `min_lifetime` секции.
3. `TicketAuth.of_bytes(principal, service, blob, min_lifetime)` → секция
   `method = "kerberos_ticket"` с `ccache: SecretStr` (base64).
4. `profile.with_call_ticket(ticket)` — билет на месте старой секции.
   Форма профиля не меняется: тело не знает, как билет получен.

Дальше `revealed()`: `TicketAuth._dump_ccache` раскрывает байты по
контексту; keytab или пароль, добравшиеся до дампа с контекстом, роняют
`KerberosDump.json`. В теле `TicketCredentials.applied_async()`
материализует ccache во временный файл вызова и выставляет `KRB5CCNAME`.

### 4.10. Web-вариант: соединение покрывает хост

У `web`-инструментов есть второй аргумент, который читает хост: `url`.
Профиль `HttpConnection` покрывает хост `base_url` (точное имя или шаблон
`*.corp.example`, поддомены любой глубины, но не сам apex). Хост в
`UserConnections._at_host` проверяет, что хост URL попадает под выбранное
соединение — иначе `RefusalError(host_not_allowed)` — и привязывает
профиль к конкретному хосту (`bound_to`). Это делается **до** выпуска
билета, потому что SPN `HTTP@host` требует конкретного хоста, а не шаблона.

Тело проверяет то же ещё раз: `cfg.resolve_for(connection_name, url)` →
`UnknownHostError` (`kind = unknown_host`). Конфиг `WebConnection` несёт
третье поле `hosts: dict[name, host]`, чтобы `web_connection_list`
показывал, какой хост покрывает каждое имя, не отправляя профили.

### 4.11. Ошибки по kind

| Ситуация | Кто поднимает | Kind |
|---|---|---|
| имя неизвестно субъекту | тело, `cfg.resolve` | `unknown_target` (из `EXPECTED`) |
| имя выдано дважды | хост | `ambiguous_connection` |
| хост URL вне web-соединения | хост / тело | `host_not_allowed` / `unknown_host` |
| delegated-строка без SSO-кредов | хост | `no_delegated_credentials` |
| строка таблицы с `kerberos_ticket` | хост | `ToolConfigError` |
| delegated в статическом конфиге | хост, `ServiceTickets` | `ToolConfigError` |
| синхронный вызов | хост | `InjectedAsyncOnlyError` |
| тип строки не установлен | хранилище | `UnknownConnectionKindError` |

Отказы хоста (`RefusalError`) — это `ToolRefusalError` с kind из
`ConnectionRefusal`; `ToolErrorGuard` превращает их в `ErrorResult`, и LLM
получает текст.

### 4.12. Отладка тела с соединением

CLI `boba.runtime.toolcli` собирает injected из toml и раскрывает секреты
через тот же `reveal`. Для профиля с keytab это упадёт «may not leave the
application»: билет CLI не выпускает. Поэтому для отладки под дебаггером
injected-файл пишется руками — `.vscode/injected/pg.json` содержит
`{"cfg": {"profiles": {"main": {...keytab...}}, "names": ["main"]}}`, а
цель `launch.json` передаёт `--injected` этим файлом плюс `--config` ради
секции `[krb]` (рабочий каталог kerberos на хосте).

Живые примеры: `packages/tools/boba-tool-postgres/` (pg), `boba-tool-clickhouse/`
(ch), `boba-tool-web/` (web с хостами); типы —
`packages/infra/db/boba-db-postgres/src/boba/db/postgres/{profile,connection.py}`,
`boba-db-clickhouse`, `packages/infra/transport/boba-transport-http/src/boba/transport/http/`.
Хостовая обвязка — `packages/services/boba-connection-broker/src/boba/connection_broker/user_connections.py`.

---

## 5. Как это устроено внутри launcher'а

### 5.1. Порядок обвязок на хосте

`ToolLoader._module_tools` (`boba.runtime.plugins`) для каждого
инструмента из `tools` файла плагина делает копию `PayloadTool` и ставит
обвязки в таком порядке:

1. `ToolProcessWrap.guard_all(tools, launcher)` — подменяет тело: вместо
   функции теперь «собрать команду и запустить launcher'ом». Схема
   аргументов запоминается полной, с injected-полями: по ней рендерится
   команда.
2. `UserConnections.bind_all` — только если манифест `ConnectedToolManifest`
   и параметр наследует `SqlProfiles | WebConnection`.
3. `ServiceTickets.bind_all` — только если статическое значение параметра
   содержит профиль с keytab/password/delegated-секцией.
4. `InjectedConfig.bind_all` — партиал статических значений и **снятие
   injected-полей со схемы**: LLM видит усечённую схему.

Выполняются они в обратном порядке (снаружи внутрь): 4 → 3 → 2 → 1.
Партиал кладёт значение через `setdefault`, обвязки 2 и 3 его
перезаписывают. Снаружи всех стоят общие guard'ы: доступ по ролям, журнал
вызова, отмена, `ToolErrorGuard`.

Статические значения собираются один раз на загрузке:
`resolve(param, annotation)` читает `SECTION` с аннотации и зовёт
`bind(raw, section, annotation)`. Нет `SECTION` — `ToolConfigError` на
старте.

### 5.2. Контракт команды тела

Команду строит `ToolArgv.render`, разбирает `ToolArgv.parse`. Одна и та же
команда у launcher'а и у человека:

```
python3 -m <модуль> <имя-инструмента> --<арг> <значение> ... \
    [--injected <файл> | --injected-fd <n>] [--fd-result <n>] [--fd-frames <n>] [--artifact]
```

- `--<арг>`: имя параметра с `_` → `-`. Строковые значения как есть,
  остальные JSON. Значение больше `MAX_ARG_STRLEN` — `argument_too_large`.
- `--injected-fd <n>` — дескриптор, из которого тело читает JSON injected
  до EOF. Человек вместо него передаёт `--injected <файл>`. stdin конфиг
  не несёт никогда: он принадлежит прикладным кадрам входа.
- `--fd-result <n>` — куда тело пишет конверт. Без него `content` печатается
  в stdout, `--artifact` дописывает JSON артефакта.
- `--fd-frames <n>` — канал исходящих кадров портов (раздел 6).
- stdout тела — его лог (`logging` настроен на stdout, уровень из
  `BOBA_LOG_LEVEL`); stdout и stderr журналируются и стримятся в панель.
- Коды выхода `ToolMain.Exit`: `0` ок, `1` ожидаемый отказ (`EXPECTED`),
  `2` нарушение контракта запуска (`unknown_tool`, `invalid_request`,
  `internal_error`). Неожиданное исключение тела — трейсбек, код не ноль,
  конверта нет; хост поднимает `LauncherError` с хвостом stderr.

Конверт (`boba.toolkit.protocol`): `ReplyOk{status="ok", content, artifact}`
или `ReplyError{status="error", kind, message}`. Хост превращает
`ReplyError` в `PayloadFailureError(kind, message)`, `ToolErrorGuard` — в
`ErrorResult` для LLM.

### 5.3. Секция `[tool_launcher]`

Union по `provider`; переключается `${env.tool_launcher}`, значение из
`BOBA_TOOL_LAUNCHER`. Секция одна на все режимы, поэтому модели с
`extra="ignore"`.

`provider = "sandbox"` — других полей нет: пути берутся из `[env]`
(`base`, `data`, `sandbox`, `models`, `krb`, `cgroup_base`), изоляция из
`[sandbox]` файла плагина.

`provider = "process"` (dev-хост):

| Поле | Значение |
|---|---|
| `workdir` | рабочий каталог тел; тело получает `workdir/<scope.id>` (пер-тредовая папка), вне контекста — общий |
| `shell` | интерпретатор для `call_text` (bash-инструмент) |
| `timeout_sec` | потолок вызова |
| `channel_limit_bytes` | лимит каналов, буферизуемых целиком в памяти (`tool_result`, stdout/stderr shell) |
| `stderr_tail_bytes` | хвост stderr для объяснения вызова без конверта |
| `kill_grace_sec` | пауза между SIGTERM и SIGKILL |

`probe()` на старте: `process` проверяет, что `workdir` существует;
`sandbox` — что `[env]` полон. Остальное `sandbox` проверяет на каждой
секции при создании launcher'а: наличие `bwrap` (тихой деградации в
процесс хоста нет), cgroup-проба, регистрация точки воркспейса.

### 5.4. Провайдер `process`: последовательность вызова

`ProcessToolCaller` (`boba.toolrun.process`):

1. `argv[1]` обязан быть `-m`; `python3` заменяется на `sys.executable`.
2. Создаются пайпы: stdin, result, frames, injected; размер данных-пайпов
   поднимается до 1 MiB. Номера детских концов дописываются флагами
   `--fd-result --fd-frames --injected-fd`.
3. `subprocess.Popen(..., cwd=workdir/<scope.id>, env=os.environ,
   pass_fds=(...), start_new_session=True)` — своя группа процессов ради
   `killpg`.
4. Стартует поток насоса (`ChannelPump`): читает stdout/stderr/result/frames,
   раскладывает по стокам. `RESULT` → `CappedChannel(channel_limit_bytes)`,
   `STDERR` → `ChannelTail`, `FRAMES` → `CallInbox`; те же каналы тиражируются
   в журнал через `ToolChannelsTap`. Переполнение `CappedChannel` убивает
   вызов (`ChannelOverflowError`).
5. Только после старта насоса injected JSON пишется в свой пайп и
   закрывается: тело читает его до EOF.
6. Насос крутится, пока открыты каналы или процесс жив, проверяя отмену
   хода и дедлайн `timeout_sec`; отмена — `killpg(SIGTERM)`, через
   `kill_grace_sec` — `SIGKILL`.
7. Конверт парсится `EnvelopeReply.parse`; пустой канал — `LauncherError`
   «no envelope on tool_result» с хвостом stderr.

`@warmup` и cgroup-лимиты в этом режиме не действуют.

### 5.5. Провайдер `sandbox`: зигота, bwrap, гость

**Зигота на секцию.** `ZygoteRegistry.obtain(section, profile, modules, ...)`
держит по одному `ZygoteSupervisor` на секцию плагина. Старт:
`socketpair(SEQPACKET)` → цепочка bwrap → гость `boba.sandbox.guest`
импортирует модули тел, исполняет `@warmup`-хуки (конфиг хука хост
собирает из `tool.<секция>` и шлёт с раскрытыми секретами) → `ready`.
Смерть зиготы — перезапуск с backoff по `ZygotePolicy`
(`[sandbox.zygote]`); после `max_start_attempts` секция в `FAILED`, все её
вызовы падают.

**Цепочка bwrap** (`ZygoteSpawner._argv`):

```
bwrap (userns, uid 0, --unshare-net если network=false)
  └─ python -m boba.workspace.launcher  --ro-image plugins/<пакет>/rootfs.ext4 /tmp/boba-rootfs  (fuse2fs)
       └─ bwrap (--ro-bind rootfs /, --unshare-pid/ipc/uts, --proc, --dev, tmpfs /tmp, ro-бинды из [sandbox] binds, --clearenv)
            └─ python3 -m boba.sandbox.guest --socket-fd N <модули>
```

Образ корня плагина монтируется read-only один раз на жизнь зиготы.
`network = true` в файле плагина убирает `--unshare-net` у внешнего
bwrap — единственный способ дать телу сеть.

**Вызов** (`ZygoteToolCaller._open_call`):

1. `argv_tail` — команда без `python3 -m <модуль>`: модуль уже импортирован.
2. `plan` — таймаут, rlimits процесса (`process_*` из `limits`), образ
   воркспейса `data/workspace/<user_id>.ext4 → /workspace` (если
   `workspace = true`), `cwd`.
3. Если запрошены `group_*` лимиты — берётся cgroup-лист под
   `[env] cgroup_base`.
4. `supervisor.begin(...)` — одна SEQPACKET-датаграмма с SCM_RIGHTS:
   дескрипторы `STDIN, STDOUT, STDERR, RESULT, FRAMES, INJECTED, CONTROL[, CGROUP]`.
5. Насос стартует, потом injected JSON пишется в свой пайп — так же, как в
   `process`.
6. Завершение узнаётся по control-сокету: `born` с pid исполнителя
   (SCM_CREDENTIALS), `CallSetupFailed`, `CallExit`. Убийство — `SIGKILL`
   этому pid.

**Гость, порядок операций исполнителя** (`ZygoteMain._grandchild`):

1. форк → `unshare(NEWNS|NEWIPC|NEWUTS)` → второй форк `clone3` с
   `CLONE_INTO_CGROUP` (исполнитель рождается уже в листе вызова);
2. приватные `/proc` и tmpfs `/tmp` размером `mounts.tmp`;
3. `dup2` stdin/stdout/stderr; закрыть лишнее (для shell-вызовов — ещё и
   result/frames/injected: пользовательская команда до них не дотянется);
4. `born` хосту;
5. монтирование образа воркспейса fuse2fs, затем `umount2(MNT_DETACH)`
   каталога всех образов: тело чужих не увидит;
6. `setrlimit` по `process_*`, `oom_score_adj`, affinity;
7. сброс всех capabilities, `NO_NEW_PRIVS`;
8. `chdir(cwd)`, дописать `--fd-result --fd-frames --injected-fd`,
   `ToolMain.run(TOOLS, argv)` с уже импортированными `TOOLS`, `os._exit(code)`.

Kerberos-билет вызова в песочницу **не биндится**: он едет внутри injected
JSON как `TicketAuth.ccache`, тело кладёт его в `/tmp` вызова (приватный
tmpfs) только внутри `applied()`, и файл умирает с вызовом. Биндится
только `krb5.conf`.

### 5.6. Секция `[sandbox]` в файле плагина

Модель `PluginSandbox`, `extra="forbid"`: опечатка в ключе — ошибка старта.

| Ключ | Что делает |
|---|---|
| `profile` | полный профиль ссылкой `"${sandbox.profiles.<имя>}"`; взаимоисключим со всеми дельта-ключами ниже. Нужен встроенным плагинам без образа |
| `network` | `true` — сеть хоста (снимает `--unshare-net`); по умолчанию сети нет |
| `workspace` | `true` — монтирует ext4 воркспейса пользователя в `/workspace` и делает его `cwd`; по умолчанию воркспейса нет, `cwd = /tmp` |
| `binds` | пары `host:guest`, только явные файлы/каталоги хоста, read-only; строка без `:` — ошибка. Пути через `${env.*}` |
| `[sandbox.limits]` | накладывается на дефолт: `process_memory_bytes` (1 GiB), `process_cpu_sec`, `process_file_bytes`, `process_open_files` (1024), `process_oom_score_adj` (900), `group_memory_bytes` (1 GiB), `group_swap_bytes` (0), `group_cpu_percent` (100 = одно ядро), `group_cpu_weight`, `group_pids_max` (256), `group_oom_kill_all`, `timeout_sec` (86400) |
| `[sandbox.isolation]` | `network`, `reap_poll_sec`, `env` (PATH, HOME=/tmp, LANG) |
| `[sandbox.run]` | `cwd`, `shell` |
| `[sandbox.host]` | хостовые буферы: `stderr_tail_bytes`, `channel_limit_bytes`, `fail_tail_chars`, `kill_grace_sec`, `mounting` |
| `[sandbox.zygote]` | `ZygotePolicy`: `max_start_attempts`, `restart_backoff_sec`, `start_timeout_sec`, `healthy_after_sec` |

Как накладывается (`ZygoteLaunchers._composed`): база из
`SandboxDefaults.profile(env, package)` (rootfs
`${env.sandbox}/plugins/<пакет>/rootfs.ext4`, дефолтные лимиты) →
`network`/`binds`/`workspace` дельты → таблицы `host/isolation/limits/run`
сливаются рекурсивно (словари сливаются, скаляры и списки заменяются) →
`SandboxProfile.model_validate`. `group_*` лимиты требуют непустого
`cgroup_base`.

Ориентиры: `pg.toml` — сеть и krb5.conf; `bash.toml` — `workspace = true`;
`kb.toml` — сеть, бинд весов, `group_cpu_percent = 400`, память 16/8 GiB.

---

## 6. Потоковые инструменты: порты

Если инструменту нужно получать данные порциями или отдавать по ходу
работы, он объявляет каналы в подписи. Единица обмена — кадр: JSON-заголовок
с полем `kind` (строковый `Literal`) плюс тело байтами.

```python
class RowsChunk(BaseModel):
    kind: Literal["redis.rows"] = "redis.rows"
    seq: int

class ScanDone(BaseModel):
    kind: Literal["redis.done"] = "redis.done"
    total: int


@tool
async def redis_scan_stream(
    pattern: Annotated[str, Field(description="Шаблон ключей")],
    cfg: Annotated[RedisToolConfig, Injected],
    out: Annotated[Outbound[RowsChunk | ScanDone], Injected],
) -> tuple[str, ToolResult]:
    """Отдаёт ключи порциями по мере обхода."""
    seq = 0
    async for batch in _scan(cfg, pattern):
        seq += 1
        out.emit(RowsChunk(seq=seq), _encode(batch))

    out.emit(ScanDone(total=seq))

    return pack_result(TextResult(text=f"streamed {seq} batches"))
```

- Входной порт — итератор `for item in feed:` с `item.head` (модель) и
  `item.body` (memoryview). Конец входа — конец цикла.
- Не больше одного входного и одного выходного порта; несколько видов —
  союз моделей в одном порте. Кадр с kind вне декларации роняет вызов на
  границе.
- `RawInbound`/`RawOutbound` — голые байты без кадрирования (COPY между
  базами, файлы). Сырое совместимо только с сырым; хост его не разбирает,
  перекачка — splice через ядро.
- Запись блокируется при медленном потребителе: залить хост тело не может.
- `return` остаётся: конверт — итог, кадры — то, что по дороге.
- Порты не снимаются со схемы, но обязательными быть не могут: значение
  строит гость по `--fd-frames` и stdin.

Потоковые инструменты — узлы конвейера: LLM собирает цепочку через
`pipeline_catalog` и `pipeline_run`, стыковку проверяет `ChainCheck` по
декларациям до старта, данные текут между узлами через ядро. Регистрации
не нужно: порт в подписи — уже узел каталога.

Образцы: `packages/testing/boba-stand/src/boba/stand/fake_toolmod.py`
(`fake_stream`, `fake_relay`), `pg_copy_out`/`pg_copy_in` в
`boba-tool-postgres`. Архитектура канала —
`docs/streaming-tools-rework-plan.md`.

---

## 7. Сборка образа песочницы: `[tool.boba.sandbox]`

В sandbox-режиме тело исполняется внутри собственного образа корня
`sandbox/plugins/<пакет>/rootfs.ext4`. Секция `[tool.boba.sandbox]` в
pyproject — декларация «что должно оказаться внутри моего образа». Её
читает `make -C build/<app> plugin-rootfs PLUGIN=<пакет>`
(скрипт `build/*/scripts/plugin_rootfs.py`); в рантайме секция не участвует.

Python-часть образа декларировать не нужно: в него автоматически ставится
закрытие `payload`-зависимостей пакета. Секция описывает остальное:

- `imports` — смоук-проверка: модули, которые сборка импортирует внутри
  образа после установки; не импортируется — сборка падает здесь, а не
  первым вызовом в проде.
- `apt` — нативные debian-пакеты (утилиты, разделяемые библиотеки, шрифты).
- `data` — данные из fetch-артефактов сборки: пары
  `<каталог в build/<app>/src>:<путь внутри образа>` (веса моделей,
  словари; сети песочнице не положено).
- `root` — каталог-оверлей внутри пакета, копируется поверх корня как есть.
- `setup` — shell-скрипт внутри пакета, исполняется после apt и `root`.

**Сборка читает декларации не только вашего пакета, но и всех boba-пакетов
по закрытию зависимостей.** Зависит `boba-tool-doc` от `boba-liteparse` —
сборка образа doc заберёт `apt`, `data`, `root`, `setup` liteparse тоже.
Поэтому каждая декларация живёт у настоящего владельца стека: libreoffice
и tessdata объявляет liteparse, а doc и knowledge про них не знают.

Служебный ключ `guest = true` у `boba-sandbox` помечает пакеты, чей код
исполняется внутри образа как гость зиготы; обычному плагину не нужен.

Живые декларации: `boba-tool-shell` (только `imports`), `boba-tool-doc`
(`imports = ["liteparse"]`, стек приезжает по закрытию), `boba-liteparse`
(`apt`, `data`, `root`, `setup`), `boba-llm` (`data` с весами эмбеддера),
`boba-sandbox` (`guest = true`).

После правок `boba-toolkit`/`boba-sandbox` образы обязаны пересобираться
(`make plugin-rootfs-all`): гость внутри rootfs отстаёт от хоста по
протоколу каналов. У chainlit и studio песочницы свои: `build/chainlit` и
`build/studio`.

---

## 8. Проверка и отладка

Установка и обнаружение:

```bash
./build/chainlit/src/uv/uv sync --all-packages
cd compose/chainlit && BOBA_TOOL_LAUNCHER=process ../../.venv/bin/python -m pytest \
    ../../packages/services/boba-runtime/tests/test_plugin_discovery.py -q
```

Entry points материализуются установкой: после правки pyproject пакет
нужно переустановить (`uv sync` либо `pip install --no-deps --force-reinstall -e`).

Тело руками — раздел 2, шаг 8. Под дебаггером — цель `launch.json`
«pg_query tool» с `boba.runtime.toolcli`: `--config` даёт `[krb]`, `--injected`
даёт готовый JSON (обязателен для keytab-профилей, раздел 4.12).

Тесты — интеграционные, на реальных зависимостях, по образцу соседей:

- конфиг из toml: `bind(raw_config, "tool.doc", DocToolSection)` и вызов
  корутины напрямую (`packages/tools/boba-tool-doc/tests/test_run_doc.py`);
- whitelist вручную: `limits.model_copy(update={"profiles": {"main": service}})`
  (`packages/tools/boba-tool-postgres/tests/test_run_pg.py`);
- контракт запуска субпроцессом с JSON injected
  (`packages/core/boba-toolkit/tests/test_entry.py`);
- инструмент виден в чате — сценарий в UI-стенде `tests/ui/test_tools_ui.py`.

Полный прогон — по пакетам отдельными pytest-процессами; однопроцессный
прогон всего набора каскадит.

---

## 9. Куда смотреть, если что-то не так

- Тип не появился в `kinds()` — не прогнан `uv sync`, или имя entry point
  не совпало с `kind`.
- Старт падает «conf/plugins/<name>.toml is missing» — плагин установлен,
  файла в развёртывании нет (в studio тоже).
- Старт падает «injected parameter 'cfg' has no SECTION on its model» —
  забыт `SECTION: ClassVar[str]`.
- Старт падает «[tool.<name>] takes its connections from the connections
  table» — `ConnectedToolManifest` при `[connections] enable = false`.
- Тело получает `**********` вместо секрета — модель конфига не наследует
  `SecretRevealing` (раздел 3.1) либо у поля объявлен свой
  `field_serializer`, который маскирует.
- «credentials may not leave the application» — keytab/пароль kerberos
  дошёл до дампа: профиль не наследует `ConnectionProfileBase`, не
  реализует `kerberos_section`/`service_name`/`with_call_ticket`, либо это
  toolcli без `--injected` (раздел 4.12).
- «is built in the async body only» — инструмент с соединениями объявлен
  `def`, а не `async def`.
- Соединение есть в таблице, а `resolve` даёт unknown — имя выдано дважды
  (лично и ролью) и попало в `ambiguous`; либо грант есть, а kind строки не
  тот, что в `UserConnectionsSpec`.
- Строка соединения с пометкой «type not installed» — пакет типа не
  установлен в этом развёртывании.
- Зигота секции не поднимается в контейнере — образ плагина не собран или
  собран до правок деклараций: `make plugin-rootfs PLUGIN=<пакет>`.
- «control closed on call …» или смерть зиготы на первом вызове — гость в
  rootfs отстал от хоста: `make plugin-rootfs-all` для обеих сборок.
- Тело не видит сеть — нет `network = true` или биндов `resolv.conf`/`hosts`.
- «inbound frame does not match the declared port» — источник шлёт kind
  вне декларации входа; «raw and framed ports do not mix» — кадровый выход
  соединили с сырым входом.
