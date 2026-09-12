# Граф знаний над корпусами: план слоя хранения

## 1. Цель и границы

Появляется общий слой хранения для индексаторов разных корпусов. Первый
корпус — Confluence: страницы и вложения. Второй — хранилище данных: базы,
схемы, таблицы, представления, колонки, индексы, процедуры разных движков
(PostgreSQL, ClickHouse, Oracle, MySQL). Оба индексатора решают одну задачу:
найти взаимосвязи между единицами корпуса и описать их так, чтобы поиск
находил опорные единицы полнотекстом и вектором, расширял выдачу по графу и
возвращал адрес исходника для чтения оригинала большой моделью.

В хранилище данных явных связей почти нет: внешние ключи объявлены редко,
комментарии пусты, имена таблиц — история миграций. Индексатор хранилища
строит связи из косвенных признаков: определений представлений, совпадающих
имён и типов колонок, пересечения значений, совместного использования в
запросах, упоминаний в комментариях. В Confluence те же роли играют ссылки,
оглавления, упоминания заголовков и метки. Поэтому ядро одно, а признаки —
свои у каждого корпуса.

Слои по коду:

- **доменное ядро** (`boba-graph`) — модели узла, ребра, сущности, адреса;
  порты хранения; конвейер. О СУБД, DDL и SQL оно не знает: хранение
  приходит реализацией портов;
- **реализация хранения** (`boba-db-pggraph`) — порты ядра на Postgres,
  и только на нём: pgvector, `tsvector`, `pg_trgm`, AGE. Имя называет
  движок, как у соседей `boba-db-postgres` и `boba-db-pgvector`; другая
  СУБД — другой пакет с теми же портами:
  DDL graph tables, обход, слияние поиска по индексам, два бэкенда
  графа;
- **корпуса** — по пакету на источник, симметричные и независимые:
  `boba-corpus-confluence` сейчас, `boba-corpus-warehouse` следующим. В
  каждом — виды узлов, текстов и связей, ридер, content tables с их DDL и
  индексами поиска, явные рёбра, резолвер, инструменты индексации
  своего корпуса;
- **инструменты над графом** (`boba-tool-graph`) — поиск, обход, глобальная
  стадия, установка схемы; работают с любым корпусом через реестр и о
  Confluence или хранилище не знают.

Слои по данным — каждый корпус в своей схеме Postgres: `confluence`,
`warehouse`, стендовые `confluence_test`, `warehouse_test`. Внутри схемы:

- **graph tables** — только то, что ядро обрабатывает одинаково для
  любого корпуса: идентичность узлов, учёт обхода, рёбра, сущности,
  метрики, реестр моделей эмбеддинга. Их структура — реализация портов
  ядра в Postgres; DDL живёт в `boba-db-pggraph`, а не в ядре;
- **content tables** — всё остальное, включая тексты и векторы: у Confluence
  страницы, вложения, разделы, таблицы страниц, саммари; у хранилища
  отношения, колонки, ограничения, индексы, процедуры, профили, DDL,
  комментарии. Текст и его индексы лежат там, где лежит объект, которому
  они принадлежат, со своей структурой, а не сплющенными в общую строку.

Graph tables отвечают на вопросы «что это», «с чем связано» и «где
исходник» одинаково для любого корпуса. Всё содержимое — «о чём» — у
content tables; ядро добирается до него через индексы поиска, которые корпус
объявляет (раздел 2).

Это индексатор 2.0: свои пакеты, своя схема, свой конвейер, свои
инструменты. Текущий индексатор (`kb_*`, `boba-db-pgvector`,
`boba-tool-confluence`, `boba-tool-knowledge`) не меняется и работает
рядом; ничего из него не мигрируется, схема 2.0 создаётся установкой с
нуля. Ридеры форматов, секции, чанкер и эмбеддер из
`boba-indexing`/`boba-text` используются как библиотеки, без правок.

Граф хранится в Postgres в одном из двух бэкендов на выбор конфига:
реляционном (таблица рёбер и рекурсивный SQL) или Apache AGE (вершины,
рёбра и обход на openCypher). Какой доступен на базе — тот и используется;
ядро пишется под оба через один порт (раздел 3.7).

## 2. Корпус — полиморфный компонент, а не набор значений `kind`

Ядро не знает, что такое страница, таблица или индекс. Каждая колонка
`kind` в схеме — строка, которую ядро хранит, сравнивает на равенство и
никогда не толкует. Смысл строке даёт корпус: у каждого свои перечисления
видов узлов, нарезок и рёбер, свой ридер, своё описание узла, свой
резолвер исходника. Общего перечисления «все виды всех корпусов» нет ни в
коде, ни в базе, ни в документации — иначе код Confluence и хранилища
нельзя развести по пакетам.

Ядро задаёт протокол корпуса и работает только через него. Никакой
классификации связей у ядра нет: обход умножает вес ребра на множитель
по его `kind` из конфига корпуса, а два вида рёбер, которые ядро пишет
само — по общим сущностям и по близости векторов, — оно называет именами,
которые ему даёт корпус.

**Узел ядра и узел корпуса.** Виды узлов и модели адресов объявляет
пакет источника, который и так знает его объекты: `boba-confluence` —
`ConfluenceNodeKind` и адреса страниц, `boba-db-postgres` — `PgNodeKind`
и адреса объектов PostgreSQL, `boba-db-clickhouse` — `ChNodeKind` (2.1).
Там же — union узлов источника по `kind` (`ConfluenceNode`, `PgNode`,
`ChNode`), которым строка ядра разбирается в типизированную модель.
Корпус этих моделей не дублирует и не оборачивает; его собственное —
виды текстов и рёбер (`ConfluenceTextKind`, `ConfluenceEdgeKind`,
`WarehouseTextKind`, `WarehouseEdgeKind` — разделы 3.4, 4.1, 4.2), потому
что это его таблицы и его связи. Ядро в SQL и в коде
оперирует `kind: str` и никогда по нему не ветвится.
Инструменты поиска принимают имя корпуса и получают его реализацию из
реестра; общих `if kind == "page"` в ядре быть не может по построению.

`kind` — полный дискриминатор узла: он один определяет, какой класс
натягивается на строку, какая модель адреса её разбирает и в каких
content tables лежит содержимое. Поэтому значение называет и источник, и
объект: `confluence_page`, `pg_table`, `ch_column`, `oracle_view`, а не
`table` с уточнением через `scheme` — таблица PostgreSQL и таблица
ClickHouse адресуются по-разному и хранятся в разных полях. «Все таблицы
любого движка» при этом выбираются по адресу: `address ? 'table'`.
`scheme` в адресе остаётся, хотя выводим из `kind`: он нужен грамматике
строки и держит адрес самодостаточным.

`kind` — отдельная колонка и отдельное поле модели, а не ключ внутри
`address`: в базе по нему фильтруют и индексируют без разбора JSON, в
коде он — дискриминатор pydantic-union, а дискриминатор живёт на верхнем
уровне модели. Идентичность узла от этого не зависит: одинаковый адрес с
разными видами невозможен, поэтому `kind` в ключ уникальности не входит.

Узел ядра непрозрачен, а два jsonb-поля graph tables — адрес узла и
обоснование ребра — имеют типизированную модель на стороне того, кто их
пишет. Ядро задаёт им базовые классы: pydantic-модель не может наследовать
`Protocol` (конфликт метаклассов), а наследование должно быть явным
(правило §14), поэтому база — сама pydantic-модель с абстрактными
методами. Пакет источника наследует `Address`, вычислитель ребра —
`Evidence`:

```python
# boba-graph: ядро.
# Ошибки наружу:
# AddressError — строка адреса не по грамматике схемы или не по канону 3.6: без порта, с учётными данными,
#   с чужими ролями; кидают split()/parse() адресов.
class AddressError(Exception): ...

class Node(BaseModel):
    id: int
    kind: str
    address: Mapping[str, str | int]   # включая scheme

class Address(BaseModel, ABC):
    """База адреса узла: части для nodes.address и каноническая строка.

    Наследуют модели адресов пакетов источников (ConfluenceAddress,
    PgAddress, ChAddress). parts() — в jsonb при записи, типы частей
    канонизирует наследник (port: int); render() — строка по грамматике
    схемы (3.6), её знает только наследник. Обратные направления — из
    jsonb через model_validate, из строки через parse() наследника; ядру
    они не нужны: узел оно ищет по parts(). Лишних частей нет: параметр
    подключения или неизвестная роль — ошибка валидации, не часть адреса.
    Грамматику строки ядро не знает — ни web, ни баз.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: str

    def parts(self) -> Mapping[str, str | int]:
        return self.model_dump(by_alias=True)

    @abstractmethod
    def render(self) -> str: ...

class Evidence(BaseModel):
    """База обоснования ребра — то, что лежит в edges.evidence.

    Наследует модель каждого вида ребра (3.4); ядро зовёт только dump()
    при записи, обратно jsonb читает тот, кто знает вид: инструмент показа
    отдаёт как есть, kb_graph_check сверяет параметры расчёта по ключам.
    """

    def dump(self) -> Mapping[str, object]:
        return self.model_dump()
```

### 2.1 Модели источников — в пакетах источников

Вид узла и его адрес — знание об объектах источника, и оно лежит там, где
уже лежит остальное знание о нём: модели страниц Confluence — в
`boba-confluence`, каталог PostgreSQL — в `boba-db-postgres`, ClickHouse —
в `boba-db-clickhouse`. Пакет источника о графе и корпусе не знает;
единственная его зависимость на `boba-graph` — база `Address`, и
направление слоёв (core ← infra) соблюдено. Строку адреса собирает и разбирает база адресов пакета: `PgAddress` и
`ChAddress` — на `urllib.parse`, `ConfluenceAddress` — на `httpx.URL`,
где `httpx` уже есть. Ядро грамматик не знает. Движки, у
которых пакета ещё нет (MSSQL, Oracle, MySQL), придут со своими
`boba-db-*` и своими перечислениями; общего «перечисления всех движков»
не будет ни в одном пакете.

```python
# boba-confluence: boba/confluence/nodes.py
class ConfluenceNodeKind(StrEnum):
    SPACE = "confluence_space"
    PAGE = "confluence_page"
    ATTACHMENT = "confluence_attachment"

class WebScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"

    def default_port(self) -> int:
        if self is WebScheme.HTTP:
            return 80

        return 443

class ConfluenceAddress(Address):
    """Адрес объекта Confluence: REST-путь на сервере.

    Части — схема, хост, порт, путь; query, фрагмент и учётные данные в
    адрес не входят (как у SourceId ридера). Порт в частях всегда, в строке
    httpx опускает порт по умолчанию схемы: https://host/path. Сборка и
    разбор — httpx.URL, единственное место для адресов Confluence.
    """

    scheme: WebScheme
    host: str
    port: int
    path: str

    def render(self) -> str:
        url = httpx.URL(scheme=self.scheme.value, host=self.host, port=self.port, path=self.path)
        return str(url)

    @classmethod
    def parse(cls, text: str) -> Self:
        try:
            url = httpx.URL(text)
        except httpx.InvalidURL as exc:
            raise AddressError(f"confluence address {text!r}: {exc}") from exc

        if url.userinfo:
            raise AddressError(f"confluence address {text!r}: credentials are not part of an address")

        if url.query:
            raise AddressError(f"confluence address {text!r}: query is not part of an address")

        if url.fragment:
            raise AddressError(f"confluence address {text!r}: fragment is not part of an address")

        if not url.host:
            raise AddressError(f"confluence address {text!r}: host is required")

        try:
            scheme = WebScheme(url.scheme)
        except ValueError as exc:
            raise AddressError(f"confluence address {text!r}: expected scheme http or https, got {url.scheme!r}") from exc

        port = url.port
        if port is None:
            port = scheme.default_port()

        try:
            return cls(scheme=scheme, host=url.host, port=port, path=url.path)
        except ValidationError as exc:
            raise AddressError(f"{cls.__name__}: address {text!r} is not valid: {exc}") from exc

class SpaceAddress(ConfluenceAddress):
    PATH_RE: ClassVar[re.Pattern[str]] = re.compile(r"/rest/api/space/[^/?#]+$")

    @field_validator("path")
    @classmethod
    def _space_path(cls, value: str) -> str:
        if cls.PATH_RE.search(value) is None:
            raise ValueError(f"confluence space address expects /rest/api/space/<key>, got {value!r}")

        return value

class PageAddress(ConfluenceAddress):
    PATH_RE: ClassVar[re.Pattern[str]] = re.compile(r"/rest/api/content/[^/?#]+$")   # тот же шаблон, что у SourceId.page_id_of

    @field_validator("path")
    @classmethod
    def _content_path(cls, value: str) -> str:
        if cls.PATH_RE.search(value) is None:
            raise ValueError(f"confluence page address expects /rest/api/content/<id>, got {value!r}")

        return value

class AttachmentAddress(ConfluenceAddress):
    PATH_RE: ClassVar[re.Pattern[str]] = re.compile(r"/download/attachments/[^/?#]+/[^/?#]+$")

    @field_validator("path")
    @classmethod
    def _download_path(cls, value: str) -> str:
        if cls.PATH_RE.search(value) is None:
            raise ValueError(f"confluence attachment address expects /download/attachments/<page>/<file>, got {value!r}")

        return value

class SpaceNode(BaseModel):
    kind: Literal[ConfluenceNodeKind.SPACE]
    address: SpaceAddress

class PageNode(BaseModel):
    kind: Literal[ConfluenceNodeKind.PAGE]
    address: PageAddress

class AttachmentNode(BaseModel):
    kind: Literal[ConfluenceNodeKind.ATTACHMENT]
    address: AttachmentAddress

ConfluenceNode = Annotated[SpaceNode | PageNode | AttachmentNode, Field(discriminator="kind")]
```

```python
# boba-db-postgres: boba/db/postgres/nodes.py
class PgNodeKind(StrEnum):
    DATABASE = "pg_database"
    SCHEMA = "pg_schema"
    TABLE = "pg_table"
    VIEW = "pg_view"
    MATVIEW = "pg_matview"
    COLUMN = "pg_column"
    INDEX = "pg_index"
    CONSTRAINT = "pg_constraint"
    FUNCTION = "pg_function"
    PROCEDURE = "pg_procedure"
    TRIGGER = "pg_trigger"
    SEQUENCE = "pg_sequence"

class PgAddress(Address):
    """База адресов объектов PostgreSQL: postgresql://host:port/database?роль=имя&… (3.6).

    Часть подключения — libpq URI, объект внутри базы — query-параметры
    с ролью в имени в порядке объявления полей наследника; один класс на
    строку списка 3.6. Сборка и разбор — urllib.parse, здесь и только здесь.
    """

    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset({"scheme", "host", "port", "database"})

    scheme: Literal["postgresql"]
    host: str
    port: int
    database: str

    @classmethod
    def roles(cls) -> Sequence[str]:
        """Роли объекта — поля наследника после полей подключения, в порядке объявления, по alias."""
        names: list[str] = []
        for name, field in cls.model_fields.items():
            if name in cls.BASE_FIELDS:
                continue

            alias = field.alias
            if alias is None:
                alias = name

            names.append(alias)

        return names

    def render(self) -> str:
        query = urlencode(self.model_dump(by_alias=True, exclude=self.BASE_FIELDS), quote_via=quote)
        split = SplitResult(
            scheme=self.scheme,
            netloc=self._netloc(),
            path="/" + quote(self.database, safe=""),
            query=query,
            fragment="",
        )
        return urlunsplit(split)

    def _netloc(self) -> str:
        host = self.host
        if ":" in host:                      # IPv6 — в скобках, RFC 3986 §3.2.2
            host = f"[{host}]"

        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> Self:
        """Строка → адрес этого класса; канон 3.6: без учётных данных, с портом, path = /database, роли по составу и порядку."""
        url = urlsplit(text)
        if url.scheme != "postgresql":
            raise AddressError(f"postgresql address {text!r}: expected scheme postgresql, got {url.scheme!r}")

        if url.username is not None:
            raise AddressError(f"postgresql address {text!r}: credentials are not part of an address")

        if url.fragment:
            raise AddressError(f"postgresql address {text!r}: fragment is not part of an address")

        host = url.hostname
        if host is None:
            raise AddressError(f"postgresql address {text!r}: host is required")

        try:
            port = url.port
        except ValueError as exc:
            raise AddressError(f"postgresql address {text!r}: port is not a number: {exc}") from exc

        if port is None:
            raise AddressError(f"postgresql address {text!r}: port is required")

        database = unquote(url.path.removeprefix("/"))
        if not database:
            raise AddressError(f"postgresql address {text!r}: path must be /<database>, got {url.path!r}")

        if "/" in database:
            raise AddressError(f"postgresql address {text!r}: path must be a single segment /<database>, got {url.path!r}")

        roles = parse_qsl(url.query, keep_blank_values=True)
        given: list[str] = []
        for name, _ in roles:
            given.append(name)

        expected = list(cls.roles())
        if given != expected:
            raise AddressError(f"{cls.__name__}: address {text!r} expects roles {expected}, got {given}")

        parts: dict[str, str | int] = {"scheme": url.scheme, "host": host, "port": port, "database": database}
        parts.update(roles)
        try:
            return cls.model_validate(parts)
        except ValidationError as exc:
            raise AddressError(f"{cls.__name__}: address {text!r} is not valid: {exc}") from exc

class PgDatabaseAddress(PgAddress): ...

class PgSchemaAddress(PgAddress):
    schema_name: str = Field(alias="schema")

class PgTableAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    table: str

class PgTableColumnAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    table: str
    column: str

class PgViewColumnAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    view: str
    column: str

class PgIndexAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    index: str

class PgFunctionAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    function: str
    args: str                                  # pg_get_function_identity_arguments; пустая строка обязательна

class PgConstraintAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    table: str
    constraint: str

# … view, matview и его колонка, sequence, procedure, trigger — по строке 3.6 каждый

class PgAddresses:
    """Строка → адрес конкретного объекта PostgreSQL: класс выбирается по составу ролей в query."""

    MODELS: ClassVar[Sequence[type[PgAddress]]] = (
        PgDatabaseAddress, PgSchemaAddress, PgTableAddress, PgTableColumnAddress, PgViewColumnAddress,
        PgIndexAddress, PgFunctionAddress, PgConstraintAddress,
    )

    @classmethod
    def parse(cls, text: str) -> PgAddress:
        given: list[str] = []
        for name, _ in parse_qsl(urlsplit(text).query, keep_blank_values=True):
            given.append(name)

        for model in cls.MODELS:
            if list(model.roles()) == given:
                return model.parse(text)

        raise AddressError(f"postgresql address {text!r}: no object has roles {given}")

class PgTableNode(BaseModel):
    kind: Literal[PgNodeKind.TABLE]
    address: PgTableAddress

class PgColumnNode(BaseModel):
    kind: Literal[PgNodeKind.COLUMN]
    address: PgTableColumnAddress | PgViewColumnAddress | PgMatviewColumnAddress   # колонка чьей-то реляции; pydantic различит по ролям

PgNode = Annotated[PgDatabaseNode | PgSchemaNode | PgTableNode | PgColumnNode | ..., Field(discriminator="kind")]
```

