# Как добавить новый тип соединения и новый tool-плагин

Пошаговая инструкция на двух сквозных примерах: тип соединения `redis` и
tool-плагин `redis` с инструментом `redis_query`. Везде, где написано
«redis», подставьте своё имя.

Обе системы устроены одинаково: пакет объявляет манифест в entry points,
приложение находит его само. В коде ядра ничего перечислять не нужно.

---

## Часть 1. Новый тип соединения

Тип соединения — это то, что администратор создаёт на странице «Соединения»:
модель полей (host, port, auth, …), проверка кнопкой «Check» и правила
kerberos. Всё это живёт в одном инфра-пакете — «владельце типа».

### Шаг 1. Выберите пакет-владелец

Тип живёт в инфра-пакете, который умеет ходить в эту систему. Для redis это
будет новый пакет `packages/infra/db/boba-db-redis`. Если инфра-пакет уже
есть (как boba-db-postgres) — новый пакет не нужен, всё добавляется в него.

### Шаг 2. Напишите модель профиля

Файл `src/boba/db/redis/profile.py`. Модель наследует
`ConnectionProfileBase` из `boba.connections.base` и обязана иметь поле
`kind` со своим значением:

```python
"""Профиль соединения redis."""

from typing import Literal

from pydantic import Field, SecretStr

from boba.connections.base import ConnectionProfileBase


class RedisConfig(ConnectionProfileBase):
    """Подключение к redis: адрес и пароль."""

    kind: Literal["redis"] = Field(
        default="redis",
        description="Дискриминатор соединения при хранении в базе.",
    )

    host: str
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None

    def trace(self) -> str:
        return f"auth=password host={self.host}"
```

Что здесь важно:

- `kind: Literal["redis"]` — по этому полю строка из базы находит свою
  модель. Значение хранится в jsonb, менять его потом нельзя.
- Секреты объявляются типом `SecretStr` — этого достаточно: хранилище само
  шифрует их на любой глубине модели, а формы показывают их как password.
- `trace()` — строка для журнала («под кем идём»); обязателен.
- Если тип умеет kerberos — переопределите ещё три метода базового класса:
  `kerberos_section()` (где в профиле лежит kerberos-секция),
  `with_call_ticket(ticket)` (профиль с билетом вызова вместо секции),
  `service_name()` (SPN сервиса). Смотрите живой пример:
  `packages/infra/db/boba-db-postgres/src/boba/db/postgres/profile/config.py`.
- Если серверу можно подписать сессию именем клиента (как application_name
  у postgres) — переопределите `labeled(label)`.

### Шаг 3. Напишите манифест с пробой

Файл `src/boba/db/redis/connection.py`. Проба — это то, что выполняет
кнопка «Check» на странице: открыть соединение, сделать пробный запрос,
вернуть строку об успехе:

```python
"""Тип соединения redis: манифест для реестра boba.connections."""

from boba.connections.base import ConnectionProfileBase, ConnectionTypeError
from boba.connections.manifest import ConnectionTypeManifest
from boba.db.redis.profile import RedisConfig

__all__ = ["MANIFEST"]


async def _probe(profile: ConnectionProfileBase) -> str:
    if not isinstance(profile, RedisConfig):
        raise ConnectionTypeError(f"redis probe got a {profile.kind!r} profile")

    client = await open_redis(profile)          # ваш клиент из этого же пакета
    try:
        pong = await client.ping()
    finally:
        await client.close()

    return f"PONG {pong}"


MANIFEST = ConnectionTypeManifest(
    kind="redis",
    profile=RedisConfig,
    probe=_probe,
)
```

Ошибки пробе глотать не нужно: любое исключение превратится в аккуратный
`ProbeResult(ok=False, message=...)` на границе.

### Шаг 4. Объявите entry point

В `pyproject.toml` пакета-владельца:

```toml
[project.entry-points."boba.connections"]
redis = "boba.db.redis.connection:MANIFEST"
```

Имя entry point (`redis`) обязано совпадать со значением `kind` манифеста —
реестр это проверяет на старте.

### Шаг 5. Установите и проверьте

```bash
./build/chainlit/src/uv/uv sync --all-packages
.venv/bin/python -c "from boba.connections.manifest import ConnectionTypes; print(ConnectionTypes.discover().kinds())"
```

В выводе должен появиться `redis`. Всё: страница «Соединения» сама покажет
новый тип в форме (схема строится из реестра), хранилище сам разберёт его
строки из базы, проба заработает у кнопки «Check». Никакие файлы ядра,
брокера или api править не нужно.

Если пакет типа потом удалить, приложение стартует как раньше: строки этого
типа в списках отмечаются как «type not installed», а попытка использовать
такое соединение падает понятной ошибкой `UnknownConnectionKindError`.

---

## Часть 2. Новый tool-плагин

Tool-плагин — это пакет с инструментами для LLM. Приложение находит его
через entry points и требует ровно один файл конфига у развёртывания.

### Шаг 1. Создайте пакет

`packages/tools/boba-tool-redis` со структурой:

```
packages/tools/boba-tool-redis/
    pyproject.toml
    src/boba/tool/redis/
        __init__.py
        plugin.py      # манифест
        tools.py       # сами инструменты
```

### Шаг 2. Напишите инструменты

`src/boba/tool/redis/tools.py` — функции с декоратором `@tool` из toolkit,
собранные в кортеж `TOOLS` (живой пример —
`packages/tools/boba-tool-postgres/src/boba/tool/pg/tools.py`).

У параметра инструмента бывает три вида, и вид задаётся аннотацией:

- **аргумент для LLM** — обычное поле (`sql: Annotated[str, Field(...)]`);
  модель его видит и заполняет, лончер кодирует во флаг команды;
- **injected-конфиг** — `Annotated[RedisToolConfig, Injected]`; LLM его не
  видит, значение собирает приложение из toml секции (`SECTION` на модели)
  и присылает телу отдельным каналом — секреты в argv не попадают;
- **порт данных** — `Annotated[Inbound[...] | Outbound[...] | RawInbound |
  RawOutbound, Injected]`; это канал, по которому данные текут во время
  вызова. Про порты — раздел ниже.

Обычный (накопительный) инструмент выглядит так и остаётся самым частым
случаем:

```python
@tool
async def redis_query(
    connection_name: RedisConnection,
    command: Annotated[str, Field(min_length=1, description="Команда redis")],
    cfg: Annotated[RedisToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Выполняет команду на соединении и возвращает ответ."""
    ...
    artifact = TextResult(text=reply)
    return render_for_llm(artifact), artifact
```

Ожидаемые отказы тела объявляются картой `EXPECTED` на уровне модуля
(исключение -> kind), неожиданные исключения уходят наверх как дефект.

#### Потоковые инструменты: порты в подписи

Если инструменту нужно получать данные порциями или отдавать результаты по
ходу работы (аудио, выгрузки, длинные генерации) — он объявляет каналы
прямо в подписи. Единица обмена — кадр: маленький JSON-заголовок с
метаданными плюс сырое тело байтами.

Сначала модели заголовков; обязательное правило — поле `kind` со строковым
`Literal` (по нему pydantic разводит союз моделей на границе):

```python
class RowsChunk(BaseModel):
    """Порция строк выгрузки."""

    kind: Literal["redis.rows"] = "redis.rows"
    seq: int

class ScanDone(BaseModel):
    """Финальный кадр: сколько всего отдано."""

    kind: Literal["redis.done"] = "redis.done"
    total: int
```

Потом порты в подписи (`boba.toolkit.ports`):

```python
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

    artifact = TextResult(text=f"streamed {seq} batches")
    return render_for_llm(artifact), artifact
```

Входной порт — итератор: `for item in feed:` отдаёт `Framed` с уже
провалидированным заголовком (`item.head` — экземпляр вашей модели) и телом
(`item.body` — байты; это memoryview, для склейки с bytes — `bytes(item.body)`).
Конец входа — просто конец цикла (EOF канала), никаких служебных кадров.
Кадр с kind вне декларации роняет вызов на границе с внятной ошибкой — до
тела мусор не доходит.

Правила и свойства:

- не больше одного входного и одного выходного порта на инструмент;
  несколько видов данных — союз моделей в одном порте;
- `return` никуда не девается: потоковый инструмент всё равно возвращает
  итог конвертом, кадры — то, что происходит по дороге;
- запись в порт блокируется, когда потребитель не успевает (backpressure):
  заливать хост данными тело не может;
- декларация машиночитаема: `StreamSpec.of_schema` отдаёт входные и
  выходные kind'ы для манифеста и проверки стыковки цепочек
  (`ChainCheck` в `boba.toolkit.chain`), по ней же цепочки A -> B
  соединяются перекачкой `CallRelay` (в том числе zero-copy через ядро).