```python
# boba-db-clickhouse: boba/db/clickhouse/nodes.py
class ChNodeKind(StrEnum):
    DATABASE = "ch_database"
    TABLE = "ch_table"
    VIEW = "ch_view"
    MATVIEW = "ch_matview"
    COLUMN = "ch_column"
    INDEX = "ch_index"          # skip-индекс, внутри таблицы
    PROJECTION = "ch_projection"
    DICTIONARY = "ch_dictionary"
    FUNCTION = "ch_function"

class ChAddress(Address):
    """База адресов объектов ClickHouse: clickhouse://host:port/database?роль=имя&… (3.6).

    Схем нет, объекты сразу в базе; грамматика та же, что у PgAddress, со
    своей схемой. Сборка и разбор — urllib.parse, здесь и только здесь.
    """

    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset({"scheme", "host", "port", "database"})

    scheme: Literal["clickhouse"]
    host: str
    port: int
    database: str

    @classmethod
    def roles(cls) -> Sequence[str]:
        """Роли объекта — поля наследника после полей подключения, в порядке объявления, по alias."""
        names: list[str] = []
        for name, field in cls.model_fields.items():
            if name in cls.BASE_FIELDS:
                continue

            alias = field.alias
            if alias is None:
                alias = name

            names.append(alias)

        return names

    def render(self) -> str:
        query = urlencode(self.model_dump(by_alias=True, exclude=self.BASE_FIELDS), quote_via=quote)
        split = SplitResult(
            scheme=self.scheme,
            netloc=self._netloc(),
            path="/" + quote(self.database, safe=""),
            query=query,
            fragment="",
        )
        return urlunsplit(split)

    def _netloc(self) -> str:
        host = self.host
        if ":" in host:                      # IPv6 — в скобках, RFC 3986 §3.2.2
            host = f"[{host}]"

        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> Self:
        """Строка → адрес этого класса; канон 3.6: без учётных данных, с портом, path = /database, роли по составу и порядку."""
        url = urlsplit(text)
        if url.scheme != "clickhouse":
            raise AddressError(f"clickhouse address {text!r}: expected scheme clickhouse, got {url.scheme!r}")

        if url.username is not None:
            raise AddressError(f"clickhouse address {text!r}: credentials are not part of an address")

        if url.fragment:
            raise AddressError(f"clickhouse address {text!r}: fragment is not part of an address")

        host = url.hostname
        if host is None:
            raise AddressError(f"clickhouse address {text!r}: host is required")

        try:
            port = url.port
        except ValueError as exc:
            raise AddressError(f"clickhouse address {text!r}: port is not a number: {exc}") from exc

        if port is None:
            raise AddressError(f"clickhouse address {text!r}: port is required")

        database = unquote(url.path.removeprefix("/"))
        if not database:
            raise AddressError(f"clickhouse address {text!r}: path must be /<database>, got {url.path!r}")

        if "/" in database:
            raise AddressError(f"clickhouse address {text!r}: path must be a single segment /<database>, got {url.path!r}")

        roles = parse_qsl(url.query, keep_blank_values=True)
        given: list[str] = []
        for name, _ in roles:
            given.append(name)

        expected = list(cls.roles())
        if given != expected:
            raise AddressError(f"{cls.__name__}: address {text!r} expects roles {expected}, got {given}")

        parts: dict[str, str | int] = {"scheme": url.scheme, "host": host, "port": port, "database": database}
        parts.update(roles)
        try:
            return cls.model_validate(parts)
        except ValidationError as exc:
            raise AddressError(f"{cls.__name__}: address {text!r} is not valid: {exc}") from exc

class ChDatabaseAddress(ChAddress): ...

class ChTableAddress(ChAddress):
    table: str

class ChTableColumnAddress(ChAddress):
    table: str
    column: str

class ChIndexAddress(ChAddress):               # skip-индекс уникален внутри таблицы — после table
    table: str
    index: str

class ChDictionaryAddress(ChAddress):
    dictionary: str

class ChFunctionAddress(ChAddress):            # перегрузок нет — args не нужен
    function: str

# … view, matview и их колонки, projection — по строке 3.6 каждый

class ChAddresses:
    """Строка → адрес конкретного объекта ClickHouse: класс по составу ролей, как PgAddresses."""

    MODELS: ClassVar[Sequence[type[ChAddress]]] = (
        ChDatabaseAddress, ChTableAddress, ChTableColumnAddress, ChIndexAddress, ChDictionaryAddress, ChFunctionAddress,
    )

    @classmethod
    def parse(cls, text: str) -> ChAddress:
        given: list[str] = []
        for name, _ in parse_qsl(urlsplit(text).query, keep_blank_values=True):
            given.append(name)

        for model in cls.MODELS:
            if list(model.roles()) == given:
                return model.parse(text)

        raise AddressError(f"clickhouse address {text!r}: no object has roles {given}")

class ChColumnNode(BaseModel):
    kind: Literal[ChNodeKind.COLUMN]
    address: ChTableColumnAddress | ChViewColumnAddress | ChMatviewColumnAddress

ChNode = Annotated[ChDatabaseNode | ChTableNode | ChColumnNode | ..., Field(discriminator="kind")]
```

Граница — на входе методов корпуса: `Node` ядра разбирается union'ом
источника (`ConfluenceNode`, `PgNode`, `ChNode`) в типизированную модель,
на выходе собирается обратно. Наследник `Address` канонизирует типы
частей (`port: int`) и один знает грамматику своей строки (3.6). Строку, о которой не
известно, какой объект она называет (ввод `kb_node`), разбирают
`PgAddresses.parse` и `ChAddresses.parse`: класс адреса выбирается по
составу ролей в query, и это единственная точка такого разбора в пакете.

### 2.2 Индексы поиска

Индекс поиска — и объявление, и исполнитель: он знает свои таблицу и
колонки и сам собирает по ним запрос. Ядро объявляет протокол индекса без
привязки к драйверу: готовый запрос драйвера — параметр типа, ядро его не
разбирает, ничего постгресового в протоколе нет. Реализации для Postgres
живут в `boba-db-pggraph` (2.4) и держат схему своим полем; корпус создаёт
их экземпляры при старте из своего конфига (`[storage] pg_schema`) и
отдаёт тремя группами по типу зонда (2.3).

К индексу приходят с разным входом: пользователь печатает текст, а
ребро близости приносит готовый вектор соседнего узла, ребро упоминания —
его заголовок. Поэтому у индекса свой **зонд** — то, чем он ищет в своей
таблице: у полнотекстового и точного это строка, у векторного — вектор.
Индекс отвечает только за запрос по зонду; кто делает зонд — зависит от
вызова, и это видно в таблице после объявлений.

```python
# boba-graph — ядро: зонды и векторы — общие модели данных, SQL нет
class Vector(BaseModel):
    """Общее у любого вектора: какой моделью он посчитан. Подклассы — форма чисел."""

    model: str                              # имя из embedding_models; ставит энкодер или строка таблицы

class DenseVector(Vector):
    """Плотный вектор: число на каждую координату пространства модели.

    Столько координат, сколько у модели размерность (embedding_models.dim):
    у e5-large — 1024 числа, у text-embedding-3-large — 3072. Сравнивается
    косинусом или скалярным произведением; в базе — pgvector vector(dim).
    """

    values: Sequence[float]                 # координаты по порядку, len(values) == dim модели

class SparseVector(Vector):
    """Разреженный вектор: почти все координаты — нули, хранятся только ненулевые.

    Координата — слово (токен) словаря модели, значение — его вес в тексте;
    словарь SPLADE — десятки тысяч токенов, в тексте ненулевых — десятки.
    Поэтому хранятся пары «номер координаты — вес», а не весь ряд нулей;
    в базе — pgvector sparsevec, текстовая форма {i1:v1,i2:v2,…}/dim.
    """

    indices: Sequence[int]                  # номера ненулевых координат по возрастанию: id токенов словаря модели
    values: Sequence[float]                 # веса тех же координат, len(values) == len(indices)
    dim: int                                # полная размерность пространства — размер словаря модели; нужна sparsevec

V = TypeVar("V", bound=Vector)                          # обобщённые функции и dataclass'ы
V_co = TypeVar("V_co", bound=Vector, covariant=True)    # протоколы: вектор только на выходе — pyright требует covariant

class Probe(BaseModel):
    """Зонд — то, чем индекс ищет, вместе с рамками запроса.

    Базовый класс держит общее: лимит кандидатов с этого индекса до
    слияния (не итоговый top_k инструмента). Порог по счёту, смещение,
    фильтр по виду узла добавятся полями сюда, не меняя сигнатуру
    statement у реализаций. Подклассы добавляют сам зонд.
    """

    limit: int = Field(gt=0)

class TextProbe(Probe):
    text: str

class DenseProbe(Probe):
    vector: DenseVector

class SparseProbe(Probe):
    vector: SparseVector

P = TypeVar("P", bound=Probe)                                   # реализация типизирована своим зондом: statement(probe: TextProbe)
P_contra = TypeVar("P_contra", bound=Probe, contravariant=True)  # протоколы: зонд только на входе

class VectorEncoder(Protocol[V_co]):
    """Текст пользователя -> вектор; один метод, назначение — в классе реализации."""
    async def encode(self, text: str) -> V_co: ...

class VectorEncoderRegistry(Protocol):
    """Энкодер по имени модели из embedding_models; форму вектора выбирает метод.

    Собирается при старте из строк embedding_models и [encoders]; dense и
    sparse сверяют modality строки с запрошенной формой: рассогласование —
    ошибка конфига с именем модели, а не пустая выдача в запросе.
    """

    def dense(self, model: str) -> VectorEncoder[DenseVector]: ...
    def sparse(self, model: str) -> VectorEncoder[SparseVector]: ...

class SearchHit(BaseModel):
    node_id: int
    row_id: int
    text: str          # что показать как цитату
    rank: int          # позиция в списке своего индекса; счёт между индексами не переносится

S = TypeVar("S")                              # готовый запрос драйвера целиком: у psycopg — PgStatement (sql.Composed + параметры)
S_co = TypeVar("S_co", covariant=True)        # протоколы: запрос только на выходе

class SqlIndex(Protocol[P_contra, S_co]):
    """Индекс поиска: сборка своего запроса плюс то, что нужно слиянию и выдаче.

    Протокол требует ровно то, чем пользуется обобщённый код, и только
    методами. statement — запрос, который SearchStore исполняет. weight —
    насколько попадание из этого индекса весомее попаданий из других при
    слиянии списков: точное совпадение заголовка значит больше, чем
    совпадение по абзацу раздела (раздел 8, RRF). label — чем узел найден,
    для выдачи и логов: «title/exact», «summary/vector» — по этому
    пользователь и модель судят, насколько доверять попаданию. Откуда
    реализация берёт вес и подпись — из своих полей, из конфига, из
    констант класса — её дело. Что индекс ищет, по какой таблице, каким
    способом — тоже знает только она. Что такое готовый запрос (S) — знает
    только SearchStore того же драйвера: ядро его не разбирает, поэтому
    протокола запроса в ядре нет.
    """

    def statement(self, probe: P_contra) -> S_co: ...
    def weight(self) -> float: ...     # вес попаданий этого индекса при слиянии списков
    def label(self) -> str: ...        # чем найдено: "title/exact", "summary/vector"

class VectorSqlIndex(SqlIndex[DenseProbe, S_co], Protocol[S_co]):
    """Индекс над таблицей плотных векторов одной модели.

    model — имя этой модели в embedding_models: ею же SearchStore считает
    зонд из текста (VectorEncoderRegistry.dense), один раз на модель для
    всех индексов, что её делят.
    """

    def model(self) -> str: ...

class SparseSqlIndex(SqlIndex[SparseProbe, S_co], Protocol[S_co]):
    def model(self) -> str: ...
```

Кто с чем приходит к индексу, по объявленным выше типам (реализации
`Pg*Index` — 2.4, `similarity_index()` и `title_index()` — методы
`Corpus`, 2.3):

| вызов | индекс | зонд |
|---|---|---|
| `kb_search`, текст пользователя | `PgFtsIndex`, `PgTrigramIndex`, `PgExactIndex`, `PgBm25Index` | `TextProbe` со строкой запроса |
| `kb_search` | `PgVectorIndex`, `PgSparseIndex` | `DenseProbe` / `SparseProbe`: вектор из строки моделью индекса |
| `kb_search` | `PgImageVectorIndex` | `DenseProbe`: вектор из строки текстовым энкодером CLIP |
| ребро `similar` (ядро) | `similarity_index()` | `DenseProbe` с готовым вектором соседнего узла |
| ребро `mention` (ядро) | `title_index()` | `TextProbe` с заголовком другого узла, `limit = 1` |
| ребро `same_column` (корпус) | `PgExactIndex` над `columns.title` | `TextProbe` с именем колонки |

### 2.3 Протокол корпуса

Протокол корпуса и то, что через него ходит. Ребро от корпуса приходит
черновиком с адресом цели (наследник `Address`, 2.1) и обоснованием
(наследник `Evidence`, 3.4): id цели ядро находит само по `parts()`.
Исходник узла корпус отдаёт в виде для большой модели (раздел 5):

```python
class EdgeDraft(BaseModel):
    """Ребро до записи: цель адресом, id цели ядро найдёт по target.parts()."""

    target: Address
    kind: str
    weight: float
    evidence: Evidence

class SourceView(BaseModel):
    """Исходник узла в виде для большой модели: заголовок, текст в markdown, ссылка на источник."""

    title: str
    text: str
    url: str

class Corpus(Protocol[S_co]):
    """Всё, что ядро спрашивает у корпуса; реализация — пакет корпуса.

    S — готовый запрос драйвера хранилища, которым корпус пользуется:
    ConfluenceCorpus(Corpus[PgStatement]). Индексы отдаются
    группами по типу зонда: P у SqlIndex стоит в позиции аргумента и
    контравариантен, поэтому один список индексов с разными зондами не
    типизируется без Any; три группы типизируются точно, и у каждой свой
    сборщик зондов в SearchStore.
    """

    node_kinds: type[StrEnum]
    text_kinds: type[StrEnum]
    edge_kinds: type[StrEnum]

    def text_indexes(self) -> Sequence[SqlIndex[TextProbe, S_co]]: ...   # fts, trigram, exact, bm25
    def dense_indexes(self) -> Sequence[VectorSqlIndex[S_co]]: ...       # vector, image_vector
    def sparse_indexes(self) -> Sequence[SparseSqlIndex[S_co]]: ...      # sparse
    def title_index(self) -> SqlIndex[TextProbe, S_co]: ...              # имена узлов: MENTION и NAMING
    def similarity_index(self) -> VectorSqlIndex[S_co]: ...              # чей вектор берёт similar
    def entity_edge_kind(self) -> str: ...               # как корпус называет ребро по общим сущностям
    def similar_edge_kind(self) -> str: ...              # как корпус называет ребро по близости векторов
    def entity_texts(self, node_id: int) -> Sequence[str]: ...   # из чего извлекать сущности
    def explicit_edges(self, node: Node) -> Iterable[EdgeDraft]: ...
    def resolve(self, node: Node) -> SourceView: ...

S_contra = TypeVar("S_contra", contravariant=True)   # протоколы: запрос корпуса только на входе

class Seed(BaseModel):
    node_id: int
    score: float                 # s_base после RRF
    best: SearchHit              # лучшее попадание: будущая цитата
    found_by: Sequence[str]      # label() индексов, где узел встретился

class SearchStore(Protocol[S_contra]):
    """Опорные узлы по тексту запроса; реализация — у драйвера (PgSearchStore).

    Corpus[S] стоит в позиции аргумента, поэтому S здесь контравариантен.
    """

    async def seeds(self, corpus: Corpus[S_contra], query: str, top_k: int) -> Sequence[Seed]: ...
```

### 2.4 Индексы в Postgres

```python
# boba-db-pggraph — реализации Pg*Index: S = PgStatement, схема Postgres —
# поле schema каждой реализации, шаблон — свойство класса, остальные поля — экземпляра.
# Объявления — frozen dataclass с явным наследованием протокола (правило §14:
# pydantic-модель протокол наследовать не может).
# Ошибки пакета наружу:
# SearchIndexError — индексу дали зонд не той модели или таблица индекса не найдена.
# EncoderConfigError — модели нет в embedding_models или её modality не сходится с запросом.
# SearchStoreError — запрос индекса упал в Postgres; текст — имя индекса, таблица, ошибка psycopg.

@dataclass(frozen=True)
class PgStatement:
    """Готовый запрос psycopg: собранный sql.Composed и его именованные параметры.

    Собирают Pg*Index.statement(), исполняет PgSearchStore._run(); ядро
    видит его только как параметр типа S.
    """

    query: sql.Composed
    params: Mapping[str, object]

@dataclass(frozen=True)
class PgFtsIndex(SqlIndex[TextProbe, PgStatement]):      # tsvector + GIN, ts_rank_cd
    name: str                             # "section/fts": подпись для выдачи и ключ веса в [search.weights]
    rrf_weight: float                     # из [search.weights] корпуса по name
    schema: str                           # схема корпуса из [storage]: confluence | confluence_test — деталь реализации
    table: str                            # таблица content tables: page_sections
    node_column: str                      # колонка со ссылкой на nodes.id
    row_column: str                       # ключ строки: id у page_sections, node_id у pages
    text_column: str                      # колонка текста, отдаваемого в выдачу
    tsv_column: str

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    TEMPLATE: ClassVar[LiteralString] = """
        with q as (
            select
                websearch_to_tsquery('russian', unaccent(%(text)s))
                || websearch_to_tsquery('english', unaccent(%(text)s)) as tsq
        )
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as text,
            ts_rank_cd(t.{tsv}, q.tsq) as score
        from
            {schema}.{table} t,
            q
        where
            t.{tsv} @@ q.tsq
        order by
            score desc
        limit %(limit)s
    """

    def statement(self, probe: TextProbe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column), tsv=sql.Identifier(self.tsv_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgModelIndex:
    """База индексов над таблицей векторов: имя модели таблицы и проверка зонда.

    Наследуют PgVectorIndex, PgSparseIndex, PgImageVectorIndex; имя модели
    приходит из конфига корпуса.
    """

    model_name: str

    def model(self) -> str:
        return self.model_name

    def expect(self, vector: Vector, index: str, table: str) -> None:
        """Вектор другой модели — ошибка вызова, не пустая выдача."""
        if vector.model == self.model_name:
            return

        raise SearchIndexError(
            f"index {index} on {table} expects vectors of "
            f"model {self.model_name!r}, got {vector.model!r}"
        )

@dataclass(frozen=True)
class PgVectorIndex(PgModelIndex, VectorSqlIndex[PgStatement]):   # pgvector + HNSW; таблица векторов — на одну модель, фильтра по модели в запросе нет
    name: str
    rrf_weight: float
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str
    vector_table: str                     # таблица векторов этой поверхности и этой модели: page_section_vectors__e5
    ref_column: str                       # ссылка на row_column

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as text,
            v.embedding <=> %(vector)s::vector as score
        from
            {schema}.{vectors} v
            join {schema}.{table} t on
                t.{row} = v.{ref}
        order by
            v.embedding <=> %(vector)s::vector
        limit %(limit)s
    """

    def statement(self, probe: DenseProbe) -> PgStatement:
        self.expect(probe.vector, self.name, self.table)

        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), vectors=sql.Identifier(self.vector_table),
            table=sql.Identifier(self.table), node=sql.Identifier(self.node_column),
            row=sql.Identifier(self.row_column), text=sql.Identifier(self.text_column),
            ref=sql.Identifier(self.ref_column),
        )
        params = {"vector": list(probe.vector.values), "limit": probe.limit}
        return PgStatement(query=composed, params=params)

@dataclass(frozen=True)
class PgTrigramIndex(SqlIndex[TextProbe, PgStatement]):   # pg_trgm + GiST (gist_trgm_ops): опечатки, склонения, части имён.
                                                             # GiST, а не GIN: только он даёт top-N по <-> прямо из индекса (KNN);
                                                             # % отсекает мусор по pg_trgm.similarity_threshold; %% — экранированный %
    name: str
    rrf_weight: float
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as text,
            1 - (t.{text} <-> %(text)s) as score
        from
            {schema}.{table} t
        where
            t.{text} %% %(text)s
        order by
            t.{text} <-> %(text)s
        limit %(limit)s
    """

    def statement(self, probe: TextProbe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgExactIndex(SqlIndex[TextProbe, PgStatement]):     # btree по lower(text): MENTION, NAMING, коды вида FLIP-457
    name: str
    rrf_weight: float
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as text,
            1.0 as score
        from
            {schema}.{table} t
        where
            lower(t.{text}) = lower(%(text)s)
        limit %(limit)s
    """

    def statement(self, probe: TextProbe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgBm25Index(SqlIndex[TextProbe, PgStatement]):      # pg_search (ParadeDB): BM25 с нормировкой по длине; только если расширение стоит
    name: str
    rrf_weight: float
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str
    index_name: str                        # индекс bm25 над таблицей; нужен установке, запрос идёт через оператор @@@

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as text,
            paradedb.score(t.{row}) as score
        from
            {schema}.{table} t
        where
            t.{text} @@@ %(text)s
        order by
            score desc
        limit %(limit)s
    """

    def statement(self, probe: TextProbe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgSparseIndex(PgModelIndex, SparseSqlIndex[PgStatement]):   # pgvector sparsevec + HNSW (sparsevec_ip_ops): SPLADE / BM42; таблица на модель;
                                                       # <#> — отрицательное скалярное произведение: меньше — ближе
    name: str
    rrf_weight: float
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str
    vector_table: str
    ref_column: str

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as text,
            v.embedding <#> %(vector)s::sparsevec as score
        from
            {schema}.{vectors} v
            join {schema}.{table} t on
                t.{row} = v.{ref}
        order by
            v.embedding <#> %(vector)s::sparsevec
        limit %(limit)s
    """

    def statement(self, probe: SparseProbe) -> PgStatement:
        self.expect(probe.vector, self.name, self.table)

        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), vectors=sql.Identifier(self.vector_table),
            table=sql.Identifier(self.table), node=sql.Identifier(self.node_column),
            row=sql.Identifier(self.row_column), text=sql.Identifier(self.text_column),
            ref=sql.Identifier(self.ref_column),
        )
        params = {"vector": SparseVectorText.render(probe.vector), "limit": probe.limit}
        return PgStatement(query=composed, params=params)
        # SparseVectorText.render: '{i1:v1,i2:v2,…}/dim' — текстовая форма sparsevec; одна точка сборки

@dataclass(frozen=True)
class PgImageVectorIndex(PgModelIndex, VectorSqlIndex[PgStatement]):
    """Поиск картинок вложений по смыслу: текст запроса → вектор в пространстве
    картинок (SigLIP/CLIP), ближайшие векторы картинок в pgvector.

    Текста у картинки нет, поэтому в выдачу идёт title_column — имя файла;
    content_column с байтами в запросе не участвует, он нужен стадии
    индексации, которая считает вектор картинки ImageEncoder той же модели.
    """

    name: str                              # "image/image_vector": подпись выдачи и ключ веса в [search.weights]
    rrf_weight: float                      # вес попаданий этого индекса при слиянии, из конфига по name
    schema: str                            # схема Postgres корпуса: confluence | confluence_test
    table: str                             # attachment_images
    node_column: str                       # ссылка на nodes.id
    row_column: str                        # ключ строки картинки
    title_column: str                      # что показать в выдаче: имя файла
    content_column: str                    # bytea картинки; в запросе не участвует
    vector_table: str                      # attachment_image_vectors__siglip: таблица на модель
    ref_column: str                        # колонка векторной таблицы, ссылающаяся на row_column

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{title} as text,
            v.embedding <=> %(vector)s::vector as score
        from
            {schema}.{vectors} v
            join {schema}.{table} t on
                t.{row} = v.{ref}
        order by
            v.embedding <=> %(vector)s::vector
        limit %(limit)s
    """

    def weight(self) -> float:
        return self.rrf_weight

    def label(self) -> str:
        return self.name

    def statement(self, probe: DenseProbe) -> PgStatement:
        self.expect(probe.vector, self.name, self.table)

        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), vectors=sql.Identifier(self.vector_table),
            table=sql.Identifier(self.table), node=sql.Identifier(self.node_column),
            row=sql.Identifier(self.row_column), title=sql.Identifier(self.title_column),
            ref=sql.Identifier(self.ref_column),
        )
        params = {"vector": list(probe.vector.values), "limit": probe.limit}
        return PgStatement(query=composed, params=params)
```

Имена таблиц и колонок подставляются как `sql.Identifier`, значения — как
параметры: инъекции через объявление нет по построению, а `LiteralString`
в `ClassVar` не даёт собрать шаблон из строк на лету. Один класс
обслуживает любую таблицу: `PgFtsIndex` для `pages.title_tsv` и для
`page_sections.tsv` — два экземпляра. Новый способ поиска — новый класс с
полями и `TEMPLATE`; ни ядро, ни `SearchStore` не меняются. Исходные
данные не всегда текст: у `PgImageVectorIndex` колонка байтов, а не текста,
поэтому общего класса местоположения нет — каждый индекс объявляет свои
поля.

### 2.5 Энкодеры

Энкодеры живут в `boba-llm` и одинаково служат индексации (вектор
документа) и поиску (вектор запроса). Строка `embedding_models` описывает
модель — что она такое; конфиг корпуса описывает, где её веса и как к ней
подключаться; реестр соединяет их по имени:

```python
class EmbeddingModel(BaseModel):            # строка embedding_models
    id: int
    name: str
    slug: str                               # суффикс имён таблиц векторов: e5
    provider: str                           # local | openai
    modality: str                           # text | image | sparse
    revision: str
    dim: int
    index_distance: str
    normalize: bool
    max_tokens: int
    query_prefix: str
    passage_prefix: str

class TextEmbedder(VectorEncoder[DenseVector]):
    """e5, bge: префикс запроса, обрезка по max_tokens, нормировка — из строки модели;
    сам расчёт — существующий порт boba.llm.embedding (fastembed локально или openai)."""

    def __init__(self, spec: EmbeddingModel, backend: Embedder[str]) -> None: ...

    async def encode(self, text: str) -> DenseVector:
        prefixed = self._spec.query_prefix + self._truncate(text)
        values = await self._backend.embed_query(prefixed)
        if self._spec.normalize:
            values = self._normalized(values)
        return DenseVector(values=values)

class ClipTextEncoder(VectorEncoder[DenseVector]):
    """SigLIP/CLIP, текстовая башня на onnxruntime: вектор в общем с картинками пространстве.
    Парный ImageEncoder(bytes) -> DenseVector той же моделью зовёт стадия индексации вложений."""

    def __init__(self, spec: EmbeddingModel, session: OnnxSession, tokenizer: Tokenizer) -> None: ...

    async def encode(self, text: str) -> DenseVector: ...

class SparseEncoder(VectorEncoder[SparseVector]):
    """SPLADE / BM42 на onnxruntime: веса термов по словарю модели -> sparsevec."""

    def __init__(self, spec: EmbeddingModel, session: OnnxSession, tokenizer: Tokenizer) -> None: ...

    async def encode(self, text: str) -> SparseVector: ...

class PgVectorEncoderRegistry(VectorEncoderRegistry):
    """Собирается при старте из строк embedding_models и секции [encoders] конфига;
    экземпляры прогреваются один раз и живут весь прогон."""

    def dense(self, model: str) -> VectorEncoder[DenseVector]:
        if model not in self._dense:                    # собраны при старте по (modality, provider) строки
            raise EncoderConfigError(f"embedding_models: dense model {model!r} is not registered: known {sorted(self._dense)}")

        return self._dense[model]

    def sparse(self, model: str) -> VectorEncoder[SparseVector]:
        if model not in self._sparse:
            raise EncoderConfigError(f"embedding_models: sparse model {model!r} is not registered: known {sorted(self._sparse)}")

        return self._sparse[model]
```

Что берётся откуда: `modality` и `provider` выбирают класс (`text`+`local`
→ `TextEmbedder` над fastembed, `text`+`openai` → `TextEmbedder` над
HTTP, `image` → `ClipTextEncoder`, `sparse` → `SparseEncoder`);
`query_prefix`, `max_tokens`, `normalize`, `dim` — из строки; каталог
весов или endpoint и ключ — из конфига:

```toml
[encoders.models."multilingual-e5-large"]
    model_dir = "${env.models}/fastembed/multilingual-e5-large"
[encoders.models."siglip-so400m"]
    model_dir = "${env.models}/onnx/siglip-so400m"
[encoders.models."text-embedding-3-large"]
    http      = "${http}"
    base_url  = "${site.llm_url}"
    api_key   = "${site.llm_token}"
```

Модель, объявленная в конфиге, но отсутствующая в `embedding_models`, и
наоборот — ошибка старта с именем модели: реестр не угадывает.

### 2.6 Опорные узлы

Что делает `seeds` и как считается счёт, описано в его
docstring; три потребителя индексов, и ни один не получает лишнего:

```python
class PgSearchStore(SearchStore[PgStatement]):
    """Реализация SearchStore над psycopg: исполняет PgStatement индексов корпуса и сливает списки RRF.

    Пул — общий пул приложения; энкодеры — PgVectorEncoderRegistry;
    candidates — [search] candidates корпуса: лимит кандидатов с индекса.
    """

    def __init__(self, pool: AsyncConnectionPool, encoders: VectorEncoderRegistry, candidates: int) -> None:
        self._pool = pool
        self._encoders = encoders
        self._candidates = candidates

    async def seeds(self, corpus: Corpus[PgStatement], query: str, top_k: int) -> Sequence[Seed]:
        """Опорные узлы: первый шаг поиска, узлы похожие на запрос ещё без графа.

        От опорных узлов вторым шагом GraphStore.expand пойдёт обход рёбер.
        Индексы корпуса берутся тремя группами по типу зонда: текстовым — одна
        строка запроса на всех, векторным — вектор запроса энкодером модели
        индекса, один раз на модель. Каждый индекс собирает свой запрос
        (statement), запросы выполняются параллельно, каждый отдаёт свой
        ранжированный список узлов с текстом попадания. Списки сливаются по
        обратному рангу с весами индексов (RRF): узел получает
        sum(weight / (60 + rank)) по спискам, где встретился, — найденный
        несколькими индексами поднимается. У узла остаётся счёт s_base, лучшее
        попадание (будущая цитата) и подписи индексов; список режется до top_k.

        Пример. Запрос «таймауты подключения к postgres»: section/fts находит
        страницу про libpq третьей, summary/vector — её же первой, title/trigram —
        «Настройки драйвера Postgres» первой. Страница про libpq набирает
        w/63 + w/61 и выходит выше; «Настройки драйвера» — w_title/61 — рядом;
        обе становятся опорными, expand подтянет их соседей по link, mention,
        similar.
        """
        text_indexes = corpus.text_indexes()
        dense_indexes = corpus.dense_indexes()
        sparse_indexes = corpus.sparse_indexes()

        text_probe = TextProbe(text=query, limit=self._candidates)
        dense_probes = await self._dense_probes(dense_indexes, query)
        sparse_probes = await self._sparse_probes(sparse_indexes, query)

        statements: list[PgStatement] = []
        labels: list[str] = []
        weights: list[float] = []
        for index in text_indexes:
            statements.append(index.statement(text_probe))
            labels.append(index.label())
            weights.append(index.weight())

        for index, probe in zip(dense_indexes, dense_probes, strict=True):
            statements.append(index.statement(probe))
            labels.append(index.label())
            weights.append(index.weight())

        for index, probe in zip(sparse_indexes, sparse_probes, strict=True):
            statements.append(index.statement(probe))
            labels.append(index.label())
            weights.append(index.weight())

        runs: list[Awaitable[Sequence[SearchHit]]] = []
        for statement in statements:
            runs.append(self._run(statement))

        lists = await asyncio.gather(*runs)
        merged = self._rrf(lists, labels, weights)
        return merged[:top_k]

    async def _dense_probes(
        self, indexes: Sequence[VectorSqlIndex[PgStatement]], query: str
    ) -> Sequence[DenseProbe]:
        vectors = await self._encode(indexes, self._encoders.dense, query)
        probes: list[DenseProbe] = []
        for index in indexes:
            probes.append(DenseProbe(vector=vectors[index.model()], limit=self._candidates))

        return probes

    async def _sparse_probes(
        self, indexes: Sequence[SparseSqlIndex[PgStatement]], query: str
    ) -> Sequence[SparseProbe]:
        vectors = await self._encode(indexes, self._encoders.sparse, query)
        probes: list[SparseProbe] = []
        for index in indexes:
            probes.append(SparseProbe(vector=vectors[index.model()], limit=self._candidates))

        return probes

    async def _encode(
        self,
        indexes: Sequence[VectorSqlIndex[PgStatement] | SparseSqlIndex[PgStatement]],
        encoder_of: Callable[[str], VectorEncoder[V]],
        query: str,
    ) -> Mapping[str, V]:
        """Вектор запроса считается один раз на модель, сколько бы индексов её ни делили."""
        vectors: dict[str, V] = {}
        for index in indexes:
            if index.model() in vectors:
                continue

            encoder = encoder_of(index.model())
            vectors[index.model()] = await encoder.encode(query)

        return vectors

    async def _run(self, statement: PgStatement) -> Sequence[SearchHit]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(statement.query, statement.params)
            rows = await cursor.fetchall()

        hits: list[SearchHit] = []
        for rank, row in enumerate(rows, start=1):
            hits.append(SearchHit(node_id=row.node_id, row_id=row.row_id, text=row.text, rank=rank))

        return hits

# ребро similar: вектор узла уже есть, encode не нужен
statement = corpus.similarity_index().statement(DenseProbe(vector=node_vector, limit=top_k))          # node_vector: DenseVector из vectors-таблицы

# ребро mention: заголовок другого узла, точное совпадение
statement = corpus.title_index().statement(TextProbe(text=title, limit=1))
```

`Probe.limit` для `kb_search` — `candidates` из конфига поиска: сколько
кандидатов берётся с одного индекса до слияния; `top_k` — сколько отдаёт
инструмент после RRF. HNSW отдаёт не больше `hnsw.ef_search` строк за
скан (по умолчанию 40), поэтому `SearchStore` перед векторными запросами
ставит `set local hnsw.ef_search = max(limit, 40)` на транзакцию поиска —
иначе `limit 50` молча вернёт 40. Веса списков — `[search.weights]` корпуса, ключ
«вид/способ»; корпус ставит их в `rrf_weight` индекса при создании, индекс
без веса в конфиге — ошибка старта; наружу вес отдаёт метод `weight()`. Вектор
считается только у индексов, которым он нужен, и один раз на индекс.