#### Сырые порты: passthrough без структур

Когда формат потока задаёт внешняя система и разбирать его не нужно
(COPY между базами, файлы, PCM) — берите истинно сырые порты: никаких
моделей, на проводе голые байты без какого-либо кадрирования:

```python
@tool
async def redis_restore(
    cfg: Annotated[RedisToolConfig, Injected],
    feed: Annotated[RawInbound, Injected],       # порции bytes как есть
) -> tuple[str, ToolResult]:
    """Грузит поток дампа со входа."""
    total = 0
    for chunk in feed:
        total += len(chunk)
        await _restore(cfg, chunk)

    artifact = TextResult(text=f"restored {total} bytes")
    return render_for_llm(artifact), artifact
```

Сырой канал совместим только с сырым (кадровый поток в сырой вход не
собирается — это ловит `ChainCheck` до запуска); хост сырой канал не
разбирает и не журналирует, его путь перекачки — splice через ядро.

Потоковые инструменты — узлы конвейера: LLM не зовёт их поодиночке, а
собирает в цепочку через инструменты оркестрации `pipeline_catalog`
(каталог узлов с kind'ами входов и выходов) и `pipeline_run` (запуск
линейной цепочки: стыковка проверяется по декларациям до старта, данные
текут между узлами через ядро, в контекст модели попадают только конверты).
Никакой регистрации для этого не нужно: инструмент попадает в каталог
самим фактом объявления порта в подписи.

Образцы потоковых тел — в стенде:
`packages/testing/boba-stand/src/boba/stand/fake_toolmod.py`
(`fake_stream` — модельные порты, `fake_relay` — сырой passthrough).
Оркестратор конвейеров — `boba.toolrun.pipeline`; архитектура канала
вызова целиком — `docs/streaming-tools-rework-plan.md`.

### Шаг 3. Напишите манифест

`src/boba/tool/redis/plugin.py`:

```python
"""Манифест плагина redis: entry point группы boba.tools."""

from typing import Final

from boba.toolkit.manifest import ToolPluginManifest
from boba.tool.redis.tools import TOOLS

MANIFEST: Final = ToolPluginManifest(
    section="redis",
    tools=tuple(TOOLS),
)
```

`section` — идентификатор плагина: он же имя секции конфига `tool.redis` и
имя файла `conf/plugins/redis.toml`.

Если инструменты работают с соединениями пользователя (как pg_query) —
вместо `ToolPluginManifest` берите `ConnectedToolManifest` и укажите вид
соединения через манифест типа:

```python
from boba.connections.marks import ConnectedToolManifest, UserConnectionsSpec
from boba.connections.whitelist import ConnectionKeying
from boba.db.redis.connection import MANIFEST as REDIS_CONNECTION

MANIFEST: Final = ConnectedToolManifest(
    section="redis",
    tools=tuple(TOOLS),
    connections=UserConnectionsSpec(REDIS_CONNECTION.kind, ConnectionKeying.NAME),
)
```

### Шаг 4. Заполните pyproject

```toml
[project]
name = "boba-tool-redis"
version = "0.0.15.dev1"
dependencies = [
    "boba-toolkit==0.0.15.dev1",
    "boba-db-redis==0.0.15.dev1",
]

[project.entry-points."boba.tools"]
redis = "boba.tool.redis.plugin:MANIFEST"

[project.optional-dependencies]
payload = [
    "redis>=5",
]

[tool.boba.sandbox]
imports = ["redis"]
```

Здесь решаются две вещи, не считая обычных зависимостей:

- `payload` — зависимости, нужные только телу инструмента в песочнице
  (клиент redis, парсеры, ML-библиотеки). Приложение-хост их не
  устанавливает: в его окружении тела не исполняются.
- Регистрация пакета в workspace: добавьте его в members корневого
  pyproject (по образцу соседей), затем `uv sync --all-packages`.

#### Секция [tool.boba.sandbox]: из чего собирается образ песочницы