## 3. Graph tables

Ниже — реализация портов ядра в Postgres: то, что `boba-db-pggraph` создаёт
в схеме корпуса. Здесь нет ни текста, ни векторов: они в content tables (раздел
4), ядро добирается до них через объявленные корпусом индексы.

Все узлы и связи ходят по суррогатному `bigint` внутри схемы. Внешняя
идентичность узла — адрес частями (раздел 3.6), схема — одна из частей;
уникальность держится на нём. Строка адреса — представление, а не хранимое
поле: ядро собирает её из частей по одному правилу и разбирает обратно.

| таблица | что хранит |
|---|---|
| `nodes` | узел: только идентичность |
| `sync` | учёт обхода: что качать, что разбирать, что забыть |
| `entities` | словарь сущностей корпуса |
| `node_entities` | сущности узла с числом упоминаний и весом |
| `edges` | рёбра по виду связи, с весом и обоснованием |
| `ranks` | метрики узла по алгоритмам |
| `embedding_models` | модели эмбеддинга и их атрибуты |

Об узле хранится информация трёх уровней:

| уровень | где | страница FLIP-457 | таблица `dm.fact_orders` |
|---|---|---|---|
| 1 идентичность и учёт | `nodes`, `sync` | `kind = confluence_page`, адрес из `scheme`, `host`, `port`, `path` | `kind = pg_table`, адрес из `scheme`, `host`, `port`, `database`, `schema`, `table` |
| 2 связи и производные ядра | `edges`, `node_entities`, `ranks` | `link`, `mention`, `entity`, `similar`; `pagerank` | `contains`, `foreign_key`, `inferred_key`; `pagerank` |
| 3 всё содержимое | content tables | `pages`: заголовок, метки, оглавление, версия<br>`page_sections`: оригинал, markdown, `tsv`, векторы<br>`page_summaries`: саммари, `tsv`, векторы | `relations`: определение, комментарий, оценка строк<br>`relation_ddl`, `relation_profiles`, `relation_samples`: `tsv`, векторы<br>`columns`, `column_profiles` |

### 3.1 Узлы

```sql
create table nodes (
    id            bigserial   primary key,
    kind          text        not null,   -- полный дискриминатор: confluence_page | pg_table | ch_column — перечисление корпуса, раздел 2
    address       jsonb       not null,   -- адрес по ролям, включая scheme, раздел 3.6; идентичность узла
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create unique index on nodes (address);   -- идентичность: одна строка на адрес
create index on nodes (kind);
create index on nodes using gin (address jsonb_path_ops);
```

В `nodes` нет ни текста, ни имени, ни вектора, ни ссылок на другие узлы:
узел — это только «что это и где». Имя, тексты и векторы — в content tables; все
связи между узлами, включая вложенность (страница → вложение, таблица →
колонка), — только в `edges`. Жизненный цикл узла от других узлов не зависит: колонка исчезнувшей таблицы
удаляется не каскадом от таблицы, а потому, что её самой больше нет в
списке обхода (3.2).

| id | kind | address |
|---|---|---|
| 3 | `confluence_space` | `{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/space/FLINK"}` |
| 17 | `confluence_page` | `{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/content/307136992"}` |
| 18 | `confluence_attachment` | `{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/download/attachments/307136992/design.pdf"}` |
| 40 | `pg_schema` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm"}` |
| 41 | `pg_table` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders"}` |
| 42 | `pg_column` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders", "column": "amount"}` |
| 43 | `ch_index` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "index": "events_ts_minmax"}` |

Строка `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders` в
таблице не хранится: её даёт `render()` модели адреса при выдаче, а
входящую строку `parse()` той же модели разбирает в части, и поиск узла
идёт по `address`.

Что 18 лежит в 17, а 42 — в 41, говорят рёбра `(17, 18, has_attachment)` и
`(41, 42, contains)`.

### 3.2 Учёт обхода

Индексация — не разовая загрузка, а повторяющийся обход источника.
Каждый прогон должен качать и разбирать только то, что изменилось, и
убирать из индекса то, что в источнике исчезло.

```sql
create table sync (
    node_id           bigint      primary key references nodes on delete cascade,
    source_versions   jsonb       not null default '{}',
        -- аспект → версия по данным списка или каталога, без скачивания.
        -- аспекты объявляет корпус: у страницы один (content), у таблицы два (structure, data)
    body_hashes       jsonb       not null default '{}',
        -- аспект → sha256 скачанного по этому аспекту: тело страницы, DDL, профиль
    applied_methods   text[]      not null default '{}',
        -- способы обработки, которыми узел уже разобран: перечисление корпуса
    pipeline_stamp    text        not null default '',
        -- отпечаток настроек конвейера, которыми узел разобран
    crawl_scope       text        not null default '',
        -- область обхода, в которой узел найден
    last_seen_run     text        not null default '',
        -- прогон, в списке которого узел был в последний раз
    last_seen_at      timestamptz,
    last_indexed_at   timestamptz,
    skip_reason       text        not null default ''
        -- почему узел не индексируется правилами; пусто — индексируется
);
create index on sync (crawl_scope, last_seen_run);
```

Строка `sync` есть у каждого узла. У страницы, вложения, таблицы она
отвечает на все вопросы ниже; у колонки, индекса, ограничения — только на
вопрос об исчезновении: они приходят в списке вместе с таблицей, версии и
хэши у них пусты, а `last_seen_run` проставляется так же, как у всех.
Поэтому исчезновение любого узла — от пространства до колонки —
определяется одним правилом.

**Аспекты.** У узла может быть несколько независимых сторон, которые
меняются порознь и требуют разной работы. У таблицы их две: структура —
DDL, колонки, ограничения, индексы, комментарии — и данные — содержимое:
профили колонок, пример строк, число строк. Изменилась структура —
перечитать каталог, пересобрать тексты `ddl` и `columns`, явные рёбра;
изменились данные — перепрофилировать, обновить пример, пересчитать
`inferred_key`. Аспекты объявляет корпус и привязывает к ним стадии:

```python
class ConfluenceAspect(StrEnum):
    CONTENT = "content"        # тело страницы или вложения — единственный аспект

class WarehouseAspect(StrEnum):
    STRUCTURE = "structure"    # parse, ddl, columns, явные рёбра
    DATA = "data"              # column_profiles, relation_samples, inferred_key
```

Прогон сравнивает версии по каждому аспекту отдельно и запускает только
стадии изменившегося; у Confluence аспект один.

**`source_versions` и `body_hashes` — два разных вопроса.** Первый: «надо
ли качать?» — отвечается по списку или каталогу, до скачивания. Второй:
«надо ли разбирать?» — отвечается по скачанному, после. Они не выводятся
друг из друга.

| корпус, узел, аспект           | что берётся                                                                 | пример |
|--------------------------------|-----------------------------------------------------------------------------|--------|
| страница Confluence, content   | номер версии                                                                | `v10` |
| вложение Confluence, content   | версия, дата, размер, тип                                                   | `v3:2026-05-01T09:12:00Z:184320:application/pdf` |
| таблица PostgreSQL, structure  | `xmin` строк каталога: `pg_class`, `pg_attribute`, `pg_description`, `pg_constraint`, `pg_index` — сводятся в хэш | `3f9a1c…` |
| таблица PostgreSQL, data       | `pg_stat_user_tables`: `n_tup_ins`, `n_tup_upd`, `n_tup_del`, `last_autoanalyze`; сброс статистики меняет счётчики — лишняя перепрофилировка, не пропуск | `ins=12401233;upd=88102;del=0;analyzed=2026-09-11T03:00:00` |
| таблица ClickHouse, structure  | `system.tables.metadata_modification_time`                                  | `2026-09-11T22:40:03` |
| таблица ClickHouse, data       | `system.parts` по активным партам: `max(modification_time)`, `sum(rows)`    | `2026-09-12T01:10:44;rows=9812004411` |
| таблица MSSQL, structure       | `sys.objects.modify_date`                                                   | `2026-09-10T14:02:11` |
| таблица MSSQL, data            | `sys.dm_db_index_usage_stats.last_user_update`, `sys.partitions.rows`       | `2026-09-12T00:05:19;rows=4410233` |
| таблица Oracle, structure      | `ALL_OBJECTS.LAST_DDL_TIME`                                                 | `2026-09-09T08:15:00` |
| таблица Oracle, data           | `DBA_TAB_MODIFICATIONS` (`inserts`, `updates`, `deletes`), `LAST_ANALYZED`  | `ins=1200;upd=0;del=0;analyzed=2026-09-08` |
| таблица MySQL, structure       | `CREATE_TIME` при перестройке; иначе версии нет — решает хэш DDL            | `2026-08-30T12:00:00` |
| таблица MySQL, data            | `information_schema.TABLES.UPDATE_TIME` — у InnoDB ненадёжно; иначе `count(*)` выборкой | `2026-09-12T02:00:00;rows=88012` |

`body_hashes` — хэш скачанного по аспекту: `content` — HTML страницы
вместе с заголовком или байты вложения; `structure` — нормализованный
DDL с комментариями; `data` — профиль колонок и пример строк.

Страница, аспект `content`:

| прогон | `source_versions` | `body_hashes` | что случилось в источнике | что делает прогон |
|---|---|---|---|---|
| 1 | `{content: v10}` | `{content: cc4f…}` | первая индексация | качать, разбирать, писать |
| 2 | `{content: v10}` | `{content: cc4f…}` | ничего | список совпал — не качать |
| 3 | `{content: v11}` | `{content: cc4f…}` | пересохранили без правок | версия новая — качать; хэш совпал — не разбирать, обновить версию |
| 4 | `{content: v12}` | `{content: 9b1e…}` | текст изменили | качать, разбирать, писать |
| 5 | `{content: v13}` | `{content: 7a20…}` | переименовали, текст прежний | хэш считается с заголовком — разбирать: заголовок в content tables и в связях |

Таблица, аспекты `structure` и `data`:

| прогон | `source_versions` | что случилось | что делает прогон |
|---|---|---|---|
| 1 | `{structure: 3f9a…, data: ins=12.40M…}` | первая индексация | обе группы стадий |
| 2 | `{structure: 3f9a…, data: ins=12.41M…}` | ночная загрузка, структура прежняя | только `data`: профили, `sample`, `inferred_key` |
| 3 | `{structure: 7b21…, data: ins=12.41M…}` | добавили колонку | только `structure`: DDL, `columns`, явные рёбра |
| 4 | `{structure: 7b21…, data: ins=12.41M…}` | ничего | ничего |

Случай 3 у страницы — самый частый в Confluence (сохранение без
изменений, правка метки, перестановка в дереве) и самый дорогой без
хэшей: без `body_hashes` каждая такая версия шла бы в OCR вложений и в
модель саммари заново. Случай 2 у таблицы — самый частый в хранилище:
ежедневная загрузка меняет данные, но не структуру, и перечитывать
каталог с пересборкой рёбер незачем.

У PostgreSQL времени изменения объекта в каталоге нет, но есть системная
колонка `xmin` — номер транзакции, последней изменившей строку каталога.
`ALTER TABLE`, переименование, смена владельца обновляют строку
`pg_class`; добавление, удаление и смена типа колонки — `pg_attribute`;
комментарии — `pg_description`; ключи и индексы — `pg_constraint` и
`pg_index`. Хэш от их `xmin` и есть версия структуры:

```sql
select
    md5(concat_ws(':',
        c.xmin::text,
        (select max(a.xmin::text::bigint) from pg_attribute a where a.attrelid = c.oid),
        (select max(d.xmin::text::bigint) from pg_description d where d.objoid = c.oid),
        (select max(k.xmin::text::bigint) from pg_constraint k where k.conrelid = c.oid),
        (select max(i.xmin::text::bigint) from pg_index i where i.indrelid = c.oid)
    )) as structure_version
from
    pg_class c
where
    c.oid = 'dm.fact_orders'::regclass
```

`xmin` 32-битный и после ~4 млрд транзакций идёт по кругу; здесь он
сравнивается только на равенство, и совпадение старого значения после
оборота при неизменном хэше структуры безвредно — DDL сверяется всё
равно. OID для этого не годится: он стабилен на всё время жизни объекта и
при `ALTER` не меняется. Изменение данных `xmin` не ловит — это аспект
`data` со своим сигналом из таблицы выше.

**`applied_methods`** — какими способами узел уже обработан. Способы — не
уровни: OCR, распознавание смысла картинок, саммари, извлечение сущностей
независимы и включаются в любом сочетании; перечисление объявляет корпус:

```python
class ConfluenceParseMethod(StrEnum):
    TEXT = "text"          # текст страницы и текстовый слой вложений — всегда
    OCR = "ocr"            # текст с картинок и сканов
    CAPTION = "caption"    # описание содержимого картинок моделью зрения
    SUMMARY = "summary"    # саммари узла языковой моделью
    ENTITIES = "entities"  # сущности из текста
```

Прогон запрашивает набор способов флагами вызова; узел переразбирается,
если запрошенное множество не входит в применённое — даже при совпавших
версиях и хэшах. Применённое множество не сокращается: прогон без `ocr`
не стирает распознанное. Способ дописывается после успеха стадии; сбой
стадии его не дописывает, и следующий прогон повторит её.

| узел | `applied_methods` | запрошено | что делает прогон |
|---|---|---|---|
| 17 | `{text}` | `{text, summary}` | саммари не применялось — считать, дописать `summary` |
| 17 | `{text, summary}` | `{text}` | всё запрошенное есть — ничего |
| 18 | `{text, ocr}` | `{text, caption}` | нет `caption` — описать картинку моделью зрения; `ocr` остаётся |

**`pipeline_stamp`** — отпечаток настроек, которыми узел разобран. Конвейер
— цепочка стадий индексации (раздел 7); у каждой стадии есть параметры, от
которых зависит результат, и штамп — их сводка одной строкой:

```
reader=confluence:3;chunk=4000/0;embed=multilingual-e5-large;ner=gliner_multi-v2.1
```

Штамп в конфиге не совпал со штампом узла — узел переиндексируется
целиком, что бы ни говорили версии и хэши. Без штампа после смены модели
эмбеддинга в индексе лежали бы векторы двух несовместимых моделей.

**`crawl_scope` и `last_seen_run` — удаление исчезнувшего.** Confluence
не сообщает об удалении страницы: REST отдаёт список того, что есть.
Единственный способ узнать об удалении — увидеть, что в списке страницы
больше нет:

- каждый прогон получает идентификатор, например `run-2026-09-12-0912a`;
- прогон обходит область — `crawl_scope`: пространство `space:FLINK`,
  схему `schema:dm` — и каждому узлу из списка пишет `last_seen_run`;
- когда список области прочитан до конца без ошибок, узлы этой области с
  другим `last_seen_run` — которых в списке не было — удаляются вместе со
  всем, что на них ссылается (внешние ключи с каскадом);
- удалённая таблица уносит колонки не через себя: колонок тоже нет в
  списке, и они удаляются тем же правилом;
- если список оборвался (сеть, лимиты), очистка не запускается: неполный
  список не доказывает, что чего-то нет.

Область нужна, чтобы прогон по одному пространству не удалил страницы
других; идентификатор прогона, а не время, — чтобы очистка опиралась на
факт «этот обход завершился», а не на давность.

Прогон `run-0912a` по `space:FLINK`: в списке страницы 17, 21 и вложение 18;
страница 19 и её вложение 20 удалены неделю назад.

| node_id | kind | `crawl_scope` | `last_seen_run` | после списка | после очистки |
|---|---|---|---|---|---|
| 17 | `confluence_page` | `space:FLINK` | `run-0912a` | в списке | остаётся |
| 18 | `confluence_attachment` | `space:FLINK` | `run-0912a` | в списке | остаётся |
| 21 | `confluence_page` | `space:FLINK` | `run-0912a` | в списке | остаётся |
| 19 | `confluence_page` | `space:FLINK` | `run-0905c` | не в списке | удаляется |
| 20 | `confluence_attachment` | `space:FLINK` | `run-0905c` | не в списке | удаляется сама, не «вслед за 19» |
| 33 | `confluence_page` | `space:KAFKA` | `run-0905c` | область не листалась | остаётся |

**`skip_reason`** — узел есть в источнике, но правила его не индексируют:
вложение вне allowlist, страница-черновик. Он отмечается увиденным, иначе
очистка приняла бы его за исчезнувший.

| node_id | `source_versions` | `body_hashes` | `applied_methods` | `pipeline_stamp` | `crawl_scope` | `last_seen_run` | `skip_reason` |
|---|---|---|---|---|---|---|---|
| 17 | `{content: v10}` | `{content: cc4fe8ea…}` | `{text, summary, entities}` | `reader=confluence:3;chunk=4000/0;embed=e5` | `space:FLINK` | `run-0912a` | |
| 18 | `{content: v3:2026-05-01T09:12:00Z:184320:…/pdf}` | `{content: 9b1e02…}` | `{text, ocr}` | `reader=confluence:3;chunk=4000/0;embed=e5` | `space:FLINK` | `run-0912a` | |
| 19 | `{content: v1:2026-04-02T10:00:00Z:20480:image/png}` | `{}` | `{}` | | `space:FLINK` | `run-0912a` | `image/png not in allowlist` |
| 41 | `{structure: 3f9a1c…, data: ins=12401233;upd=88102;…}` | `{structure: 8c02d7…, data: e1b4…}` | `{text, profile, entities}` | `reader=postgres:1;chunk=4000/0;embed=e5` | `schema:dm` | `run-0912b` | |
| 50 | `{structure: 2026-09-11T22:40:03, data: …;rows=9812…}` | `{structure: e77b…, data: 40c1…}` | `{text, profile}` | `reader=clickhouse:1;chunk=4000/0;embed=e5` | `database:logs` | `run-0912b` | |

### 3.3 Сущности

Сущность — именованная вещь, которая встречается в тексте узлов и может
быть общей у нескольких: технология, продукт, версия, организация, термин
предметной области, метка. Ядро извлекает их одинаково для любого корпуса
из текстов, которые корпус назовёт (`Corpus.entity_texts`), и одинаково
считает по ним рёбра — поэтому это graph tables. Вид сущности — из
перечисления корпуса.

```sql
create table entities (
    id            bigserial   primary key,
    name          text        not null,   -- нормализованная форма: нижний регистр, один пробел, без диакритики
    kind          text        not null,   -- вид сущности из перечисления корпуса
    display       text        not null,   -- форма, в которой встретилась первой
    unique (name, kind)
);
create table node_entities (
    node_id       bigint      not null references nodes on delete cascade,
    entity_id     bigint      not null references entities on delete cascade,
    count         int         not null,   -- сколько раз сущность встретилась в тексте узла
    weight        real        not null,   -- tf-idf, нормирован в [0, 1] внутри узла
    primary key (node_id, entity_id)
);
create index on node_entities (entity_id, node_id) include (weight);   -- entity-рёбра: узлы с общей сущностью без heap
```

Откуда сущности берутся:

- Confluence — NER по тексту страницы (GLiNER: `software product`,
  `technology`, `version`, `organization`), ключевые термины (YAKE, вид
  `term`), метки страницы (вид `label`).
- Хранилище — NER по комментариям и описаниям, термины предметной области
  из комментариев, токены имён колонок и таблиц (вид `field`:
  `customer_id` → `customer`), теги/владельцы из метаданных (вид `label`).

`entities`:

| id | name | kind | display |
|---|---|---|---|
| 1 | `cassandra` | software product | Cassandra |
| 2 | `kraft` | software product | KRaft |
| 3 | `kubernetes` | technology | Kubernetes |
| 4 | `accepted` | label | accepted |
| 5 | `customer` | field | customer |
| 6 | `oms` | term | OMS |
| 7 | `заказ` | term | заказ |

`node_entities`:

| node_id | entity_id | count | weight | почему такой вес |
|---|---|---|---|---|
| 17 | 3 | 4 | 0.61 | страница FLIP-457 упоминает Kubernetes 4 раза |
| 17 | 4 | 1 | 0.20 | метка `accepted` — на 40% страниц пространства, вес низкий |
| 41 | 5 | 2 | 0.83 | колонки `customer_id`, `customer_region` дают `field = customer` |
| 41 | 6 | 3 | 0.95 | OMS в комментариях таблицы |
| 41 | 7 | 5 | 0.71 | |

`count` — число вхождений: совпадения NER по окнам плюс точные совпадения
имени сущности в тексте узла. `weight` — tf-idf, считается глобальной
стадией по всему корпусу:

```
tf(n, e)  = count(n, e) / Σ count(n, ·)
idf(e)    = ln((N + 1) / (df(e) + 1)) + 1        N — узлов с сущностями, df — узлов с e
weight    = tf · idf / max по узлу n            в [0, 1], 1 у самой характерной сущности узла
```

Сущность на половине корпуса (`cassandra` в пространстве Cassandra) получает
малый `idf` и не связывает всё со всем; сущность на 2–5 узлах связывает их
сильно. Вес ребра `entity` между узлами a и b — взвешенный Jaccard:
`Σ min(w_a, w_b) / Σ max(w_a, w_b)` по объединению их сущностей.

### 3.4 Рёбра

```sql
create table edges (
    source_id     bigint      not null references nodes on delete cascade,
    target_id     bigint      not null references nodes on delete cascade,
    kind          text        not null,   -- вид связи из перечисления корпуса
    weight        real        not null,   -- 0..1
    evidence      jsonb       not null default '{}',   -- Evidence.dump() модели вида ребра: факты и параметры расчёта
    computed_at   timestamptz not null default now(),
    primary key (source_id, target_id, kind)          -- прямой обход: index-only по (source_id, …)
);
create index on edges (target_id, source_id, kind) include (weight);   -- обратный обход adjacency: index-only
```

Один вид — одно ребро; вес пары агрегируется в запросе по классам
(раздел 8). Симметричные виды хранятся один раз, `source_id < target_id`.
Обход идёт по представлению `adjacency`: рёбра из `edges`, развёрнутые в
обе стороны для симметричных видов.

Ядро само считает два вида для любого корпуса — по общим сущностям и по
близости векторов `similarity_index`; имена этим рёбрам даёт корпус
(`entity_edge_kind`, `similar_edge_kind`). Всё остальное, включая
вложенность, объявляет и считает корпус. Ни один список ниже не закрыт:
новый признак связи — новый член перечисления корпуса и его вычислитель,
ядро не меняется. Множители веса при обходе — по `kind` в конфиге
корпуса (раздел 8).

```python
class ConfluenceEdgeKind(StrEnum):
    IN_SPACE = "in_space"              # пространство → страница
    CHILD_PAGE = "child_page"          # страница → дочерняя страница в дереве пространства
    HAS_ATTACHMENT = "has_attachment"  # страница → вложение
    LINK = "link"                      # ссылка на страницу в теле
    ATTACHMENT_REF = "attachment_ref"  # ссылка на вложение другой страницы
    MENTION = "mention"                # заголовок другой страницы встретился в тексте
    SERIES = "series"                  # общий код серии в заголовках: FLIP-457 и FLIP-458
    ENTITY = "entity"                  # общие сущности                        (считает ядро)
    SIMILAR = "similar"                # близость векторов                     (считает ядро)
    SAME_AUTHOR = "same_author"        # один автор последней правки

class WarehouseEdgeKind(StrEnum):
    CONTAINS = "contains"              # база → схема → таблица; таблица → колонка, индекс, ограничение, триггер
    FOREIGN_KEY = "foreign_key"        # объявленный внешний ключ; редок, но надёжен
    VIEW_SOURCE = "view_source"        # представление читает таблицу — из его определения
    ROUTINE_USES = "routine_uses"      # процедура читает или пишет таблицу
    INFERRED_KEY = "inferred_key"      # значения колонки A содержатся в значениях колонки B: кандидат в ключ
    SAME_COLUMN = "same_column"        # колонка с тем же именем и типом в двух таблицах
    NAME_PATTERN = "name_pattern"      # общий префикс или суффикс имён: fact_*, *_hist, stg_orders/dm_orders
    CO_QUERIED = "co_queried"          # таблицы вместе в одних запросах: pg_stat_statements, system.query_log
    MENTION = "mention"                # имя таблицы в комментарии другой
    ENTITY = "entity"                  # общие сущности                        (считает ядро)
    SIMILAR = "similar"                # близость векторов                     (считает ядро)
```

Для хранилища именно косвенные виды — `inferred_key`, `same_column`,
`name_pattern`, `co_queried` — описывают хаос, где внешних ключей нет:
`fact_orders.customer_id ⊆ dim_customer.customer_id` при 99,8% покрытия
значений — почти наверняка ключ, хоть он и не объявлен.

**Обоснование ребра.** `weight` — число для обхода, `evidence` — почему
оно такое: факты, по которым ребро посчитано, и параметры расчёта, при
которых оно построено. Пишется вместе с ребром стадией `edges`; при
переиндексации узла его рёбра удаляются в обе стороны и строятся заново
вместе с обоснованием. У каждого вида ребра своя модель — наследник
`Evidence` ядра (раздел 2); словаря общего вида нет.

```python
# boba-graph: обоснования рёбер, которые считает ядро
class FactEvidence(Evidence):
    """Ребро — факт метаданных источника (in_space, has_attachment, contains): обосновывать нечего, dump() даёт {}."""

class SharedEntity(BaseModel):
    name: str
    weight: float                       # min(w в узле A, w в узле B)

class EntityEvidence(Evidence):
    """Общие сущности двух узлов и взвешенный Jaccard, по которому построено ребро."""

    shared: Sequence[SharedEntity]      # верх по весу, не длиннее entity_top_shared
    jaccard: float                      # Σ min(w_a, w_b) / Σ max(w_a, w_b) по объединению сущностей
    min_jaccard: float                  # порог из [graph] на момент расчёта
    min_shared: int

class SimilarEvidence(Evidence):
    """Близость векторов: косинус, чей вектор и при каком пороге."""

    cosine: float
    index: str                          # label() similarity_index: "summary/vector"
    model: str                          # embedding_models.name
    min_cos: float                      # порог из [graph] на момент расчёта

# boba-corpus-confluence: обоснования явных рёбер
class LinkEvidence(Evidence):
    """Ссылка в теле страницы: якорь и фраза вокруг, из какого раздела."""

    anchor: str
    phrase: str
    section_id: int

class MentionEvidence(Evidence):
    """Заголовок другого узла найден в тексте точным совпадением по title_index."""

    title: str
    occurrences: int
    section_id: int
    min_words: int                      # mention_min_words на момент расчёта

class SeriesEvidence(Evidence):
    """Общий код серии в заголовках: FLIP-457 и FLIP-458."""

    prefix: str
    numbers: Sequence[int]

class SameAuthorEvidence(Evidence):
    author: str