В sandbox-режиме тело инструмента исполняется не на хосте, а внутри
собственного образа корня — `sandbox/plugins/<пакет>/rootfs.ext4`. Секция
`[tool.boba.sandbox]` — это декларация «что должно оказаться внутри моего
образа». Её читает сборочная цель `make plugin-rootfs PLUGIN=<пакет>`
(скрипт build/*/scripts/plugin_rootfs.py); в рантайме секция не участвует.

Python-часть образа декларировать не нужно — в него автоматически ставится
закрытие `payload`-зависимостей пакета. Секция описывает всё остальное:

`imports` — смоук-проверка собранного образа. Список модулей, которые
сборка попробует импортировать внутри образа сразу после установки
зависимостей; если модуль не импортируется, сборка падает здесь, а не
первым вызовом инструмента в проде.

```toml
imports = ["redis"]
```

`apt` — нативные debian-пакеты, без которых python-библиотекам не жить:
системные утилиты, разделяемые библиотеки, шрифты. Ставятся в образ на
стадии сборки корня.

```toml
apt = ["imagemagick", "fonts-dejavu-core"]
```

`data` — данные, которые запекаются в образ из fetch-артефактов сборки.
Каждая строка — пара `<каталог в build/<app>/src>:<путь внутри образа>`.
Так в образы попадают веса моделей и словари: сеть песочнице не положена,
всё нужное должно лежать внутри заранее.

```toml
data = ["tessdata:/usr/share/tessdata"]
```

`root` — каталог-оверлей внутри пакета: его содержимое копируется поверх
корня образа как есть. Место для собственных обёрток и файлов по абсолютным
путям (пример: boba-liteparse кладёт `sandbox-root/usr/local/bin/magick`).

```toml
root = "sandbox-root"
```

`setup` — shell-скрипт внутри пакета, выполняется на стадии сборки корня
после установки apt-пакетов и наложения `root`. Для правок, которые нельзя
выразить копированием файлов (пример: boba-liteparse правит policy.xml
ImageMagick и раскладывает симлинки tessdata).

```toml
setup = "sandbox-setup.sh"
```

Ключевой принцип: **сборка читает декларации не только вашего пакета, но и
всех boba-пакетов, от которых он зависит** — напрямую и через промежуточные
зависимости, по всей цепочке (это и называется «закрытие зависимостей»).
Зависит boba-tool-doc от boba-liteparse — значит, при сборке образа doc
сборка заглянет и в pyproject liteparse и заберёт его `apt`, `data`, `root`
и `setup` тоже.

Поэтому каждая декларация живёт у настоящего владельца: libreoffice и
tessdata объявляет boba-liteparse — это его нативный стек, без которого не
работает его код. А boba-tool-doc и boba-tool-knowledge ничего про
libreoffice не знают: он приезжает в их образы сам, просто потому что они
зависят от liteparse. Вашему пакету достаточно объявить только то, чем
владеет он сам.

Служебный ключ `guest = true` встречается у boba-sandbox: он помечает
пакеты, чей код исполняется внутри образа как гость зиготы, и в закрытие
каждого образа они попадают всегда. Обычному плагину он не нужен.

Живые декларации, от простой к богатой:

- `packages/tools/boba-tool-shell/pyproject.toml` — минимум: только смоук
  `imports = ["pandas", "openpyxl"]`, весь образ — payload-закрытие.
- `packages/tools/boba-tool-doc/pyproject.toml` — сам декларирует лишь
  `imports = ["liteparse"]`; весь нативный стек приезжает по закрытию
  от liteparse.
- `packages/infra/format/boba-liteparse/pyproject.toml` — владелец стека
  документов: `apt` (imagemagick, libreoffice-*, ghostscript, шрифты),
  `data = ["tessdata:/usr/share/tessdata"]`, оверлей `sandbox-root/`
  (обёртка `usr/local/bin/magick`) и `sandbox-setup.sh` (правка policy.xml
  ImageMagick, симлинки tessdata).
- `packages/infra/llm/boba-llm/pyproject.toml` — запекание весов эмбеддера:
  `data = ["fastembed:/var/cache/fastembed"]`; kb- и ingest-образы получают
  их по закрытию.
- `packages/infra/sandbox/boba-sandbox/pyproject.toml` — тот самый
  `guest = true`.

### Шаг 5. Положите файл конфига плагина

Это обязательный шаг: установленный плагин без файла конфига — ошибка
старта. Файл кладётся в каждое развёртывание:
`compose/chainlit/conf/plugins/redis.toml` (и `compose/studio/...`, если
плагин нужен studio):

```toml
enable = true
tools  = ["redis_query"]

[sandbox]
    network = true
    binds   = [
        "/etc/resolv.conf:/etc/resolv.conf",
        "/etc/hosts:/etc/hosts"
    ]
```

Корневая часть — доменные настройки секции (их читает `config_model`
манифеста, если объявлена). `[sandbox]` — изоляция: по умолчанию сети нет и
воркспейса нет; `network = true` включает сеть, `workspace = true` даёт
рабочий каталог, `binds` — только явные пары host:guest хостовых файлов.
Таблицы `[sandbox.limits]`, `[sandbox.isolation]`, `[sandbox.run]`,
`[sandbox.zygote]` накладываются на дефолтный профиль точечно.

### Шаг 6. Соберите образ песочницы

```bash
make -C build/chainlit plugin-rootfs PLUGIN=boba-tool-redis
```

Цель читает декларации `[tool.boba.sandbox]` по закрытию пакета и собирает
`sandbox/plugins/boba-tool-redis/rootfs.ext4`. На dev-хосте в process-режиме
этот шаг не нужен — тело работает субпроцессом хоста.

### Шаг 7. Проверьте

```bash
./build/chainlit/src/uv/uv sync --all-packages
cd compose/chainlit && BOBA_TOOL_LAUNCHER=process .venv/bin/python -m pytest \
    ../../packages/services/boba-runtime/tests/test_plugin_discovery.py -q
```

Дальше — интеграционный тест инструмента по образцу соседних
(`packages/tools/*/tests`) и, если инструмент виден в чате, сценарий в
UI-стенде (`tests/ui/test_tools_ui.py`).

---

## Живые примеры целиком

Типы соединений (часть 1 вживую):

- postgres: `packages/infra/db/boba-db-postgres/src/boba/db/postgres/`
  — модель в `profile/config.py` (kerberos-методы контракта:
  `kerberos_section`, `with_call_ticket`, `service_name`, `labeled`),
  манифест с пробой в `connection.py`, entry point в `pyproject.toml`.
- clickhouse: `packages/infra/db/boba-db-clickhouse/src/boba/db/clickhouse/`
  — то же строение: `profile/` + `connection.py`.
- web: `packages/infra/transport/boba-transport-http/src/boba/transport/http/`
  — `profile.py` (вложенный union способов auth) и `connection.py`
  (проба HTTP-запросом).

Tool-плагины (часть 2 вживую):

- `packages/tools/boba-tool-postgres/` — плагин с соединениями пользователя:
  `src/boba/tool/pg/plugin.py` (ConnectedToolManifest, kind из манифеста
  типа), инструменты в `tools.py`, payload и entry point в `pyproject.toml`.
- `packages/tools/boba-tool-doc/` — плагин с тяжёлым нативным стеком,
  который приезжает по закрытию от liteparse.
- `packages/tools/boba-tool-chart/` — плагин без соединений: обычный
  ToolPluginManifest.

Конфиги плагинов развёртывания: `compose/chainlit/conf/plugins/*.toml` —
четырнадцать живых файлов; `pg.toml` — сетевой с kerberos-биндом,
`bash.toml` — с workspace, `kb.toml` — с поднятыми лимитами памяти.

## Куда смотреть, если что-то не так

- Тип не появился в `kinds()` — не прогнан `uv sync` (entry points
  материализуются установкой), или имя entry point не совпало с `kind`.
- Приложение не стартует с «conf/plugins/<name>.toml is missing» — плагин
  установлен, а файла конфига в развёртывании нет: положите файл (шаг 5).
- Строка соединения в списке с пометкой «type not installed» — пакет
  типа не установлен в этом развёртывании.
- Зигота секции не поднимается в контейнере — образ плагина не собран или
  собран до правок деклараций: `make plugin-rootfs PLUGIN=<пакет>`.
- Вызовы падают «control closed on call …» или зигота умирает на первом
  вызове — гость внутри rootfs отстал от хоста по протоколу каналов: после
  правок boba-toolkit/boba-sandbox образы обязаны пересобираться
  (`make plugin-rootfs-all`); у chainlit и studio песочницы свои —
  `build/chainlit` и `build/studio`, пересобирать обе.
- Потоковый вызов падает «inbound frame does not match the declared port»
  — источник шлёт kind, которого нет в декларации входа: расширьте союз
  моделей входного порта либо почините источник; стыковку цепочки заранее
  проверяет `ChainCheck.ensure`.
- Ошибка «raw and framed ports do not mix» — кадровый выход соединили с
  сырым входом (или наоборот): сырое совместимо только с сырым.