```

Как считается каждое, по видам:

- `link`, `attachment_ref` — корпус, из разобранной страницы: каждая
  ссылка в теле даёт якорь и фразу вокруг него; адрес цели разрешается в
  узел, а если цели ещё нет, ссылка ждёт в `pending_links` и ребро
  строится при её появлении.
- `mention` — корпус: заголовки других узлов ищутся в тексте страницы
  через `title_index` точным совпадением; заголовок короче
  `mention_min_words` не считается.
- `series` — корпус: код серии из заголовка регулярным выражением; общий
  префикс у двух страниц.
- `in_space`, `child_page`, `has_attachment`, `contains` — факт из
  метаданных источника, `FactEvidence`; `same_author` — логин автора.
- `entity` — ядро, после стадии `entities`: SQL по `node_entities`
  соединяет узлы по общим сущностям, вес общей сущности — меньший из
  двух, Jaccard — сумма минимумов к сумме максимумов по объединению;
  ребро при `entity_min_shared` общих и Jaccard не ниже
  `entity_min_jaccard`.
- `similar` — ядро: kNN по векторной таблице `similarity_index` через
  HNSW, верх `similar_top_k`, косинус не ниже `similar_min_cos`.
- Хранилище, следующий план: `inferred_key` — включение значений колонки
  A в значения B на выборке, в обосновании покрытие и размер выборки;
  `same_column` — совпадение имени и типа; `name_pattern` — общий префикс
  или суффикс; `co_queried` — число совместных запросов за окно из
  `pg_stat_statements` или `system.query_log`; `view_source`,
  `routine_uses` — разбор определения, в обосновании имя объекта.

Три потребителя обоснования, и ранжирование среди них не значится: обход
(раздел 8) берёт только `weight` и множитель по `kind`.

1. **Показ.** `kb_node` и `kb_related` отдают рёбра узла с `evidence`
   как есть: по нему модель судит, насколько доверять связи — ссылка с
   якорем «see FLIP-458» весомее, чем `similar 0.81`.
2. **Проверка.** `kb_graph_check` сверяет параметры расчёта в обосновании
   с текущим конфигом: `evidence->>'min_cos'`, `'min_jaccard'`,
   `'min_words'` — и перечисляет рёбра, построенные при других порогах.
3. **Частичный пересчёт.** После смены порога стадия `edges` перестраивает
   только виды рёбер, чьи параметры в обосновании разошлись с конфигом,
   а не весь граф.

| source_id | target_id | kind | weight | evidence |
|---|---|---|---|---|
| 17 | 18 | `has_attachment` | 1.00 | `{}` |
| 41 | 42 | `contains` | 1.00 | `{}` |
| 17 | 21 | `link` | 1.00 | `{"anchor": "FLIP-458", "phrase": "see FLIP-458 for the API", "section_id": 905}` |
| 17 | 21 | `series` | 0.50 | `{"prefix": "FLIP", "numbers": [457, 458]}` |
| 17 | 33 | `entity` | 0.42 | `{"shared": [{"name": "kraft", "weight": 0.6}, {"name": "kubernetes", "weight": 0.3}], "jaccard": 0.42, "min_jaccard": 0.10, "min_shared": 2}` |
| 17 | 33 | `similar` | 0.87 | `{"cosine": 0.87, "index": "summary/vector", "model": "multilingual-e5-large", "min_cos": 0.80}` |
| 41 | 44 | `inferred_key` | 0.99 | `{"column": "customer_id", "target_column": "dim_customer.customer_id", "coverage": 0.998, "sample": 100000}` |
| 41 | 44 | `same_column` | 0.70 | `{"column": "customer_id", "type": "bigint"}` |
| 41 | 52 | `view_source` | 1.00 | `{"view": "dm.v_orders_daily"}` |
| 41 | 45 | `co_queried` | 0.63 | `{"queries": 118, "window": "30d"}` |

### 3.5 Метрики

Метрики — глобальные величины по всему графу, каждая от своего алгоритма.
Таблица в длинном формате: одна строка на узел и метрику, набор метрик
открыт.

```sql
create table ranks (
    node_id       bigint      not null references nodes on delete cascade,
    metric        text        not null,   -- имя метрики: pagerank | betweenness | closeness | degree_in | degree_out | community | hits_hub | hits_authority
    value         double precision not null,   -- значение; у community — номер сообщества
    computed_at   timestamptz not null,
    run_id        text        not null,   -- прогон глобальной стадии, давший значение
    primary key (node_id, metric)
);
create index on ranks (metric, value desc);
```

Глобальная стадия считает набор метрик из конфига; добавление алгоритма —
новая функция NetworkX и новое имя метрики, схема не меняется. Ранжирование
использует те метрики, что названы в его конфиге (раздел 8).

| node_id | metric | value | computed_at | run_id | |
|---|---|---|---|---|---|
| 17 | `pagerank` | 0.00412 | 2026-09-12 | `graph-0912` | |
| 17 | `betweenness` | 0.0187 | 2026-09-12 | `graph-0912` | |
| 17 | `degree_in` | 14 | 2026-09-12 | `graph-0912` | |
| 17 | `community` | 7 | 2026-09-12 | `graph-0912` | |
| 41 | `pagerank` | 0.0301 | 2026-09-12 | `graph-0912` | таблица, на которую ссылаются многие |
| 41 | `community` | 2 | 2026-09-12 | `graph-0912` | |

### 3.6 Адрес узла

Адрес хранится частями в `nodes.address` — jsonb-объект «роль → значение».
Роли объявляет корпус, ядро их не толкует: хранит, сравнивает, собирает
строку и разбирает обратно. Схема — первая часть адреса, как в RFC 3986, а не
отдельное поле: она выбирает грамматику строки, но это свойство самого
адреса. Идентичность узла — `address` целиком, на нём уникальный индекс:
jsonb хранится нормализованно, порядок ключей на равенство не влияет. Чтобы равенство было честным, типы значений
канонизирует модель адреса корпуса (`port: int`, остальные части —
строки, раздел 2); иначе `5432` и `"5432"` — разные адреса. Запросы по частям — «всё на хосте
`dwh.local`», «все колонки схемы `dm`» — идут GIN-индексом через
`address @> '{"host": ...}'`.

```json
{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders", "column": "amount"}
{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "index": "events_ts_minmax"}
{"scheme": "mssql", "host": "sql01.corp", "port": 1433, "instance": "ERP", "database": "erp", "schema": "dbo", "table": "Orders"}
{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/content/307136992"}
{"scheme": "smb", "host": "fs01.corp", "share": "reports", "path": "/2026/q1.xlsx", "sheet": "Summary"}
{"scheme": "s3", "host": "minio.corp", "port": 9000, "bucket": "raw", "key": "orders/2026-09-01.parquet"}
```

Запрос «всё в PostgreSQL» — тот же GIN: `address @> '{"scheme": "postgresql"}'`.

Строка адреса — представление частей для человека, модели и параметров
инструментов; в таблице она не хранится. Строится по стандартным
грамматикам адресов — там, где стандарт есть:

| scheme       | стандарт части подключения                  | пример подключения                       |
|--------------|---------------------------------------------|------------------------------------------|
| `postgresql` | libpq Connection URI                        | `postgresql://dwh.local:5432/dwh`        |
| `mssql`      | JDBC `sqlserver://host:port;databaseName=…` | `mssql://sql01.corp:1433/erp`            |
| `mysql`      | MySQL / JDBC URI                            | `mysql://db1:3306/shop`                  |
| `clickhouse` | JDBC / clickhouse-connect URI               | `clickhouse://ch1:9000/logs`             |
| `oracle`     | JDBC thin `//host:port/service`             | `oracle://ora1:1521/ORCL`                |
| `https`      | RFC 3986                                    | `https://cwiki.apache.org/confluence/...`|
| `file`       | RFC 8089                                    | `file:///share/reports/2026/q1.xlsx`     |
| `smb`        | URI `smb://host/share/path` (Samba, IANA)   | `smb://fs01.corp/reports`                |
| `hdfs`       | Hadoop URI `hdfs://namenode:port/path`      | `hdfs://nn1.corp:8020`                   |
| `s3`         | `s3://bucket/key` (AWS CLI, Hadoop)         | `s3://minio.corp:9000/raw`               |

Две оговорки к стандартам. У MSSQL именованный инстанс `host\ERP` в
JDBC пишется `instanceName=ERP`; в каноне это роль `instance` в query, а
не обратный слеш в хосте. У S3 стандарт ставит бакет в authority, endpoint
в адресе не участвует — для AWS это допустимо, для своих MinIO нет: два
хранилища с одинаковыми именами бакетов неразличимы. Поэтому в каноне
authority — endpoint, бакет — первый сегмент пути; для AWS endpoint —
`s3.<region>.amazonaws.com`.

Все они описывают подключение и заканчиваются на базе данных: стандарта на
адрес таблицы, колонки или индекса внутри базы нет ни у libpq, ни у JDBC.
Поэтому объект внутри базы задаётся query-параметрами с ролью в имени, в
фиксированном порядке ролей корпуса: строка остаётся валидным URL по
RFC 3986, её разбирает `urllib.parse` из stdlib, а роль в имени параметра
снимает позиционную неоднозначность — `table=daily` и `view=daily` не
спутать.

```
postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders
postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&column=amount
postgresql://dwh.local:5432/dwh?schema=dm&index=fact_orders_pk
mssql://sql01.corp:1433/erp?schema=dbo&table=Orders
mssql://sql01.corp:1433/erp?instance=ERP&schema=dbo&table=Orders&column=OrderID
mssql://sql01.corp:1433/erp?schema=dbo&procedure=usp_CloseOrder
clickhouse://ch1:9000/logs?table=events&index=events_ts_minmax
oracle://ora1:1521/ORCL?schema=SALES&table=ORDERS
https://cwiki.apache.org/confluence/rest/api/content/307136992
https://cwiki.apache.org/confluence/download/attachments/307136992/design.pdf
file:///share/reports/2026/q1.xlsx?sheet=Summary
smb://fs01.corp/reports/2026/q1.xlsx
smb://fs01.corp/reports/2026/q1.xlsx?sheet=Summary
hdfs://nn1.corp:8020/data/raw/orders/2026-09-01.parquet
hdfs://nn1.corp:8020/data/raw/orders?partition=dt%3D2026-09-01
hdfs://nn1.corp:8020/data/raw/orders/2026-09-01.parquet?column=amount
s3://minio.corp:9000/raw/orders/2026-09-01.parquet
s3://s3.eu-central-1.amazonaws.com/company-raw/orders/2026-09-01.parquet?column=amount
```

Файловые схемы (`file`, `smb`, `hdfs`, `s3`) адресуют файл путём, а объект
внутри файла — лист таблицы, партицию каталога, колонку parquet — той же
ролью в query, что и объект внутри базы.

Полный набор объектов PostgreSQL. Роли идут в порядке вложенности:
`schema`, затем объект, затем то, что внутри объекта.

| объект | адрес |
|---|---|
| база | `postgresql://dwh.local:5432/dwh` |
| схема | `postgresql://dwh.local:5432/dwh?schema=dm` |
| таблица | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders` |
| колонка таблицы | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&column=amount` |
| представление | `postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily` |
| колонка представления | `postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily&column=day` |
| материализованное представление | `postgresql://dwh.local:5432/dwh?schema=dm&matview=mv_orders_month` |
| колонка matview | `postgresql://dwh.local:5432/dwh?schema=dm&matview=mv_orders_month&column=total` |
| индекс, имя уникально в схеме | `postgresql://dwh.local:5432/dwh?schema=dm&index=fact_orders_customer_idx` |
| последовательность | `postgresql://dwh.local:5432/dwh?schema=dm&sequence=fact_orders_order_id_seq` |
| функция | `postgresql://dwh.local:5432/dwh?schema=dm&function=calc_total&args=bigint%2Cnumeric` |
| перегрузка той же функции — другой узел | `postgresql://dwh.local:5432/dwh?schema=dm&function=calc_total&args=bigint` |
| функция без аргументов: `args` пуст, но присутствует | `postgresql://dwh.local:5432/dwh?schema=dm&function=now_utc&args=` |
| процедура | `postgresql://dwh.local:5432/dwh?schema=dm&procedure=close_orders&args=date%2Ctext` |
| ограничение | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&constraint=fact_orders_customer_fkey` |
| триггер | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&trigger=trg_orders_audit` |

Функции и процедуры уникальны сигнатурой, а не именем: `args` — типы
аргументов в форме `pg_get_function_identity_arguments` (имена типов
каноничны, `integer`, а не `int4`), через запятую, запятая кодируется.
Роль `args` у функций и процедур обязательна даже пустой, чтобы адрес
без аргументов не спутать с адресом, где аргументы забыли. Индексы и
последовательности в PostgreSQL уникальны в схеме и от таблицы не зависят,
поэтому `table` в их адрес не входит — принадлежность таблице выражает
ребро `contains`; ограничения и триггеры уникальны внутри таблицы, поэтому идут
после `table`.

Набор ClickHouse. Схем нет: база сразу содержит объекты; представления и
материализованные представления — отдельные роли, хотя в
`system.tables` они лежат рядом с таблицами и различаются полем `engine`.

| объект | адрес |
|---|---|
| база | `clickhouse://ch1:9000/logs` |
| таблица | `clickhouse://ch1:9000/logs?table=events` |
| колонка | `clickhouse://ch1:9000/logs?table=events&column=user_id` |
| представление | `clickhouse://ch1:9000/logs?view=v_events_hourly` |
| колонка представления | `clickhouse://ch1:9000/logs?view=v_events_hourly&column=hour` |
| материализованное представление | `clickhouse://ch1:9000/logs?matview=mv_events_daily` |
| колонка matview | `clickhouse://ch1:9000/logs?matview=mv_events_daily&column=day` |
| skip-индекс, уникален внутри таблицы | `clickhouse://ch1:9000/logs?table=events&index=events_ts_minmax` |
| проекция | `clickhouse://ch1:9000/logs?table=events&projection=events_by_user` |
| словарь | `clickhouse://ch1:9000/logs?dictionary=dict_users` |
| UDF: перегрузок нет, `args` не нужен | `clickhouse://ch1:9000/logs?function=to_rub` |

У ClickHouse skip-индекс и проекция принадлежат таблице и уникальны только
внутри неё, поэтому идут после `table` — в отличие от PostgreSQL, где
индекс уникален в схеме. Это ровно та разница между движками, ради которой
роли объявляет корпус, а не ядро: порядок и состав ролей — часть
`Introspector` движка (раздел 4.2).

Правила канона, обязательные для `render()` и `parse()` каждой модели
адреса (2.1): учётных данных в строке адреса нет никогда; параметры
подключения (`sslmode`, `application_name`) — не часть идентичности; хост
в нижнем регистре; порт в частях обязателен, в строке web-адреса порт по
умолчанию схемы опускается (так делает `httpx.URL`); порядок
query-параметров — порядок объявления ролей в модели; значения кодируются по RFC 3986. Обе стороны
одной грамматики — один класс в пакете источника (`PgAddress`,
`ChAddress`, `ConfluenceAddress`); других мест сборки и разбора нет, ядро
строки не знает.

`url` для человека и модели живёт в content tables: Confluence отдаёт
`…/pages/viewpage.action?pageId=…` через `httpx.URL`; `httpx` в ядре нет.
Вложенность из адреса не выводится: ребро `contains` или `has_attachment`
ставит корпус по данным источника. Индекс PostgreSQL адресуется в схеме, а
ребром привязан к таблице — адрес и связь независимы.

### 3.7 Модели эмбеддинга и два бэкенда графа

Векторы лежат в content tables, но модель, которой они посчитаны, описана один
раз в graph tables — на неё ссылаются все векторные таблицы content tables:

```sql
create table embedding_models (
    id             smallserial primary key,
    name           text        not null unique,   -- multilingual-e5-large
    slug           text        not null unique,   -- e5: суффикс имени таблицы векторов page_section_vectors__e5
    provider       text        not null,   -- local | openai
    modality       text        not null,   -- text | image | sparse — какой VectorEncoder строит реестр для этой модели
    revision       text        not null default '',   -- версия весов: то же имя с другими весами — другая строка
    dim            int         not null,   -- размерность; у HNSW-индекса фиксирована
    index_distance text        not null,   -- cosine | dot | l2 — метрика, под которую построен индекс, и оператор по умолчанию
    normalize      bool        not null,
    max_tokens     int         not null,   -- где резать вход
    query_prefix   text        not null default '',   -- e5: 'query: '
    passage_prefix text        not null default '',   -- e5: 'passage: '
    created_at     timestamptz not null default now()
);
```

Векторы одной поверхности и одной модели лежат в своей таблице —
`page_section_vectors__e5`, `page_section_vectors__bge` — с колонкой
`vector(dim)` фиксированной размерности и обычным HNSW. Общая таблица с
`model_id` и частичными индексами не работает: HNSW требует typmod, у
моделей он разный, а частичный индекс планировщик берёт лишь при
буквальном совпадении предиката, чего ни join по имени, ни параметр в
generic-плане psycopg не дают. Таблица на модель делает запрос чистым
index scan без фильтров. По `index_distance` выбирается класс операторов
индекса (`vector_cosine_ops`, `vector_ip_ops`, `vector_l2_ops`) и
оператор запроса; другой оператор в запросе индекс не использует. `query_prefix`/`passage_prefix` — e5 требует разные префиксы для
запроса и документа, без них качество падает молча. `revision` отделяет
те же имена с другими весами: пересчёт — новая строка и новая таблица
векторов, старая живёт до его конца. Таблицы векторов создаёт установка по
моделям из конфига корпуса, размерность — из строки модели.

| id | name | slug | provider | modality | revision | dim | index_distance | normalize | max_tokens | query_prefix | passage_prefix |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `multilingual-e5-large` | `e5` | local | text | 2024-02 | 1024 | cosine | true | 512 | `query: ` | `passage: ` |
| 2 | `bge-m3` | `bge` | local | text | 2024-06 | 1024 | cosine | true | 8192 | | |
| 3 | `text-embedding-3-large` | `oai3large` | openai | text | | 3072 | cosine | true | 8191 | | |
| 4 | `siglip-so400m` | `siglip` | local | image | 2024-01 | 1152 | cosine | true | 64 | | |

**Бэкенды графа.** Всё, кроме рёбер, всегда реляционное. Бэкенд выбирает
только, где живут рёбра и как выполняется обход. Порт ядра:

| метод `GraphStore` | что делает |
|---|---|
| `replace_edges(node_id, kinds, edges)` | рёбра узла указанных видов заменить целиком |
| `neighbors(node_id, kinds) -> edges` | соседи с весом и обоснованием |
| `expand(seeds, depth, decay) -> scores` | обход от опорных: `node_id`, `s_graph`, `distance`, `path` |
| `export() -> edges` | весь граф для глобальной стадии (NetworkX) |
| `drop_node(node_id)` | убрать узел из графа (AGE: вершину и её рёбра) |

**Реляционный бэкенд** — таблица `edges` (3.4), представление `adjacency`,
рекурсивный CTE (раздел 8). Целостность — внешними ключами.

**Бэкенд AGE.** Граф AGE физически — отдельная схема Postgres с именем
графа, поэтому граф зовётся `<схема>_graph`: `confluence_graph` рядом с
`confluence`. Внутри AGE создаёт по таблице на метку:

| таблица AGE | что в ней |
|---|---|
| `confluence_graph._ag_label_vertex` | все вершины |
| `confluence_graph._ag_label_edge` | все рёбра |
| `confluence_graph.node` | вершины метки `node`: `id graphid`, `properties agtype` |
| `confluence_graph.link` | рёбра метки `link`: `id`, `start_id`, `end_id`, `properties agtype` |
| `confluence_graph.mention` | по таблице на каждый вид ребра корпуса |

Раскладка ядра на AGE:

- одна метка вершин `node`, свойства `{node_id, kind}`; `node_id` —
  мост к реляционной части, по нему индекс
  `create index on confluence_graph.node (ag_catalog.agtype_access_operator(properties, '"node_id"'))`;
- метка ребра = вид ребра корпуса; свойства `{weight, evidence, computed_at}`;
  симметричные виды хранятся одним ребром, обход идёт без направления;
- вершина создаётся при upsert узла и удаляется `DETACH DELETE` при
  удалении узла — внешних ключей между `nodes` и вершинами нет, за
  согласованность отвечает `GraphStore.age`, а инструмент `kb_graph_check`
  сверяет число узлов и вершин и чинит расхождение;
- обход — `cypher()` внутри того же SQL, что и pgvector:

```sql
select
    n.id,
    w.s_graph,
    w.depth
from
    cypher('confluence_graph', $$
        match (s:node)-[e*1..2]-(t:node)
        where s.node_id in $seeds
        return t.node_id, reduce(w = 1.0, r in e | w * r.weight), length(e)
    $$, $params) as w(node_id agtype, s_graph agtype, depth agtype)
    join nodes n on
        n.id = w.node_id::bigint
```

Что даёт AGE сверх реляционного: обход переменной длины и паттерны путей
(«таблицы, к которым от этой ведёт цепочка `view_source` любой длины»)
пишутся одной строкой Cypher вместо рекурсивного CTE на каждый вопрос.
Что стоит: нет внешних ключей, свой тип `agtype` на границе, отдельная
схема на корпус. Ранжирование получает от порта одни и те же `scores` и о
бэкенде не знает.

Выбор — `[graph] backend = "relational" | "age"`; установка проверяет
наличие расширения и падает с внятной ошибкой, если выбранного нет.

## 4. Content tables

Content tables — таблицы корпуса в той же схеме. Здесь лежит всё содержимое узла:
структурные атрибуты, тексты со своими полнотекстовыми индексами и
векторные таблицы к ним. Каждая текстовая таблица — со своей структурой:
у раздела страницы — оригинал и markdown, у саммари — модель и промпт, у
комментария колонки — тип и позиция; ничего не сплющивается в общую
строку. Строки content tables ссылаются на `nodes.id` с каскадом.

Поиск добирается до текстов через индексы (протокол `SqlIndex` в ядре,
реализации в `boba-db-pggraph`), которые корпус создаёт при старте и
отдаёт тремя группами по типу зонда: каждый знает таблицу и колонки текста
и чем он покрыт. Векторные таблицы content tables ссылаются на
`embedding_models` из graph tables.

### 4.1 Confluence

Виды узлов и адреса — `ConfluenceNodeKind` из `boba-confluence` (2.1);
корпус объявляет виды своих текстов, по таблице на вид:

```python
# boba-corpus-confluence
class ConfluenceTextKind(StrEnum):
    TITLE = "title"
    OUTLINE = "outline"
    SECTION = "section"
    ATTACHMENT_TEXT = "attachment_text"   # текстовый слой и OCR
    CAPTION = "caption"                   # описание картинки моделью зрения
    SUMMARY = "summary"
```

```sql
create table pages (
    node_id       bigint      primary key references nodes on delete cascade,
    page_id       text        not null unique,
    space         text        not null,
    title         text        not null,
    version       int         not null,
    modified_at   timestamptz,
    author        text        not null default '',
    url           text        not null,           -- webui, для человека и модели
    breadcrumb    text[]      not null default '{}',
    labels        text[]      not null default '{}',
    outline       jsonb       not null default '[]',   -- [{level, text, anchor}]
    outline_text  text        not null default '',    -- крошки, метки, оглавление одной строкой для поиска
    body_format   text        not null,
    title_tsv     tsvector    generated always as (to_tsvector('simple', unaccent(title))) stored,
    outline_tsv   tsvector    generated always as (to_tsvector('russian', unaccent(outline_text)) || to_tsvector('english', unaccent(outline_text))) stored
);
create index on pages (lower(title));                         -- PgExactIndex: lower(title) = lower(q)
create index on pages using gist (title gist_trgm_ops);        -- PgTrigramIndex: title <-> q, top-N из индекса
create index on pages using gin (title_tsv);                   -- PgFtsIndex
create index on pages using gin (outline_tsv);

create table page_sections (                   -- текст страницы по разделам и таблицам
    id            bigserial   primary key,
    node_id       bigint      not null references nodes on delete cascade,
    ordinal       int         not null,
    kind          text        not null,   -- section | table
    heading_path  text        not null default '',
    anchor        text        not null default '',
    raw_content   text        not null,   -- HTML раздела как в источнике
    format_content text       not null,   -- markdown: отдаётся модели и эмбеддится
    metadata      jsonb       not null default '{}',   -- у таблицы: подпись, колонки, раскладка
    content_sha256 text       not null,
    tsv           tsvector    generated always as (...format_content...) stored,
    unique (node_id, ordinal)
);
create index on page_sections using gin (tsv);
create table page_section_vectors__e5 (        -- на модель: имя = поверхность + "__" + embedding_models.slug, dim из embedding_models
    section_id    bigint      primary key references page_sections on delete cascade,
    embedding     vector(1024) not null
);
create index on page_section_vectors__e5 using hnsw (embedding vector_cosine_ops);

create table page_summaries (
    node_id            bigint  primary key references nodes on delete cascade,
    summary            text    not null,
    topics             text[]  not null default '{}',
    model              text    not null,
    system_prompt_hash text    not null,
    tsv                tsvector generated always as (...summary...) stored,
    updated_at         timestamptz not null default now()
);
create index on page_summaries using gin (tsv);
create table page_summary_vectors__e5 (
    node_id       bigint      primary key references page_summaries on delete cascade,
    embedding     vector(1024) not null
);
create index on page_summary_vectors__e5 using hnsw (embedding vector_cosine_ops);

create table attachments (
    node_id       bigint      primary key references nodes on delete cascade,
    attachment_id text        not null unique,
    title         text        not null,   -- имя файла
    media_type    text        not null,
    file_size     bigint      not null,
    version       int         not null,
    download_path text        not null
);
create table attachment_texts (                -- текст документа: текстовый слой или OCR
    id            bigserial   primary key,
    node_id       bigint      not null references nodes on delete cascade,
    method        text        not null,   -- text | ocr — чем получен; признак происхождения, не фильтр поиска
    ordinal       int         not null,   -- страница документа
    content       text        not null,
    tsv           tsvector    generated always as (...) stored,
    unique (node_id, ordinal)
);
create index on attachment_texts using gin (tsv);
create table attachment_text_vectors__e5 (
    text_id       bigint      primary key references attachment_texts on delete cascade,
    embedding     vector(1024) not null
);
create index on attachment_text_vectors__e5 using hnsw (embedding vector_cosine_ops);
create table attachment_captions (             -- описание картинки моделью зрения: другая природа, другой вес
    node_id       bigint      primary key references nodes on delete cascade,
    caption       text        not null,
    model         text        not null,
    system_prompt_hash text   not null,
    tsv           tsvector    generated always as (...) stored
);
create index on attachment_captions using gin (tsv);
create table attachment_caption_vectors__e5 (
    node_id       bigint      primary key references attachment_captions on delete cascade,
    embedding     vector(1024) not null
);
create index on attachment_caption_vectors__e5 using hnsw (embedding vector_cosine_ops);

create table attachment_images (           -- картинки вложений: поиск по смыслу изображения, не текста
    id            bigserial   primary key,
    node_id       bigint      not null references nodes on delete cascade,
    title         text        not null,   -- имя файла: единственный текст, который есть у картинки
    content       bytea       not null,   -- байты; их читает стадия индексации (ImageEncoder), не поиск
    width         int         not null,
    height        int         not null
);
create table attachment_image_vectors__siglip (   -- модель modality = image, dim 1152
    image_id      bigint      primary key references attachment_images on delete cascade,
    embedding     vector(1152) not null
);
create index on attachment_image_vectors__siglip using hnsw (embedding vector_cosine_ops);

create table pending_links (
    node_id        bigint     not null references nodes on delete cascade,
    target_title   text       not null,
    target_page_id text       not null default '',
    anchor_text    text       not null default '',
    primary key (node_id, target_title)
);
```

Индексы, которые корпус объявляет ядру:

```python
class ConfluenceCorpus(Corpus[PgStatement]):
    def __init__(self, cfg: ConfluenceCorpusConfig) -> None:
        schema = cfg.storage.pg_schema
        w = cfg.search.weights                      # [search.weights] "title/exact" = 3.0 …
        e5 = cfg.embedding.model                    # имя модели; slug для имён таблиц векторов — из её строки embedding_models
        self._text: Sequence[SqlIndex[TextProbe, PgStatement]] = (
            PgFtsIndex(name="title/fts", rrf_weight=w["title/fts"], schema=schema, table="pages",
                       node_column="node_id", row_column="node_id", text_column="title", tsv_column="title_tsv"),
            PgTrigramIndex(name="title/trigram", rrf_weight=w["title/trigram"], schema=schema, table="pages",
                           node_column="node_id", row_column="node_id", text_column="title"),
            PgExactIndex(name="title/exact", rrf_weight=w["title/exact"], schema=schema, table="pages",
                         node_column="node_id", row_column="node_id", text_column="title"),          # title_index
            PgFtsIndex(name="outline/fts", rrf_weight=w["outline/fts"], schema=schema, table="pages",
                       node_column="node_id", row_column="node_id", text_column="outline_text", tsv_column="outline_tsv"),
            PgFtsIndex(name="section/fts", rrf_weight=w["section/fts"], schema=schema, table="page_sections",
                       node_column="node_id", row_column="id", text_column="format_content", tsv_column="tsv"),
            PgFtsIndex(name="summary/fts", rrf_weight=w["summary/fts"], schema=schema, table="page_summaries",
                       node_column="node_id", row_column="node_id", text_column="summary", tsv_column="tsv"),
            PgFtsIndex(name="attachment_text/fts", rrf_weight=w["attachment_text/fts"], schema=schema, table="attachment_texts",
                       node_column="node_id", row_column="id", text_column="content", tsv_column="tsv"),
            PgFtsIndex(name="caption/fts", rrf_weight=w["caption/fts"], schema=schema, table="attachment_captions",
                       node_column="node_id", row_column="node_id", text_column="caption", tsv_column="tsv"),
        )
        self._dense: Sequence[VectorSqlIndex[PgStatement]] = (
            PgVectorIndex(name="section/vector", rrf_weight=w["section/vector"], schema=schema, table="page_sections",
                          node_column="node_id", row_column="id", text_column="format_content",
                          vector_table="page_section_vectors__e5", ref_column="section_id", model_name=e5),
            PgVectorIndex(name="summary/vector", rrf_weight=w["summary/vector"], schema=schema, table="page_summaries",
                          node_column="node_id", row_column="node_id", text_column="summary",
                          vector_table="page_summary_vectors__e5", ref_column="node_id", model_name=e5),   # similarity_index
            PgVectorIndex(name="attachment_text/vector", rrf_weight=w["attachment_text/vector"], schema=schema,
                          table="attachment_texts", node_column="node_id", row_column="id", text_column="content",
                          vector_table="attachment_text_vectors__e5", ref_column="text_id", model_name=e5),
            PgVectorIndex(name="caption/vector", rrf_weight=w["caption/vector"], schema=schema, table="attachment_captions",
                          node_column="node_id", row_column="node_id", text_column="caption",
                          vector_table="attachment_caption_vectors__e5", ref_column="node_id", model_name=e5),
        )
        self._sparse: Sequence[SparseSqlIndex[PgStatement]] = ()

    def text_indexes(self) -> Sequence[SqlIndex[TextProbe, PgStatement]]:
        return self._text

    def dense_indexes(self) -> Sequence[VectorSqlIndex[PgStatement]]:
        return self._dense

    def sparse_indexes(self) -> Sequence[SparseSqlIndex[PgStatement]]:
        return self._sparse

    def title_index(self) -> SqlIndex[TextProbe, PgStatement]:
        return self._text[2]

    def similarity_index(self) -> VectorSqlIndex[PgStatement]:
        return self._dense[1]
```

Индексы создаются корпусом при старте, а не константами модуля: схема,
модели и веса приходят из конфига корпуса, объявление их не знает. Три
группы — по типу зонда: так каждая типизирована точно, без `Any`.
Индекс по картинкам объявляется так же, с моделью `modality = image`:

```python
siglip = "siglip-so400m"
PgImageVectorIndex(
    name="image/image_vector", rrf_weight=w["image/image_vector"], schema=schema,
    table="attachment_images", node_column="node_id", row_column="id",
    title_column="title", content_column="content",
    vector_table="attachment_image_vectors__siglip", ref_column="image_id", model_name=siglip,
)
```

Индексация зеркальна поиску: стадия `embed` берёт `content` из
`attachment_images`, зовёт `ImageEncoder` той же модели
(`encode(image: bytes) -> DenseVector`) и пишет в `attachment_image_vectors`;
поиск считает зонд из текста `ClipTextEncoder`. Одна модель, два энкодера,
одна строка `embedding_models`.

Повторы `table`/`node_column`/`row_column` в объявлениях — намеренные:
каждый индекс читается сам по себе, без поиска общего определения. 

`SearchStore` способов поиска не знает: `statement` — у индекса (раздел 2),
вес списка и подпись попадания — тоже у индекса (`weight()`, `label()`), зонд
из текста он строит по типу зонда, он лишь запускает их параллельно и сливает ранги RRF с
весом на пару `(kind, method)` из конфига.

Два индекса над `page_sections` дают два запроса:

```sql
-- PgFtsIndex над SECTION
with q as (
    select
        websearch_to_tsquery('russian', unaccent(%(text)s))
        || websearch_to_tsquery('english', unaccent(%(text)s)) as tsq
)
select
    t.node_id,
    t.id as row_id,
    t.format_content as text,
    ts_rank_cd(t.tsv, q.tsq) as score
from
    confluence.page_sections t,
    q
where
    t.tsv @@ q.tsq
order by
    score desc
limit %(limit)s;

-- PgVectorIndex над SECTION
select
    t.node_id,
    t.id as row_id,
    t.format_content as text,
    v.embedding <=> %(vector)s::vector as score
from
    confluence.page_section_vectors__e5 v
    join confluence.page_sections t on
        t.id = v.section_id
order by
    v.embedding <=> %(vector)s::vector      -- то же выражение, что в select: HNSW отдаёт top-N по нему
limit %(limit)s;
```

`pending_links` — ссылки на страницы, которых в корпусе ещё нет; когда
цель индексируется, они становятся рёбрами `link`.

`pages`:

| node_id | page_id | title | labels | outline_text |
|---|---|---|---|---|
| 17 | 307136992 | FLIP-457: Improve Table/SQL Config… | `{accepted}` | Apache Flink Home › Flink Improvement Proposals. Labels: accepted. Sections: Status; Motivation; … |

`page_sections`:

| id | node_id | ordinal | kind | heading_path | format_content, начало |
|---|---|---|---|---|---|
| 201 | 17 | 0 | `section` | FLIP-457… › Motivation | Motivation\n\nAs Flink moves toward 2.0, we have revisited… |
| 204 | 17 | 3 | `table` | FLIP-457… › Public Interfaces… | markdown-таблица модулей и опций: `\| Module \| Configuration Options \| Class \|…` |

`page_summaries`:

| node_id | summary | topics | model | system_prompt_hash |
|---|---|---|---|---|
| 17 | FLIP-457 пересматривает опции table/SQL к выходу Flink 2.0… | `{flink, configuration, sql}` | `qwen3-4b-int4` | `5d41…` |

### 4.2 Хранилище данных

Индексатор хранилища получает подключение и обходит системный каталог
движка: `pg_catalog` в PostgreSQL, `sys.*` в MSSQL, `system.tables` /
`system.columns` в ClickHouse, `ALL_*` в Oracle, `information_schema` в
MySQL. У движков разные наборы объектов и разные слова для одного и того
же, поэтому content tables описывают объекты в терминах общей модели отношения, а
всё, чему нет места в общей модели, кладёт в `properties` движка. За
разбор каталога отвечает интроспектор движка — по классу на движок.

```python
# виды узлов — перечисления пакетов движков (2.1): PgNodeKind в boba-db-postgres,
# ChNodeKind в boba-db-clickhouse; MssqlNodeKind, OracleNodeKind, MysqlNodeKind придут
# со своими пакетами boba-db-*; у каждого свой набор объектов:
#   pg:     database, schema, table, view, matview, column, index, constraint, function, procedure, trigger, sequence
#   ch:     database, table, view, matview, column, index (skip), projection, dictionary, function
#   mssql:  database, schema, table, view, column, index, constraint, procedure, function, trigger, sequence
#   oracle: schema, table, view, matview, column, index, constraint, function, procedure, trigger, sequence, partition
#   mysql:  database, table, view, column, index, constraint, procedure, function, trigger

class WarehouseTextKind(StrEnum):
    TITLE = "title"
    COMMENT = "comment"
    DDL = "ddl"
    COLUMNS = "columns"
    PROFILE = "profile"
    SAMPLE = "sample"
    SUMMARY = "summary"

class Introspector(Protocol):
    """Каталог одного движка -> узлы и строки content tables; реализация на движок."""
    def objects(self, database: str) -> AsyncIterator[WarehouseObject]: ...
    def profile(self, table: WarehouseObject, sample: int) -> ColumnProfiles: ...
```

```sql
create table relations (               -- table | view | materialized_view | dictionary
    node_id       bigint      primary key references nodes on delete cascade,
    engine        text        not null,   -- postgresql | mssql | clickhouse | oracle | mysql
    relation_kind text        not null,   -- = nodes.kind: pg_table | pg_view | ch_table | …
    title         text        not null,   -- имя объекта без схемы
    definition    text        not null default '',   -- create table … / select … представления
    comment       text        not null default '',
    row_estimate  bigint,
    size_bytes    bigint,
    owner         text        not null default '',
    modified_at   timestamptz,
    properties    jsonb       not null default '{}',
        -- clickhouse: {"engine": "MergeTree", "order_by": ["ts","user_id"], "partition_by": "toYYYYMM(ts)"}
        -- postgresql: {"tablespace": "fast", "partitioned": true, "partition_key": "created_at"}
    title_tsv     tsvector    generated always as (to_tsvector('simple', title)) stored,
    comment_tsv   tsvector    generated always as (...comment...) stored
);
create index on relations (lower(title));                     -- PgExactIndex
create index on relations using gist (title gist_trgm_ops);    -- PgTrigramIndex
create index on relations using gin (title_tsv);
create index on relations using gin (comment_tsv);
-- что модель читает вместо базы: по таблице на вид текста, у каждой своя структура
create table relation_ddl (
    node_id       bigint      primary key references nodes on delete cascade,
    ddl           text        not null,   -- create table … / create view … as …, нормализованный вывод движка
    ddl_sha256    text        not null,
    tsv           tsvector    generated always as (...) stored
);
create table relation_column_lists (   -- колонки одной строкой: имя, тип, комментарий — для поиска по составу
    node_id       bigint      primary key references nodes on delete cascade,
    content       text        not null,
    tsv           tsvector    generated always as (...) stored
);
create table relation_profiles (       -- профиль таблицы текстом, собранный из column_profiles
    node_id       bigint      primary key references nodes on delete cascade,
    content       text        not null,
    sampled_at    timestamptz not null,
    tsv           tsvector    generated always as (...) stored
);
create table relation_samples (        -- несколько строк таблицы в раскладке столбцов
    node_id       bigint      primary key references nodes on delete cascade,
    content       text        not null,
    rows_shown    int         not null,
    sampled_at    timestamptz not null,
    tsv           tsvector    generated always as (...) stored
);
-- у каждой GIN по tsv и таблица векторов на модель: relation_ddl_vectors__e5,
-- relation_column_list_vectors__e5, relation_profile_vectors__e5, relation_sample_vectors__e5 —
-- (node_id primary key, embedding vector(1024)) с HNSW
create table relation_summaries (     -- как page_summaries
    node_id bigint primary key references nodes on delete cascade,
    summary text not null, topics text[] not null default '{}',
    model text not null, system_prompt_hash text not null,
    tsv tsvector generated always as (...) stored, updated_at timestamptz not null default now()
);
create table relation_summary_vectors__e5 (
    node_id bigint primary key references relation_summaries on delete cascade,
    embedding vector(1024) not null
);
create index on relation_summary_vectors__e5 using hnsw (embedding vector_cosine_ops);
create table columns (
    node_id       bigint      primary key references nodes on delete cascade,
    relation_id   bigint      not null references nodes on delete cascade,
    position      int         not null,
    title         text        not null,   -- имя колонки
    native_type   text        not null,   -- как в движке: Nullable(DateTime64(3)), NUMBER(18,2), timestamptz
    canonical_type text       not null,   -- integer | decimal | text | timestamp | date | bool | binary | json | array | other
    nullable      bool        not null,
    default_expr  text        not null default '',
    comment       text        not null default '',
    properties    jsonb       not null default '{}',
    comment_tsv   tsvector    generated always as (...comment...) stored
);
create index on columns (lower(title));
create index on columns using gin (comment_tsv);
create index on columns using gist (title gist_trgm_ops);      -- same_column, name_pattern: похожие имена колонок
create table column_comment_vectors__e5 (
    node_id bigint primary key references columns on delete cascade,
    embedding vector(1024) not null
);
create index on column_comment_vectors__e5 using hnsw (embedding vector_cosine_ops);
create table constraints (
    node_id         bigint    primary key references nodes on delete cascade,
    relation_id     bigint    not null references nodes on delete cascade,
    constraint_kind text      not null,   -- primary_key | foreign_key | unique | check
    columns         text[]    not null,
    ref_relation_id bigint    references nodes on delete set null,
    ref_columns     text[]    not null default '{}',
    definition      text      not null default ''
);
create table indexes (
    node_id       bigint      primary key references nodes on delete cascade,
    relation_id   bigint      not null references nodes on delete cascade,
    columns       text[]      not null,
    is_unique     bool        not null,
    index_kind    text        not null default '',   -- btree | gin | minmax | bloom_filter | …
    definition    text        not null default ''
);
create table routines (
    node_id       bigint      primary key references nodes on delete cascade,
    routine_kind  text        not null,   -- function | procedure
    title         text        not null,   -- имя с сигнатурой: calc_total(bigint, numeric)
    args          text        not null default '',
    returns       text        not null default '',
    language      text        not null default '',
    definition    text        not null default '',
    reads         text[]      not null default '{}',   -- отношения из тела, разбор по движку
    writes        text[]      not null default '{}'
);
create table column_profiles (         -- профиль данных: выборкой, не полным сканом
    node_id       bigint      primary key references nodes on delete cascade,   -- узел колонки
    sampled_at    timestamptz not null,
    sample_rows   bigint      not null,
    null_frac     real        not null,
    distinct_est  bigint      not null,
    min_value     text        not null default '',
    max_value     text        not null default '',
    top_values    jsonb       not null default '[]',   -- [{"v": "PAID", "share": 0.71}, …]
    value_pattern text        not null default '',    -- e-mail | uuid | phone | date-as-text | code:^[A-Z]{2}\d{6}$
    values_minhash bytea                               -- minhash-скетч значений: пересечение с другой колонкой без соединения таблиц
);
```

Индексы хранилища:

| вид текста | таблица, ключ строки, колонка текста | индексы |
|---|---|---|
| `title` | `relations`, `node_id`, `title` | `fts(title_tsv)`, `trigram`, `exact` — это `title_index()` |
| `title` | `columns`, `node_id`, `title` | `trigram`, `exact` — для `same_column`, `name_pattern` |
| `comment` | `relations`, `node_id`, `comment` | `fts(comment_tsv)` |
| `comment` | `columns`, `node_id`, `comment` | `fts(comment_tsv)`, `vector(column_comment_vectors.node_id)` |
| `ddl` | `relation_ddl`, `node_id`, `ddl` | `fts(tsv)`, `vector(relation_ddl_vectors__e5.node_id)` |
| `columns` | `relation_column_lists`, `node_id`, `content` | `fts(tsv)`, `vector(relation_column_list_vectors__e5.node_id)` |
| `profile` | `relation_profiles`, `node_id`, `content` | `fts(tsv)`, `vector(relation_profile_vectors__e5.node_id)` |
| `sample` | `relation_samples`, `node_id`, `content` | `fts(tsv)`, `vector(relation_sample_vectors__e5.node_id)` |
| `summary` | `relation_summaries`, `node_id`, `summary` | `fts(tsv)`, `vector(relation_summary_vectors__e5.node_id)` — это `similarity_index()` |

Четыре вида текста отношения — четыре таблицы, а не одна с колонкой
вида: у каждой своя структура (у профиля — время выборки, у примера —
число строк), свой вес в RRF и свои индексы без фильтров.

Профиль — то, что делает хаос описуемым: по нему модель понимает, что в
колонке `status` три значения, а `ext_ref` — на самом деле e-mail; по
`values_minhash` корпус оценивает пересечение значений двух колонок без
соединения таблиц и ставит ребро `inferred_key`.

`relations`:

| node_id | engine | relation_kind | title | definition, начало | comment | row_estimate | properties |
|---|---|---|---|---|---|---|---|
| 41 | postgresql | `pg_table` | `fact_orders` | `create table dm.fact_orders (…)` | Фактовые строки заказов из OMS | 12401233 | `{"partitioned": true, "partition_key": "created_at"}` |
| 50 | clickhouse | `ch_table` | `events` | `CREATE TABLE logs.events (…)` | | 9812004411 | `{"engine": "MergeTree", "order_by": ["ts","user_id"]}` |

Тексты той же таблицы 41, по строке на вид:

| таблица | content |
|---|---|
| `relation_ddl` | `create table dm.fact_orders (order_id bigint not null, customer_id bigint, amount numeric(18,2), …` |
| `relation_column_lists` | `order_id bigint pk; customer_id bigint; amount numeric(18,2) — сумма заказа в рублях с НДС; status text — NEW \| PAID \| CANCELLED; …` |
| `relation_profiles` | `order_id: 0% null, 12.4M distinct, 1..12401233` / `status: 0% null, 3 distinct: PAID 71%, NEW 22%, CANCELLED 7%` / … |
| `relation_samples` | markdown-таблица строк: `\| order_id \| customer_id \| amount \| status \| created_at \|`, `\| 1001 \| 77 \| 1290.00 \| PAID \| 2026-08-01 \|`… |

`columns`:

| node_id | relation_id | position | title | native_type | canonical_type | nullable | comment |
|---|---|---|---|---|---|---|---|
| 42 | 41 | 3 | `amount` | `numeric(18,2)` | `decimal` | false | сумма заказа в рублях с НДС |

`column_profiles`:

| node_id | sample_rows | null_frac | distinct_est | min_value | max_value | top_values | value_pattern |
|---|---|---|---|---|---|---|---|
| 42 | 100000 | 0.0 | 87211 | 0.01 | 1288400.00 | `[]` | |
| 57 | 100000 | 0.0 | 3 | CANCELLED | PAID | `[{"v":"PAID","share":0.71},…]` | `code:^[A-Z]+$` |

Что откуда: явные рёбра — `contains` из каталога, `foreign_key` из
`constraints`, `view_source` и `routine_uses` из разбора определений;
косвенные — `same_column` и `name_pattern` из имён, `inferred_key` из
профилей, `co_queried` из журнала запросов движка (`pg_stat_statements`,
`system.query_log`) — отдельным читателем, если журнал доступен.

## 5. Чтение исходника

Поиск отдаёт `kind`, адрес частями и строкой, `url` из content tables; за ними стоит
`Corpus.resolve`: по узлу вернуть оригинал в виде, который читает большая
модель. Confluence — страница целиком в
markdown, вложение — разобранным текстом. Хранилище — DDL, комментарии,
профиль и пример строк отношения, для колонки — её профиль и таблица.
Резолвер — часть корпуса, ядро знает только части адреса.

## 6. Код

Четыре новых пакета по слоям проекта, пятый — следующим планом:

| пакет | что внутри |
|---|---|
| `packages/core/boba-graph` | домен: `Node`, `Edge`, `Entity`, `Address`, `Evidence`, `Probe`, `SqlIndex[P, S]`, `VectorEncoder[V]`, `Corpus`; порты хранения и сервисов стадий; конвейер 2.0 |
| `packages/infra/db/boba-db-pggraph` | postgres: DDL graph tables, реализации портов для relational и age, слияние поиска по индексам, обход, глобальный экспорт |
| `packages/tools/boba-tool-graph` | инструменты над любым корпусом: `kb_search`, `kb_related`, `kb_entity`, `kb_node`, `kb_graph_rebuild`, `kb_graph_check`, установка схемы |
| `packages/tools/boba-corpus-confluence` | корпус Confluence: виды текстов и рёбер, content tables и их DDL, индексы поиска, транспорт и ридер 2.0, явные рёбра, резолвер, инструменты индексации `confluence_graph_index_*` |
| `packages/tools/boba-corpus-warehouse` | корпус хранилища (следующий план): виды текстов и рёбер, content tables, интроспекторы движков, профили, косвенные рёбра, резолвер |

Модели самих источников — в уже существующих пакетах источников (2.1);
единственная новая зависимость у них — база `Address` из `boba-graph`
(core ← infra, направление соблюдено):

| пакет источника | что в нём появляется |
|---|---|
| `packages/infra/format/boba-confluence` | `ConfluenceNodeKind`, адреса и узлы страниц, спейсов, вложений, `ConfluenceNode` |
| `packages/infra/db/boba-db-postgres` | `PgNodeKind`, адреса и узлы объектов каталога, `PgNode` |
| `packages/infra/db/boba-db-clickhouse` | `ChNodeKind`, адреса и узлы объектов, `ChNode` |

Корпус регистрируется как плагин `boba.tools` и попадает в реестр
корпусов по имени схемы; `boba-tool-graph` получает реализацию `Corpus`
из реестра и никогда не импортирует пакеты корпусов напрямую. Добавление
корпуса хранилища не меняет ни ядро, ни `boba-db-pggraph`, ни
`boba-tool-graph`.

Порты ядра в `boba-graph`:

| порт | что делает | кто реализует |
|---|---|---|
| `NodeStore` | upsert узла по адресу, чтение по адресу и id, удаление | хранилище |
| `SyncLedger` | реестр обхода над таблицей `sync` | хранилище |
| `EntityStore` | словарь, привязки, пересчёт idf | хранилище |
| `GraphStore` | рёбра и обход — две реализации (3.7) | хранилище |
| `RankStore` | метрики | хранилище |
| `ModelRegistry` | `embedding_models` | хранилище |
| `SearchStore` | `seeds(corpus, query, top_k)`: зонды по группам индексов, `statement` параллельно, RRF (раздел 2) | хранилище |
| `VectorEncoderRegistry` | `VectorEncoder` по имени модели и форме вектора; собран из `embedding_models` и `[encoders]` | `boba-llm` |
| `VectorEncoder` | текст в вектор одним методом; реализации по `modality` модели | `boba-llm`, зовут стадии корпуса и индексы |
| `Generator` | сервис генерации по схеме: саммари, описания картинок | сервис, зовут стадии корпуса |
| `EntityExtractor` | сервис извлечения сущностей из текста | сервис, зовёт ядро |
| `Corpus` | перечисления, content tables, индексы, явные рёбра, резолвер | корпус |

`boba-graph` зависит от `boba-indexing` только ради `Reader`, `Section`,
`Chunker`, `Embedder`, `RawDocument`. Схема создаётся установкой:
`boba-db-pggraph` держит DDL graph tables одним файлом на
`create … if not exists`, корпус — свой файл content tables; установка получает
имя схемы и бэкенд графа, накатывает graph tables, затем content tables, для AGE
создаёт граф `<схема>_graph`. Ядро `boba-graph` ни одного из этих файлов
не содержит и не импортирует.
Миграций нет: до первого релиза схема правится пересозданием, стендовые
`*_test` создаются тем же кодом.

Конфиг корпуса Confluence:

```toml
[storage]
    pg_schema = "confluence"

[graph]
    backend             = "relational"
    similar_top_k       = 10
    similar_min_cos     = 0.80
    entity_min_shared   = 2
    entity_min_jaccard  = 0.10
    entity_top_shared   = 10
    mention_min_words   = 2
    metrics             = ["pagerank", "betweenness", "degree_in", "community"]
    [graph.edge_factors]
        link           = 1.0
        attachment_ref = 0.9
        mention        = 0.8
        similar        = 0.7
        entity         = 0.7
        child_page     = 0.6
        has_attachment = 0.6
        series         = 0.4
        in_space       = 0.3
        same_author    = 0.3

[search]
    candidates = 50
    [search.weights]
        "title/exact"      = 3.0
        "title/fts"        = 2.0
        "title/trigram"    = 1.5
        "summary/vector"   = 1.5
        "summary/fts"      = 1.2
        "section/vector"   = 1.0
        "section/fts"      = 1.0
        "outline/fts"      = 1.0
        "attachment_text/fts"    = 0.8
        "attachment_text/vector" = 0.8
        "caption/fts"      = 0.6
        "caption/vector"   = 0.6

[entities]
    kind      = "gliner"
    model_dir = "${env.models}/gliner-multi"
    labels    = ["software product", "technology", "version", "organization"]
    threshold = 0.5
    stopwords = ["apache", "application", "version", "it"]
    terms_top = 20

[summary]
    input_chars = 12000
    [summary.generation]
        kind          = "local"
        model_dir     = "${env.models}/onnx-genai/qwen3-4b-int4"
        max_tokens    = 400
        reply_prefix  = "<think>\n\n</think>\n\n"
        system_prompt = "..."
```

## 7. Индексация

Стадии на узел. Ядро задаёт каркас — учёт, сущности, рёбра ядра,
глобальную стадию — и даёт сервисы; что писать в content tables, решает корпус:

| стадия | кто делает | что пишет |
|---|---|---|
| `fetch` | транспорт корпуса | Confluence: как сейчас |
| `parse` | ридер корпуса в строки content tables | страницы, разделы, таблицы, ссылки / объекты, колонки, определения |
| `embed` | корпус зовёт `VectorEncoder` для своих векторных индексов | `page_section_vectors`, `page_summary_vectors` / `relation_ddl_vectors`, … |
| `summary` | корпус зовёт `Generator`, если запрошено | `page_summaries` / `relation_summaries` |
| `entities` | ядро: `EntityExtractor` по `Corpus.entity_texts` | `entities`, `node_entities` |
| `edges` | `Corpus.explicit_edges` + `entity` + `similar` | `GraphStore` |

Стадия `edges` инкрементальна: рёбра индексируемого узла удаляются в обе
стороны и строятся заново. `entity` — SQL по `node_entities` с взвешенным
Jaccard, `similar` — kNN по векторной таблице `similarity_index` через
HNSW с порогом.

Глобальная стадия — инструмент `kb_graph_rebuild(corpus)`: пересчёт idf и
весов сущностей, экспорт рёбер в NetworkX, метрики из конфига → `ranks`.
На корпусе в 1,4 тыс. узлов — секунды; NetworkX держит десятки тысяч узлов
и миллионы рёбер в памяти.

### 7.1 Извлечение сущностей

GLiNER `multi-v2.1` в песочнице плагина вместе с torch CPU: по пробе на
50 страницах cwiki — 4 с на страницу в 13,6 тыс. символов на 8 потоках,
продукты и технологии извлекаются надёжно, мусор предсказуем и режется
стоп-листом и tf-idf. Метки — из конфига, объединение по имени без учёта
типа, тип — преобладающий. Термины — YAKE, вид `term`.

Русская проба — 17 страниц внутреннего Confluence (PHDD2, TMETA, PIXBI,
DQ; 77 тыс. символов): 1,5 с на страницу, плотность сущностей та же, что
на английском (9,7 на 10 тыс. символов против 8,3). Продукты и организации
извлекаются: `PIX BI`, `ADQM`, `Arenadata QuickMarts`, `PostgresPro`,
`Oracle`, `Airflow`, `Gazprom-Neft`, `EDM`; предметные термины тоже —
«продуктивный ландшафт», «качество данных», «lineage». Мусор того же
рода, что в английском, плюс склонённые формы («версию», «следующих
версиях»), которые режутся тем же стоп-листом и порогом на вид `version`.
YAKE на русском без лемматизации слаб («Рисунок», «Вкладка», «данных»):
термины берутся только из двух и более слов и с меньшим весом, замена на
извлечение ключевых фраз через эмбеддер e5 — отдельная проба позже.

## 8. Поиск и ранжирование

Инструмент `kb_search(corpus, query, …)`; `corpus` выбирает схему и
реализацию `Corpus`, SQL один и тот же:

1. **Опорные узлы** — `SearchStore.seeds(corpus, query, top_k)` (раздел
   2): узлы, похожие на запрос, ещё без графа. `SearchStore` строит зонды
   по группам индексов — текстовой одну строку, векторным вектор на модель, —
   индекс собирает свой запрос (`statement`), запросы идут параллельно: `fts`, `vector`, `trigram`, `exact`,
   `sparse`, `bm25` — что объявлено; каждый индекс возвращает
   ранжированный список `(node_id, текст, ранг)`. Ещё один список даёт
   сущностный поиск: запрос → `entities` → узлы через `node_entities`.
   Списки сливаются по обратному рангу (RRF): узел получает по слагаемому
   за каждый список, где встретился, — `weight / (k + место в списке)`,
   `k = 60`. Узел на первом месте в двух списках с весами 1.0 набирает
   `2/61`; узел на первом месте только по заголовку с весом 3.0 — `3/61`;
   на десятом месте по разделу с весом 1.0 — `1/70`. Ранги, а не счета,
   потому что `ts_rank` и косинус несопоставимы, а место в списке —
   сопоставимо. Вес — `index.weight()`, корпус берёт его из
   `[search.weights]` по паре «вид текста/способ»; в выдаче у узла
   перечисляются `index.label()` всех индексов, где он встретился: `title/exact 3.0, title/fts 2.0,
   summary/vector 1.5, section/vector 1.0, section/fts 1.0, sample/fts
   0.5`. RRF работает по рангам, а не по счётам, поэтому несопоставимые
   `ts_rank`, косинус и `similarity()` сливаются без нормировки. Итог —
   `seed_k` узлов с базовым счётом `s_base` и лучшим попаданием (цитатой).
   Реранк кросс-энкодером первых N — стадия поверх RRF, не индекс;
   добавляется отдельно, когда понадобится.
2. **Расширение.** `GraphStore.expand` от опорных на глубину до 2 с
   затуханием; вес ребра берётся с множителем по его `kind` из конфига
   корпуса — `[graph.edge_factors]`: у Confluence `link 1.0, mention 0.8,
   similar 0.7, entity 0.7, child_page 0.6, has_attachment 0.6, series
   0.4, same_author 0.3`; у хранилища `foreign_key 1.0, view_source 1.0,
   inferred_key 0.9, mention 0.8, similar 0.7, contains 0.6, co_queried
   0.5, same_column 0.4, name_pattern 0.3`. Ядро множители не толкует —
   вид ребра без множителя в конфиге считается ошибкой конфига.
   `s_graph(n) = Σ s_base(seed) · Π weight·factor`.
3. **Счёт.** `score = s_base + λ·s_graph + Σ μ_m·rank_m` по метрикам из
   конфига; λ и μ — параметры инструмента с дефолтами в конфиге.
4. **Выдача.** Узел: `kind`, адрес, `url`, заголовок из `title_index`,
   саммари, цитата, `why` — по какому ребру пришёл: «ссылается на
   FLIP-458», «customer_id покрывает dim_customer на 99,8%».

Обход в реляционном бэкенде:

```sql
with recursive walk as (
    select
        seed.id as node_id,
        0 as depth,
        seed.score as weight,
        array[seed.id] as path
    from
        seeds seed
    union all
    select
        a.target_id,
        w.depth + 1,
        w.weight * a.weight * c.factor,
        w.path || a.target_id
    from
        walk w
        join adjacency a on
            a.source_id = w.node_id
        join edge_factor c on          -- множители из конфига корпуса
            c.kind = a.kind
    where 1=1
        and w.depth < 2
        and not a.target_id = any(w.path)
)
select
    node_id,
    sum(weight) as s_graph,
    min(depth) as distance
from
    walk
group by
    node_id
```

Дополнительно: `kb_related(corpus, address)`, `kb_entity(corpus, term)`,
`kb_node(corpus, address)` — адрес строкой, корпус разбирает её `parse()`
своей модели адреса по схеме;
заголовок, саммари, сущности, рёбра и оригинал через
резолвер.

Связи между корпусами (страница описывает таблицу) — вне плана: у каждой
схемы своя нумерация узлов, мост — отдельная таблица без внешних ключей.
Проектируется с появлением второго корпуса.

## 9. Этапы

1. **Проба NER** — сделана на английском; русская проба на внутреннем
   Confluence — результат в 7.1.
2. **Ядро и хранение.** `boba-graph`: модели, протоколы `Address` и
   `Evidence`, порты, конвейер;
   `boba-db-pggraph`: DDL graph tables, реализации портов, реляционный
   `GraphStore`, `SearchStore` по индексам; установка `confluence` и
   `confluence_test`.
2a. **AGE.** Второй `GraphStore`, граф `confluence_graph`, те же тесты на
   обоих бэкендах; `kb_graph_check`.
3. **Корпус Confluence.** Модели узлов в `boba-confluence`;
   `boba-corpus-confluence`: виды текстов и рёбер, content
   tables и их DDL, индексы, транспорт и ридер 2.0; конвейер пишет `nodes`,
   `sync`, `pages`, `page_sections`, `attachments`, `attachment_texts`.
4. **Явные рёбра.** `in_space`, `child_page`, `has_attachment`, `link`,
   `attachment_ref`, `mention`, `series`, `pending_links`.
5. **Сущности** и рёбра `entity`, tf-idf.
6. **Семантика.** Векторные индексы, рёбра `similar`.
7. **Саммари.** `page_summaries`, генератор по схеме, способ `summary` в `applied_methods`.
8. **Глобальная стадия.** `kb_graph_rebuild`, `ranks`.
9. **Поиск.** `boba-tool-graph`: `kb_search`, `kb_related`, `kb_entity`,
   `kb_node`; резолвер — в корпусе.

Текущий индексатор всё это время не трогается; его судьба решается
отдельно, когда 2.0 принят. Корпус хранилища — следующий план поверх
этого: интроспекторы движков, профили, косвенные рёбра.
