# Перекачка данных между PostgreSQL, ClickHouse и Oracle

Этот документ описывает, как переливать данные между базами насосами boba:
какой поток байт отдаёт и принимает каждый насос, как в этом потоке выглядит
каждый тип, как стыковать два конца и где данные теряются молча. Всё, что
здесь написано, снято с живого стенда и проверено тестами: PostgreSQL от 9.0
до 19, Greenplum 6 и 7, ClickHouse 22.12, 23.12, 24.12, 25.12 и 26.7,
Oracle 12.2 Enterprise, 18 XE, 21 XE и 23 Free.

## Как устроена перекачка

Перекачка собирается из двух инструментов, соединённых трубой. Насос выгрузки
пишет кадры в свой выходной порт, насос загрузки читает их из входного
порта, хост соединяет порты, и оба насоса работают одновременно. Первый
кадр — `schema`: движок источника, формат данных `csv`, `tsv`, `binary` или
`arrow` и контракт колонок; дальше кадры `rows` с байтами. Тела никто не разбирает: какие байты выдал
источник, такие получит приёмник.

У PostgreSQL два инструмента на всё: `pg_stream_out(sql, wire, columns,
copy_options)` и `pg_stream_in(schema_name, table_name, стратегии, rules,
unknown_types, copy_options)`. Инструменты друг о друге не знают и ничем,
кроме потока кадров, не связаны: источник кладёт тела в раскладке, которую
назвал рычаг `wire`, и факты своего сервера в кадр `schema`; приёмник по
этому кадру проверяет, что раскладку он примет. Подробно в разделе «Пары
движков».

| База | Выгрузка | Загрузка | Формат потока |
|---|---|---|---|
| PostgreSQL | `pg_stream_out(sql, wire)` | `pg_stream_in(schema_name, table_name, ...)` | csv, tsv, binary или arrow — как назвал `wire` |
| ClickHouse | `ch_stream_out(sql, wire)` | `ch_stream_in(database, table_name, ...)` | tsv с типами ClickHouse как есть или arrow — как назвал `wire` |
| Oracle | `ora_stream_out(sql, columns)` | `ora_stream_in(schema_name, table_name, ...)` | только arrow с контрактом: пачки драйвера как есть |

Приёмники `pg_stream_in`, `ch_stream_in`, `ora_stream_in` принимают поток любого
источника с контрактом; подходит ли им раскладка, каждый проверяет сам по
кадру `schema`.

У PostgreSQL и ClickHouse поток пишет и читает сам сервер (COPY,
`FORMAT`), инструмент только называет ему формат из `wire`. У Oracle
серверного потока нет: всё, что идёт по сети, разбирает драйвер, и
единственный путь без разбора значений в Python — пачки Arrow драйвера как
есть. Поэтому у Oracle один формат, arrow.

`chunk_bytes` — размер порции между насосом и трубой, по умолчанию 256 КиБ:
крупнее — меньше системных вызовов на больших объёмах, мельче — раньше
первые данные у приёмника.

У каждого насоса есть ещё два аргумента, `before` и `after`: списки
стейтментов, которые выполняются в той же сессии до и после команды насоса.
Что они дают и как их писать под каждую базу, описано в разделе «Несколько
стейтментов в одном вызове».

Поток в примерах показан текстом, как он есть в UTF-8: табуляции и
переводы строк в блоках потока настоящие. В таблицах по полям невидимые
символы помечены: `⇥` — настоящая табуляция, `↵` — настоящий перевод строки;
`\t`, `\n` и `\\` в таблицах — это уже экранирование, которое вписал сервер
(два символа: обратный слэш и буква). Во всех примерах одна и та же строка
данных, выгруженная каждой базой.

## Поток PostgreSQL

### Текстовый формат COPY

Формат по умолчанию: `COPY (...) TO STDOUT` без опций.

```sql
copy (
  select
    1::bigint                                    as id,
    12.50::numeric(10,2)                         as amount,
    E'tab\tnew\nline \\ back "q", semi;'         as note,
    NULL::text                                   as empty,
    true                                         as flag,
    1.0::float8 / 3                              as ratio,
    'NaN'::float8                                as nan,
    date '2024-02-29'                            as d,
    timestamp '2024-02-29 13:14:15.123456'       as ts,
    timestamptz '2024-02-29 13:14:15+03'         as tstz,
    '\x00ff'::bytea                              as bin,
    array[1, 2, null]                            as arr,
    '{"a": [1, "x"]}'::jsonb                     as js,
    'a1b2c3d4-0000-0000-0000-000000000001'::uuid as u
) to stdout
```

Поток:

```text
1	12.50	tab\tnew\nline \\ back "q", semi;	\N	t	0.3333333333333333	NaN	2024-02-29	2024-02-29 13:14:15.123456	2024-02-29 10:14:15+00	\\x00ff	{1,2,NULL}	{"a": [1, "x"]}	a1b2c3d4-0000-0000-0000-000000000001
```

Поля разделены табуляцией, запись заканчивается переводом строки, шапки нет.

| # | Колонка | Тип | Текст поля | Что это |
|---|---|---|---|---|
| 1 | id | bigint | `1` | число как есть |
| 2 | amount | numeric(10,2) | `12.50` | со своим масштабом, хвостовой ноль сохранён |
| 3 | note | text | `tab\tnew\nline \\ back "q", semi;` | табуляция, перевод строки и `\` экранированы (`\t`, `\n`, `\\`); кавычки, запятая и `;` — как есть |
| 4 | empty | text | `\N` | NULL |
| 5 | flag | boolean | `t` | `t` / `f` |
| 6 | ratio | double precision | `0.3333333333333333` | кратчайший точный текст (с 12-й версии) |
| 7 | nan | double precision | `NaN` | также `Infinity`, `-Infinity` |
| 8 | d | date | `2024-02-29` | ISO |
| 9 | ts | timestamp | `2024-02-29 13:14:15.123456` | ISO с пробелом, до микросекунд |
| 10 | tstz | timestamptz | `2024-02-29 10:14:15+00` | в поясе сессии (`TimeZone`), со смещением |
| 11 | bin | bytea | `\\x00ff` | hex с префиксом `\x`, и `\` удвоен экранированием |
| 12 | arr | int[] | `{1,2,NULL}` | запись массивов PostgreSQL |
| 13 | js | jsonb | `{"a": [1, "x"]}` | нормализованный JSON |
| 14 | u | uuid | `a1b2c3d4-0000-0000-0000-000000000001` | с дефисами |

### CSV

`COPY (...) TO STDOUT (FORMAT CSV)`, тот же `select`.

```text
1,12.50,"tab	new
line \ back ""q"", semi;",,t,0.3333333333333333,NaN,2024-02-29,2024-02-29 13:14:15.123456,2024-02-29 10:14:15+00,\x00ff,"{1,2,NULL}","{""a"": [1, ""x""]}",a1b2c3d4-0000-0000-0000-000000000001
```

Отличается от текстового формата только в этих полях:

| # | Колонка | Текст поля | Что это |
|---|---|---|---|
| 3 | note | `"tab⇥new↵line \ back ""q"", semi;"` | в кавычках, потому что есть запятая, кавычка и перевод строки; кавычка внутри удвоена; табуляция и перевод строки — настоящие байты, `\` — один |
| 4 | empty | *(пусто)* | NULL — пустое поле без кавычек; пустая строка была бы `""` |
| 11 | bin | `\x00ff` | `\` больше не удваивается |
| 12 | arr | `"{1,2,NULL}"` | в кавычках из-за запятых |
| 13 | js | `"{""a"": [1, ""x""]}"` | в кавычках, кавычки внутри удвоены |

Значения без спецсимволов (числа, даты, `t`, uuid) идут без кавычек.

С опцией `COPY (...) TO STDOUT (FORMAT CSV, NULL '\N')` меняется одно поле:

| # | Колонка | Текст поля |
|---|---|---|
| 4 | empty | `\N` |

### Сессия COPY зафиксирована

Текст COPY зависит от настроек сессии, и без их фиксации один и тот же
запрос на двух серверах печатает разное. Оба насоса PostgreSQL
(`pg_stream_out` и `pg_stream_in`) поднимают соединение с зафиксированными GUC
через libpq-опции профиля (`CopyText` в `boba.db.postgres`):

| Настройка | Значение | Что было бы иначе |
|---|---|---|
| `DateStyle` | `ISO,YMD` | `Thu 29 Feb 10:14:15 2024 UTC`, `29-02-2024` при `Postgres, DMY` |
| `IntervalStyle` | `postgres` | `1 2:00:03.5` при `sql_standard` |
| `TimeZone` | `UTC` | `13:14:15+03` вместо `10:14:15+00`: тот же момент другим текстом |
| `bytea_output` | `hex` | `\\000\\377` при `escape` |
| `extra_float_digits` | `3` | до 12-й версии float печатается 15 знаками |
| `lc_monetary` | `C` | `12.50 ₽` при `ru_RU`, `$12.50` при `C` |
| `client_encoding` | `UTF8` | текст в кодировке сессии, не UTF-8 |
| `xmlbinary` | `base64` | bytea внутри xml как hex |
| `standard_conforming_strings` | `on` | `E'...'`-литералы запроса разбираются иначе |

Настройки из профиля соединения (таймауты, `search_path`) при этом
сохраняются. Тот же набор (`PostgresConfig.copy_text()`) стоит у всех
сессий с COPY в системе: у дампов источника в `pg-meta-scraper` и у
загрузки в ix.

Эти значения — умолчания аргумента `copy_options` у `pg_stream_out` и
`pg_stream_in`. Тот же объект несёт `chunk_bytes` (порция потока) и
`exact_floats` (hex-запись float при загрузке провода arrow). Вызов меняет
любое из них, когда поток нужен другим — приёмник ждёт `WIN1251`, деньги
нужны в локали, float короче или интервалы в `iso_8601`. Значения из
вызова перекрывают профиль соединения.

```json
{"copy_options": {"client_encoding": "WIN1251", "extra_float_digits": 0, "chunk_bytes": 65536}}
```

### Что принимает pg_stream_in

Тела csv, tsv и binary уходят в `COPY t FROM STDIN` той же раскладки, что у
выгрузки, стейтмент строит приёмник по контракту. Значение каждого поля разбирает сервер по типу колонки, поэтому
текст поля должен быть тем, что PostgreSQL понимает на вводе этого типа. Что
при этом происходит с чужими данными, собрано в разделах о стыковке.

## Поток ClickHouse

### TabSeparated

```sql
select
    toUInt64(1)                                          as id,
    toDecimal64(12.5, 2)                                 as amount,
    'tab\tnew\nline \\ back "q", semi;'                  as note,
    CAST(NULL, 'Nullable(String)')                       as empty,
    true                                                 as flag,
    1 / 3                                                as ratio,
    nan                                                  as nan,
    toDate('2024-02-29')                                 as d,
    toDateTime64('2024-02-29 13:14:15.123456', 6, 'UTC') as ts,
    [1, 2]                                               as arr,
    map('k', 1)                                          as m,
    tuple(1, 'x')                                        as t,
    toUUID('a1b2c3d4-0000-0000-0000-000000000001')       as u
format TabSeparated
```

Поток:

```text
1	12.5	tab\tnew\nline \\ back "q", semi;	\N	true	0.3333333333333333	nan	2024-02-29	2024-02-29 13:14:15.123456	[1,2]	{'k':1}	(1,'x')	a1b2c3d4-0000-0000-0000-000000000001
```

Разделители, экранирование и `\N` — как в текстовом COPY PostgreSQL, поэтому
эти два формата стыкуются байт в байт.

| # | Колонка | Тип | Текст поля | Что это |
|---|---|---|---|---|
| 1 | id | UInt64 | `1` | |
| 2 | amount | Decimal(18, 2) | `12.5` | **без хвостовых нулей**, в отличие от PostgreSQL |
| 3 | note | String | `tab\tnew\nline \\ back "q", semi;` | экранирование как у COPY |
| 4 | empty | Nullable(String) | `\N` | NULL |
| 5 | flag | Bool | `true` | `true` / `false` |
| 6 | ratio | Float64 | `0.3333333333333333` | кратчайший точный текст |
| 7 | nan | Float64 | `nan` | строчными: `nan`, `inf`, `-inf` |
| 8 | d | Date | `2024-02-29` | ISO |
| 9 | ts | DateTime64(6, 'UTC') | `2024-02-29 13:14:15.123456` | знаков столько, сколько у типа, пояса нет |
| 10 | arr | Array(UInt8) | `[1,2]` | запись ClickHouse, PostgreSQL её не читает |
| 11 | m | Map(String, UInt8) | `{'k':1}` | ключи в одинарных кавычках |
| 12 | t | Tuple(UInt8, String) | `(1,'x')` | |
| 13 | u | UUID | `a1b2c3d4-0000-0000-0000-000000000001` | с дефисами |

### TabSeparatedWithNamesAndTypes

Тот же `select` с `format TabSeparatedWithNamesAndTypes`. Перед данными идут
две строки шапки — имена и типы:

```text
id	amount	note	empty	flag	ratio	nan	d	ts	arr	m	t	u
UInt64	Decimal(18, 2)	String	Nullable(String)	Bool	Float64	Float64	Date	DateTime64(6, \'UTC\')	Array(UInt8)	Map(String, UInt8)	Tuple(UInt8, String)	UUID
1	12.5	tab\tnew\nline \\ back "q", semi;	\N	true	...
```

Кавычки внутри имени типа в шапке экранированы: `DateTime64(6, \'UTC\')`.
Строка данных — та же, что у TabSeparated.

Этот формат запрашивает `ch_stream_out` с `wire = tsv`: две строки шапки
уходят кадром `schema` как контракт с текстами типов ClickHouse, остальное —
кадрами `rows` как TabSeparated.

## Поток Oracle

У Oracle нет серверного текстового потока, и насосы Oracle работают только
на Arrow. `ora_stream_out` отдаёт пачки Arrow драйвера python-oracledb в кадры
потока как есть, `ora_stream_in` принимает поток arrow любого источника и
кладёт пачки в таблицу через `executemany`: bind'ы драйвер берёт прямо из
массивов Arrow, значений в Python никто не разбирает. Замер на стенде
(300 тысяч строк, 8 колонок): чтение 360–400 тысяч строк в секунду — на
треть быстрее построчного, дальше упирается в сервер; запись 150 тысяч в
секунду на Oracle 23 и 30 тысяч на 12.2.

### Что отдаёт ora_stream_out

`ora_stream_out(sql, columns)` разбирает стейтмент на сервере (`parse`, без
выполнения) и шлёт первым кадром контракт колонок: семейство и параметры
типа, `null_ok`, текст типа Oracle как в DDL; декларации `columns` ложатся
поверх (например, `not null` у ключа, который сервер считает nullable).
Затем сам запрос выполняется один раз, и пачки драйвера по `arraysize`
строк уходят в порт потоком Arrow IPC.

```sql
select
    1                                                            as id,
    cast(12.5 as number(10,2))                                   as amount,
    cast(123 as number)                                          as n_int,
    cast(7 as number(10))                                        as n_10,
    'tab' || chr(9) || 'x'                                       as note,
    cast(null as varchar2(5))                                    as empty,
    to_binary_double(1) / 3                                      as ratio,
    to_date('2024-02-29 13:14:15', 'yyyy-mm-dd hh24:mi:ss')      as d,
    cast(timestamp '2024-02-29 13:14:15.123456' as timestamp(6)) as ts,
    hextoraw('00FF10')                                           as bin,
    true                                                         as flag
from dual
```

Поток двоичный, поэтому показана его схема и значения первой записи, как
их читает pyarrow:

| Колонка | Тип Oracle | Тип в схеме Arrow | Значение |
|---|---|---|---|
| ID | NUMBER | `decimal128(38, 0)` | `Decimal('1')` |
| AMOUNT | NUMBER(10,2) | `decimal128(10, 2)` | `Decimal('12.50')` |
| N_INT | NUMBER без точности | `decimal128(38, 0)` | `Decimal('123')` |
| N_10 | NUMBER(10) | `int64` | `7` — целый NUMBER до 18 знаков едет целым |
| NOTE | VARCHAR2 | `large_string` | `'tab\tx'` — настоящая табуляция, ничего не экранируется |
| EMPTY | VARCHAR2 | `large_string` | `None` — NULL это null-бит Arrow |
| RATIO | BINARY_DOUBLE | `double` | `0.3333333333333333` |
| D | DATE | `timestamp[s]` | `2024-02-29 13:14:15` |
| TS | TIMESTAMP(6) | `timestamp[us]` | `2024-02-29 13:14:15.123456` |
| BIN | RAW | `large_binary` | `b'\x00\xff\x10'` — байты как есть, без hex |
| FLAG | BOOLEAN (23ai) | `bool` | `True` |

Что здесь важно:

- **имена колонок заглавные**, как их хранит Oracle; строчные — алиас в
  кавычках: `col as "col"`; приёмники сверяют колонки по именам;
- NUMBER без точности — `decimal128(38, 0)`, поэтому дробное значение в такой
  колонке — ошибка DPY-4042 при чтении: `cast(col as number(18, 6))` или
  `to_char`;
- целый NUMBER(p, 0) до 18 знаков — `int64`, NUMBER(19..38, 0) —
  `decimal128(p, 0)`, NUMBER(p, -s) — `decimal128(p + s, 0)`;
- TIMESTAMP(7..9) — `timestamp[ns]`, но драйвер обрезает до микросекунд;
- FLOAT(p) — `double`, 38 знаков NUMBER в нём не сохраняются;
- CLOB, NCLOB и BLOB едут строками и байтами (`large_string`,
  `large_binary`), в контракте они CLOB, NCLOB, BLOB.

### Что выгрузка не пропустит

Часть типов драйвер в Arrow не отдаёт или отдаёт с потерей; такие колонки
`ora_stream_out` отвергает по описанию стейтмента до выполнения запроса и
называет, чем их привести в самом `select`:

| Тип Oracle | Почему | Что писать в SELECT | В потоке |
|---|---|---|---|
| TIMESTAMP WITH TIME ZONE, WITH LOCAL TIME ZONE | смещение выбрасывается | `sys_extract_utc(col)` — настенное время в UTC, у приёмника объявить зонный тип через `column_types`; или `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')` | `timestamp[us]`, `large_string` |
| INTERVAL YEAR TO MONTH | Arrow-интервал не читает ни один приёмник | месяцы числом: `extract(year from col) * 12 + extract(month from col)`, или `to_char(col)` | `decimal128(38, 0)` |
| INTERVAL DAY TO SECOND | то же | секунды числом: `cast(extract(day from col) * 86400 + ... + extract(second from col) as number(18, 6))`, или `to_char(col)` | `decimal128(18, 6)` |
| XMLTYPE | нет в Arrow | `xmlserialize(document col as clob)` | `large_string` |
| JSON (21c+) | нет в Arrow | `json_serialize(col returning clob)` | `large_string` |
| VECTOR (23ai) | нет в Arrow | `from_vector(col)` | `large_string` |
| ROWID, UROWID | нет в Arrow | `rowidtochar(col)` | `large_string` |
| NUMBER без точности с дробью, NUMBER шире 38 знаков | DPY-4042 при чтении | `cast(col as number(p, s))` или `to_char(col, 'TM9')` | `decimal128(p, s)`, `large_string` |
| DATE до нашей эры | year out of range при чтении | `to_char(col, 'syyyy-mm-dd hh24:mi:ss')` | `large_string` |

Ошибка приходит сразу, запрос при этом не выполняется. `to_char(col, 'TM9')`
зависит от `NLS_NUMERIC_CHARACTERS` сессии; надёжнее задать разделитель
третьим аргументом: `'NLS_NUMERIC_CHARACTERS=''.,'''` (две одинарные кавычки
подряд — это одна кавычка внутри литерала).

### Что выгрузка пропустит, но потеряет

- **TIMESTAMP(9)** — в схеме `timestamp[ns]`, но драйвер уже обрезал до
  микросекунд: `.123456789` едет как `.123456000`. Нужны наносекунды —
  `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff9')`.
- **FLOAT(p)** — это NUMBER, но драйвер отдаёт его как double:
  `cast(1/3 as float(126))` едет как `0.3333333333333333` вместо 38 знаков.
  Точно — `to_char(col, 'TM9')`.
- **Однобайтовая кодировка базы.** Oracle 12.2 стенда живёт в `WE8DEC`:
  кириллица в VARCHAR2 и CLOB уже в базе хранится как `¿`, и поток повторит
  это. Юникод в такой базе живёт только в NVARCHAR2 и NCLOB.
- **`json()` до 21c** — типа JSON нет, а вызов `json('...')` молча даёт NULL.

### Что принимает ora_stream_in

`ora_stream_in(schema_name, table_name, schema_strategy, delete_strategy,
insert_strategy, rules, unknown_types, create_table, chunk_bytes, before,
after)` принимает поток arrow с контрактом от любого `*_sync_out`:
`pg_stream_out` с `wire = arrow`, `ch_stream_out` с `wire = arrow`,
`ora_stream_out`. Стратегии те же, что у `pg_stream_in`; имена колонок в
`rules` — строчными, как их сверяет приёмник (Oracle хранит имена
заглавными, приёмник сравнивает без учёта регистра). Шаблон
`create_table` — цельный стейтмент с `{schema_name}`, `{table_name}` и
`{columns}`; сюда пишутся `tablespace`, `partition by`, `compress`.

Типы колонок, которые приёмник создаёт по контракту:

| Семейство потока | Колонка Oracle |
|---|---|
| integer 8, 16, 32, 64 бит | NUMBER(3), NUMBER(5), NUMBER(10), NUMBER(19); беззнаковое 64 — NUMBER(20) |
| decimal(p, s) | NUMBER(p, s); без точности или шире 38 — NUMBER |
| float 32, 64 | BINARY_FLOAT, BINARY_DOUBLE |
| string с длиной до 4000 | VARCHAR2(n CHAR) |
| string без длины или длиннее | CLOB |
| binary с длиной до 2000 | RAW(n) |
| binary без длины или длиннее | BLOB |
| timestamp с долями секунды | TIMESTAMP(3), (6), (9); с поясом — WITH TIME ZONE |
| timestamp в секундах, date | DATE |
| boolean | BOOLEAN на 23ai; раньше — отказ, источник шлёт 0 и 1 |
| uuid текстом (из postgres) | VARCHAR2(36 CHAR) |
| time текстом (из postgres) | VARCHAR2(18 CHAR) |
| binary, присланное текстом (bytea из postgres) | строка как есть: CLOB или тип из `column_types` |
| json, inet, interval, money, xml, bit | CLOB |
| массивы, составные, прочее без пары | CLOB по `fallback_as_varchar`, иначе отказ с типом источника |
| источник Oracle | текст типа источника как есть: NUMBER(18,4), VARCHAR2(20 CHAR), TIMESTAMP(9), CLOB |

Сверка с существующей таблицей — по семействам: шире (NUMBER(30,6) под
decimal(18,4), VARCHAR2(200) под строку из 40, TIMESTAMP(6) под
миллисекунды) — предупреждение, уже — отказ до загрузки; целое из потока
ложится в NUMBER(p, 0), если разрядов хватает; момент с поясом в колонку
без пояса — отказ.

Как идёт загрузка:

- каждая пачка потока — одна команда `executemany`, значения драйвер берёт
  из массивов Arrow;
- приёмник значения не переписывает: пачка уходит в `executemany` как
  пришла, и так быстрее всего. Поле, тип Arrow которого драйвер как есть не
  положит, отвергается до DDL; отказ называет поле, его тип Arrow и тип
  Arrow, которым его прислать: uuid-расширение — utf8 или binary, time —
  utf8, duration и интервал — число секунд, fixed_size_binary — binary или
  utf8, dictionary — сам тип значений, bool на серверах до 23 — целое 0 и
  1. Как получить такой тип в своём запросе, знает источник: у ClickHouse
  это `toString(col)`, `toUInt8(col)`, у postgres — `col::text`, `col::int`;
- колонки LOB стоят в insert последними (ORA-24816): драйвер раздаёт
  bind'ы по порядку появления в стейтменте, поэтому вместе со списком
  колонок меняется и порядок массивов пачки — `RecordBatch.select`, те же
  буферы без копирования, около микросекунды на пачку;
- сессия переводится в UTC: момент без пояса из потока (например,
  `sys_extract_utc` источника Oracle) ложится в колонку WITH TIME ZONE как
  UTC, а не как время сессии сервера;
- DDL Oracle фиксирует сам, поэтому создание, бэкап (rename в
  `_bak_<время>`) и drop идут вне транзакции; удаление и вставка вместе с
  `before` и `after` — одна транзакция, ошибка шага откатывает строки;
- строки из postgres и ClickHouse создаются nullable, даже если поток
  объявил их `not null`: пустую строку Oracle хранит как NULL, и `not null`
  для неё невыполним (ORA-01400 на `''`); у источника Oracle пустых строк не
  бывает, и его `not null` доходит;
- зарезервированные слова в именах колонок (`by`, `date`, `number`) Oracle
  не принимает без кавычек: переименуйте поле через `rename_columns` или
  алиасом в `select` источника.

## PostgreSQL <-> ClickHouse

### Стейтменты

PostgreSQL -> ClickHouse, `pg_stream_out` с `wire = tsv` и `ch_stream_in`:

```sql
-- pg_stream_out (wire = tsv)
select id, name, created_at
from public.users
order by id
```

Стейтменты приёмника — `create table`, двойник `__ex`, `INSERT ... FORMAT
TabSeparated`, `exchange tables` — строит сам `ch_stream_in` по стратегиям.

ClickHouse -> PostgreSQL, `ch_stream_out` с `wire = tsv` и `pg_stream_in`:

```sql
-- ch_stream_out (wire = tsv)
select id, name, created_at
from dwh.users
order by id
```

Стейтменты приёмника — `create table`, `truncate`, `COPY ... FROM STDIN` —
строит сам `pg_stream_in` по стратегиям; тела `TabSeparated` ложатся в COPY
text как есть.

### Формат

Основная пара — текстовый COPY и `TabSeparated`: как видно по образцам выше,
разделители, экранирование и `\N` у них одинаковые, и поток одного читается
другим без единого преобразования. Поэтому между PostgreSQL и ClickHouse
`wire = tsv` в обе стороны; порядок полей — порядок колонок в запросе
источника, приёмник сопоставляет их с таблицей по контракту.

### Типы PostgreSQL в ClickHouse

Правило: если у ClickHouse есть родной тип с тем же текстовым
представлением, берём его, иначе храним текст PostgreSQL в `String`. Такая
строка вернётся в PostgreSQL тем же значением.

| Тип PostgreSQL | В потоке | Тип ClickHouse | Пояснение |
|---|---|---|---|
| smallint, integer, bigint | `42` | Int16, Int32, Int64 | диапазоны совпадают |
| numeric(p, s), p ≤ 38 | `12.50` | Decimal(p, s) | масштаб сохраняется |
| numeric без ограничения | `0.142857...` | String | родного типа нет |
| real, double precision | `0.3333333333333333`, `NaN` | Float32, Float64 | точность — ниже |
| boolean | `t` | Bool | ClickHouse читает `t`/`f` |
| text, varchar, char | `tab\tnew` | String | `char(n)` возвращается с пробелами |
| bytea | `\\x00ff` | String | едет как текст `\x00ff` |
| date | `2024-02-29` | Date32 | только 1900–2299, дальше молча обрезается |
| timestamp | `2024-02-29 13:14:15.123456` | DateTime64(6) | микросекунды сохраняются |
| timestamptz | `2024-02-29 10:14:15+00` | String | со смещением `DateTime64` не прочитает без best effort |
| time, interval | `13:14:15`, `1 day 02:00:00` | String | родных типов нет |
| uuid | `a1b2c3d4-...` | UUID | представления совпадают |
| массивы | `{1,2,NULL}` | String | ClickHouse ждёт `[1,2]` |
| point, polygon, box, path | `(1,2)` | String | ClickHouse такую запись не читает |
| inet, cidr, macaddr | `10.0.0.1/24` | String | у `inet` бывает маска |
| enum, составной тип | `ok`, `(1,"x,y")` | String | или `Enum8`, если значения известны |
| bit, varbit, money | `1010`, `$1.50` | String | родных типов нет |
| json, jsonb | `{"a": [1, "x"]}` | String | |
| диапазоны | `[1,11)` | String | |

Если данные остаются в ClickHouse, можно разобрать их богаче: массивы
выгрузить из PostgreSQL как JSON (`to_json(arr)`) и собрать `JSONExtract` в
`input()`, пояс — `parseDateTimeBestEffort`.

### Типы ClickHouse в PostgreSQL

Пара `ch_stream_out` (`wire = tsv`) -> `pg_stream_in`. Контракт — шапка
`TabSeparatedWithNamesAndTypes` того же запроса, тип postgres выбирается по
тексту типа ClickHouse, затем приёмник разбирает его у себя (`select
null::<тип>`) и сверяет с таблицей теми же правилами, что и pg -> pg. Всё
решается до DDL: у типа либо есть пара, либо «типа нет» — тогда
`unknown_types` (`fallback_as_varchar` даёт `varchar`) или явный
`rules.column_types`. Ошибка сервера при самой загрузке откатывает транзакцию
целиком.

| Тип ClickHouse | В потоке | Тип PostgreSQL | Пояснение |
|---|---|---|---|
| Int8, Int16 / Int32 / Int64 | `42` | smallint / integer / bigint | |
| UInt8 / UInt16 / UInt32 | `42` | smallint / integer / bigint | на разряд шире, чтобы вместить без знака |
| UInt64 | `18446744073709551615` | numeric(20) | |
| Int128, UInt128 / Int256, UInt256 | `1e21` цифрами | numeric(39) / numeric(78) | |
| Float32, Float64 | `0.3333333333333333`, `nan`, `inf` | real, double precision | postgres читает `nan` и `inf` |
| Decimal(p, s) любой ширины | `12.5000` | numeric(p, s) | |
| String, LowCardinality(String), Enum8, Enum16 | `tab\tnew` | text | Enum едет именем значения |
| FixedString(n) | `ab\0\0` | text | хвостовые NUL postgres не примет: в запросе `replaceAll(toString(col), '\\0', '')` |
| Date, Date32 | `2024-02-29` | date | |
| DateTime, DateTime('UTC') | `2024-02-29 13:14:15` | timestamp(0), timestamptz(0) | |
| DateTime64(n), DateTime64(n, 'UTC') | `...15.123456` | timestamp(n), timestamptz(n) | n > 6 postgres округляет до микросекунд |
| DateTime64(n, 'Europe/Moscow') | `...15.123` | типа нет | текст без смещения прочитался бы как UTC: `toDateTime64(col, n, 'UTC')` или `column_types` |
| Bool | `true` | boolean | |
| UUID | `a1b2c3d4-...` | uuid | |
| IPv4, IPv6 | `10.0.0.1` | inet | |
| JSON (24.x+) | `{"a":1}` | jsonb | `Object('json')` старых серверов печатается кортежем — типа нет |
| Array, Map, Tuple, Nested | `[1,2]`, `{'k':1}` | типа нет | `toJSONString(col)` и `column_types` jsonb, или `varchar` по `fallback_as_varchar` |
| Nullable(...) | `\N` | тот же тип, nullable | не-Nullable колонка создаётся `not null` |

Сверка с существующей таблицей та же, что у pg -> pg: шире (`numeric(30,6)`
под `Decimal(18,4)`, `integer` под `Int8`, `varchar(200)` под `String`) —
предупреждение, уже (масштаб, точность времени, другой тип) — отказ до
загрузки.

### Точность чисел с плавающей точкой

PostgreSQL до 12-й версии печатает `real` шестью значащими цифрами, а
`double precision` пятнадцатью: после круга значения расходятся в последних
знаках (до `1e-5` и `1e-14` относительно). С 12-й версии текст кратчайший
точный. Это свойство сервера, в COPY его не обойти.

ClickHouse при разборе текста в `Float64` теряет младший бит — и в ридере
формата, и в `toFloat64`. Настройка `precise_float_parsing` (23.x+, на 22.12
её нет) включает точный разбор, но только в функциях, поэтому точный путь —
принять число строкой:

```sql
insert into dwh.measures
select
    id,
    toFloat64(value) as value
from input('id UInt64, value String')
settings precise_float_parsing = 1
format TabSeparated
```

`NaN` и бесконечности проходят в обе стороны: `NaN`/`Infinity` PostgreSQL
ClickHouse читает, `nan`/`inf` ClickHouse PostgreSQL понимает.

## Oracle -> PostgreSQL

### Стейтменты

`ora_stream_out` и `pg_stream_in`: тела едут потоком arrow с контрактом,
стейтменты приёмник строит сам по стратегиям.

```sql
-- ora_stream_out
select
    id                                          as "id",
    amount                                      as "amount",
    sys_extract_utc(created_at)                 as "created_at",
    '\x' || rawtohex(payload)                   as "payload",
    note                                        as "note"
from sales.orders

-- pg_stream_in: schema_name = "dwh", table_name = "orders",
-- rules.column_types = {"created_at": "timestamptz(6)", "payload": "bytea"}
```

Колонки таблицы приёмник сверяет с контрактом по именам, поэтому алиасы в
`select` должны совпадать с колонками таблицы. Тела ложатся через CSV
сервера postgres, и двоичные значения Arrow он не берёт: RAW и BLOB едут
hex-текстом с префиксом `\x`, а колонка получает `bytea` через
`column_types` (без него — `text`). NULL в RAW при этом остаётся NULL:
`'\x' || null` в Oracle даёт NULL.

### Типы

| Тип Oracle | Что писать в select | Тип PostgreSQL |
|---|---|---|
| NUMBER(p, 0) до 18 знаков | как есть | bigint |
| NUMBER(19..38, 0), NUMBER(p, -s) | как есть | numeric(p, 0), numeric(p + s, 0) |
| NUMBER(p, s) | как есть | numeric(p, s) |
| NUMBER без точности | целые как есть; дробь — `cast(col as number(p, s))` | numeric(38, 0), numeric(p, s) |
| FLOAT(p) | как есть (double) или `to_char(col, 'TM9')` в numeric | double precision, text |
| BINARY_FLOAT, BINARY_DOUBLE | как есть | real, double precision |
| VARCHAR2(n), NVARCHAR2(n), CHAR(n) | как есть | character varying(n) |
| CLOB, NCLOB | как есть | text |
| DATE | как есть | timestamp(0) |
| TIMESTAMP(0..6) | как есть | timestamp(p) |
| TIMESTAMP(9) | как есть — микросекунды; `to_char(col, '... ff9')` в text | timestamp(6), text |
| TIMESTAMP WITH TIME ZONE | `sys_extract_utc(col)` + `column_types: timestamptz(6)` | timestamptz(6) |
| INTERVAL YEAR TO MONTH | месяцы числом | bigint |
| INTERVAL DAY TO SECOND | секунды `cast(... as number(18, 6))` | numeric(18, 6) |
| RAW, BLOB | `'\x' \|\| rawtohex(col)` + `column_types: bytea` | bytea |
| BOOLEAN (23ai) | как есть | boolean |
| JSON, XMLTYPE, VECTOR | `json_serialize`, `xmlserialize`, `from_vector` | text; jsonb, xml через `column_types` |

### Особенности

**DATE в колонку date.** DATE Oracle всегда со временем, и колонка `date`
PostgreSQL молча его отбрасывает. Приёмник создаёт `timestamp(0)`; `date`
через `column_types` — только если время не нужно.

**INTERVAL DAY TO SECOND со знаком.** `to_char` пишет знак один раз на всё
значение (`"-000000011 13:46:40.5"`), а PostgreSQL относит минус только к
суткам и получает другое значение. Поэтому интервал едет числом секунд, а
`column_types: interval` кладёт число как секунды.

**Лишние знаки numeric** PostgreSQL округляет: `-0.14286` в `numeric(18, 4)`
станет `-0.1429`.

**Greenplum 6 и очень малые double.** Число `1.942e-297` Greenplum 6
разбирает с ошибкой в младшем бите, тогда как PostgreSQL 9.4 и Greenplum 7
читают его точно. Если такие значения важны до бита, `exact_floats = true`
у `pg_stream_in` везёт float hex-записью (см. «Что принимает pg_stream_in с
провода arrow»).

## Oracle -> ClickHouse

### Стейтменты

`ora_stream_out` и `ch_stream_in`: контракт из описания стейтмента, стейтменты
приёмника строятся по стратегиям.

```sql
-- ora_stream_out
select
    id                          as "id",
    amount                      as "amount",
    sys_extract_utc(created_at) as "created_at",
    payload                     as "payload",
    note                        as "note"
from sales.orders

-- ch_stream_in: database = "dwh", table_name = "orders", order_by = "id",
-- rules.column_types = {"created_at": "DateTime64(6, 'UTC')"}
```

Преобразовывать почти нечего: `decimal128` ложится в `Decimal(p, s)`,
`int64` — в `Int64`, `double` — в `Float64` без потери бита, `timestamp[us]`
— в `DateTime64(6)`, `large_binary` — в `String`, `bool` — в `Bool`. NULL
едут null-битом Arrow, и колонки создаются `Nullable` по контракту.

### Типы

| Тип Oracle | Что писать в select | Тип ClickHouse |
|---|---|---|
| NUMBER(p, 0) до 18 знаков | как есть | Int64 |
| NUMBER(19..38, 0), NUMBER(p, -s) | как есть | Decimal(p, 0) |
| NUMBER(p, s) | как есть | Decimal(p, s) |
| NUMBER без точности | целые как есть; дробь — `cast(col as number(p, s))` | Decimal(38, 0), Decimal(p, s) |
| BINARY_FLOAT, BINARY_DOUBLE | как есть | Float32, Float64 |
| VARCHAR2, CHAR, CLOB, NVARCHAR2, NCLOB | как есть | String |
| DATE | как есть | DateTime64(0) |
| TIMESTAMP(0..6) | как есть | DateTime64(p) |
| TIMESTAMP(9) | как есть — микросекунды; `to_char(col, '... ff9')` в String | DateTime64(9), String |
| TIMESTAMP WITH TIME ZONE | `sys_extract_utc(col)` + `column_types: DateTime64(6, 'UTC')` | DateTime64(6, 'UTC') |
| INTERVAL YEAR TO MONTH | месяцы числом | Int64 |
| INTERVAL DAY TO SECOND | `cast(секунды as number(18, 6))` | Decimal(18, 6) |
| RAW, BLOB | как есть | String с байтами |
| JSON, XMLTYPE, VECTOR | `json_serialize`, `xmlserialize`, `from_vector` | String |
| BOOLEAN | как есть | Bool |

### Особенности

**Даты вне диапазона.** `DateTime64` держит 1900–2299: `0001-01-01` и
`9999-12-31` молча становятся `1900-01-01` и `2299-12-31`. Такие даты
храните строкой (`to_char` в Oracle, `String` через `column_types`).

**Лишние знаки Decimal** в существующей таблице с меньшим масштабом
отбрасываются, а не округляются; приёмник такую таблицу отвергает до
загрузки, а если нужен меньший масштаб — округлите в Oracle:
`cast(round(col, 4) as number(18, 4))`.

**Имена колонок.** Oracle отдаёт их заглавными; в ClickHouse они так и
создадутся. Строчные — алиас в кавычках.

## Поток Arrow

Arrow IPC — общий двоичный формат между концами, у которых текстовые форматы
не стыкуются или стыкуются с потерями: значения едут своими типами, без
перевода в текст и обратно. ClickHouse читает и пишет его сам: `ch_stream_out`
с `wire = arrow` дописывает к запросу `FORMAT ArrowStream` средствами
драйвера, `ch_stream_in` вставляет поток `INSERT ... FORMAT ArrowStream`. У Oracle
других форматов нет: `ora_stream_out` и `ora_stream_in` описаны в разделе
«Поток Oracle». Поток — это схема, затем пачки записей (у Oracle — по
`arraysize` строк), затем конец потока; байты между узлами идут как есть.

### Что отдаёт ch_stream_out с wire = arrow

```sql
select
    toInt64(1)                                           as id,
    toDecimal64(12.5, 2)                                 as amount,
    'tab\tx'                                             as note,
    CAST(NULL, 'Nullable(String)')                       as empty,
    1 / 3                                                as ratio,
    toDate('2024-02-29')                                 as d,
    toDateTime('2024-02-29 13:14:15', 'UTC')             as dtm,
    toDateTime64('2024-02-29 13:14:15.123456', 6, 'UTC') as ts,
    unhex('00FF10')                                      as bin,
    true                                                 as flag,
    toUUID('a1b2c3d4-0000-0000-0000-000000000001')       as u
```

`FORMAT ArrowStream` дописывает инструмент.

`Date` (до 26) и `DateTime` уходят в Arrow целыми числами, и приёмник по
контракту не отличит их от `UInt16` и `UInt32`: даты молча станут числами.
В запросе для arrow пишите `toDate32(d)` и `toDateTime64(dt, 0, 'UTC')`;
типы ClickHouse как есть везёт `ch_stream_out` с `wire = tsv`.

| Колонка | Тип ClickHouse | Тип в схеме Arrow | Что это |
|---|---|---|---|
| id | Int64 | `int64 not null` | |
| amount | Decimal(18, 2) | `decimal128(18, 2)` | |
| note | String | `string` (`binary` на 22.12 без `output_format_arrow_string_as_string`) | |
| empty | Nullable(String) | `string` | null-бит Arrow |
| ratio | Float64 | `double` | |
| d | Date | **`uint16`** до 25.x, `date32[day]` с 26 | до 26 — дни с эпохи числом, не датой |
| dtm | DateTime | **`uint32`** | секунды с эпохи числом, не временем |
| ts | DateTime64(6, 'UTC') | `timestamp[us, tz=UTC]` | |
| bin | String с байтами | `string` | **невалидный UTF-8** в строке: читатель падает |
| flag | Bool | `bool` (`uint8` на 22.12) | |
| u | UUID | `extension<arrow.uuid>` (на 22.12 — ошибка UNKNOWN_TYPE) | |

### ClickHouse -> Oracle

```sql
-- ch_stream_out, wire = arrow
select
    id,
    amount,
    toDateTime64(created_at, 0, 'UTC') as created_at,
    hex(payload)                       as payload,
    active
from dwh.orders
settings output_format_arrow_string_as_string = 1

-- ora_stream_in: schema_name = "SALES", table_name = "ORDERS"
```

Приёмник создаёт таблицу по контракту и вставляет пачки `executemany`;
что надо привести на стороне ClickHouse:

| Тип ClickHouse | Как есть | Что писать в select | Тип Oracle |
|---|---|---|---|
| Int8..Int64, UInt8..UInt32 | `int*` | как есть | NUMBER(3), NUMBER(5), NUMBER(10), NUMBER(19) |
| UInt64 | `uint64` | как есть | NUMBER(20) |
| Int128 и шире | нет в Arrow | `toString(col)` | VARCHAR2(45 CHAR) |
| Decimal(p, s) | `decimal128(p, s)` | как есть | NUMBER(p, s) |
| Float32, Float64 | `float`, `double` | как есть | BINARY_FLOAT, BINARY_DOUBLE |
| String | `string` с `output_format_arrow_string_as_string = 1` | как есть | CLOB (длины контракт не знает; `column_types: VARCHAR2(200 CHAR)`) |
| FixedString(n) | `fixed_size_binary` | `toString(col)` | CLOB |
| Date, Date32 | `uint16` до 26, `date32` | `toDate32(col)` | DATE |
| DateTime | `uint32` | `toDateTime64(col, 0, 'UTC')` | DATE |
| DateTime64(p, 'UTC') | `timestamp[p, tz=UTC]` | как есть | TIMESTAMP(p) WITH TIME ZONE |
| DateTime64(p) без пояса | `timestamp[p]` | как есть | TIMESTAMP(p) |
| Bool | `bool` (`uint8` на 22.12) | как есть на 23ai; раньше `toUInt8(col)` | BOOLEAN; NUMBER(3) |
| UUID | `arrow.uuid` (на 22.12 — ошибка UNKNOWN_TYPE) | `toString(col)` — uuid-расширение приёмник отвергает | CLOB, `column_types: VARCHAR2(36 CHAR)` |
| String с байтами | невалидный UTF-8 | `hex(col)` | CLOB текстом hex |
| Array, Map, Tuple | нет в Arrow | `toString(col)`, `toJSONString(col)` | CLOB |

Ловушки этого пути:

- **Date и DateTime как числа.** До 26 `Date` уходит в Arrow как `uint16`,
  `DateTime` — всегда как `uint32`; приёмник по контракту создаст NUMBER, а
  не дату. `toDate32` и `toDateTime64` дают настоящие моменты.
- **Юникод в однобайтовой базе.** Bind строки идёт в кодировке базы: в базе
  `WE8DEC` юникод не доедет даже до NVARCHAR2 (станет `¿`).
- **Момент с поясом в колонку без пояса** приёмник отвергает до загрузки:
  под `DateTime64(6, 'UTC')` нужна `TIMESTAMP(6) WITH TIME ZONE` или
  `toDateTime64(col, 6)` без пояса в запросе.

### PostgreSQL -> Oracle

```sql
-- pg_stream_out, wire = arrow
select id, amount, created_at, payload, tags::text as tags
from sales.orders

-- ora_stream_in: schema_name = "SALES", table_name = "ORDERS"
```

Поток `pg_stream_out` с `wire = arrow` собирается из COPY csv сервера, поэтому
часть типов едет текстом, и приёмник знает, что с ним делать:

| Тип PostgreSQL | В потоке | Тип Oracle |
|---|---|---|
| smallint, integer, bigint | `int16`, `int32`, `int64` | NUMBER(5), NUMBER(10), NUMBER(19) |
| numeric(p, s) | `decimal128(p, s)` | NUMBER(p, s) |
| numeric без точности | `large_string` | NUMBER (через `column_types`), иначе CLOB |
| real, double precision | `float`, `double` | BINARY_FLOAT, BINARY_DOUBLE |
| boolean | `bool` | BOOLEAN на 23ai; раньше отказ — `col::int` в запросе, NUMBER(10) |
| text | `large_string` | CLOB |
| varchar(n), char(n) | `large_string` с длиной | VARCHAR2(n CHAR) |
| bytea | `large_string` hex с `\x` | CLOB с hex-текстом как есть |
| date | `date32` | DATE |
| timestamp(p) | `timestamp[p]` | TIMESTAMP(p); timestamp(0) — DATE |
| timestamptz(p) | `timestamp[p, tz=UTC]` | TIMESTAMP(p) WITH TIME ZONE в UTC |
| time | `large_string` | VARCHAR2(18 CHAR) |
| uuid | `large_string` | VARCHAR2(36 CHAR) |
| json, jsonb, inet, interval, xml, money, bit | `large_string` | CLOB |
| массивы, enum, составные, диапазоны | `large_string` без пары | отказ; CLOB по `fallback_as_varchar` или тип через `column_types` |

### Что отдаёт pg_stream_out с проводом arrow

У PostgreSQL серверного потока Arrow нет, и `pg_stream_out` при `wire =
arrow` собирает его из
двух вещей, которые сервер умеет сам. Типы колонок берутся у libpq описанием
стейтмента (`prepare` + `describe`, без выполнения). Затем сам `select`
выполняется один раз как `copy (<select>) to stdout (format csv)`, а
читатель CSV pyarrow разбирает поток в C по этой схеме и отдаёт пачки Arrow
блоками по `chunk_bytes`. Python значений не касается. Запрос пишется без
`copy` и без `;` в конце — обёртку добавляет инструмент.

```sql
select
    1::bigint                                    as id,
    12.50::numeric(10,2)                         as amount,
    1.0::float8 / 3                              as ratio,
    E'tab\tx'                                    as note,
    null::text                                   as empty,
    true                                         as flag,
    date '2024-02-29'                            as d,
    timestamp '2024-02-29 13:14:15.123456'       as ts,
    timestamptz '2024-02-29 13:14:15+03'         as tz,
    '\x00ff'::bytea                              as bin,
    array[1, 2, null]                            as arr,
    'a1b2c3d4-0000-0000-0000-000000000001'::uuid as u,
    interval '1 day 2 hours'                     as iv,
    '{"a": [1, "x"]}'::jsonb                     as js
```

| Колонка | Тип postgres | Тип в схеме Arrow | Значение |
|---|---|---|---|
| id | bigint | `int64` | `1` |
| amount | numeric(10,2) | `decimal128(10, 2)` | `Decimal('12.50')` |
| ratio | double precision | `double` | `0.3333333333333333` — точно на любой версии: сессия выгрузки ставит `extra_float_digits = 3` |
| note | text | `large_string` | `'tab\tx'` — настоящая табуляция |
| empty | text | `large_string` | `None` — null-бит Arrow; пустая строка остаётся `''` |
| flag | boolean | `bool` | `True` |
| d | date | `date32[day]` | `2024-02-29` |
| ts | timestamp | `timestamp[us]` | `2024-02-29 13:14:15.123456` |
| tz | timestamptz | `timestamp[us, tz=UTC]` | `2024-02-29 10:14:15+00:00` — момент, не настенное время |
| bin | bytea | `large_string` | `'\x00ff'` — hex-текст сервера, не байты |
| arr | int[] | `large_string` | `'{1,2,NULL}'` — текст массива postgres |
| u | uuid | `large_string` | `'a1b2c3d4-...'` |
| iv | interval | `large_string` | `'1 day 02:00:00'` |
| js | jsonb | `large_string` | `'{"a": [1, "x"]}'` |

Правило раскладки: `smallint`/`integer`/`bigint` — `int16`/`int32`/`int64`,
`real`/`double precision` — `float`/`double`, `boolean` — `bool`,
`numeric(p, s)` до 38 знаков — `decimal128(p, s)`, `date` — `date32`,
`timestamp` — `timestamp[us]`, `timestamptz` — `timestamp[us, UTC]`. Всё
остальное — `large_string` с текстом, как его печатает сервер: `text`,
`char`, `bytea` (`\x`-hex), массивы (`{1,2}`), `time`, `uuid`,
`json`/`jsonb`, `inet`, `interval`, `money`, `bit`, enum, составные типы,
диапазоны, геометрия, `xml`. Так устроен CSV: двоичных данных и списков в
нём нет, а вот `decimal`, даты и время читатель Arrow собирает сам.

Что запрос обязан привести сам (ошибка приходит до выполнения, по описанию
стейтмента, тяжёлый запрос не запускается):

| Колонка | Почему | Что писать в select |
|---|---|---|
| `numeric` без точности | масштаб значений неизвестен | `col::numeric(30, 12)` или `col::text` |
| `numeric` шире 38 знаков | читатель CSV Arrow не собирает `decimal256` | `col::text` |
| `inet` с маской | текст `10.0.0.1/24` | для IPv4 ClickHouse — `host(col)` |

Точность `timestamp(p)` едет в единицу Arrow: `timestamp(0)` —
`timestamp[s]`, `timestamp(1..3)` — `timestamp[ms]`, остальное —
`timestamp[us]`; приёмник по ней сверяет и создаёт колонку той же точности.
`numeric` со значением `NaN` и `timestamp` со значением `infinity` читатель
CSV Arrow не собирает — отказ с текстом причины; в select их приводят
`::float8` (NaN сохраняется) или `::text`.

Строка CSV обязана уместиться в один блок читателя: блок равен `chunk_bytes`,
но не меньше 1 MiB. Строка шире (большой `text`, `bytea`, `jsonb`)
отвергается с ошибкой `a row must fit into one block, raise chunk_bytes`;
`chunk_bytes` до 64 MiB решает.

### Что принимает pg_stream_in с провода arrow

`pg_stream_in` пишет каждую пачку писателем CSV pyarrow в C, и блок уходит
в `copy dwh.orders (id, amount, note) from stdin (format csv)`, который
приёмник строит сам по контракту; колонки в нём идут в порядке полей
потока, шапки в теле нет (`HEADER` не
указывать), значения разбирает сервер по типу колонки. Одна транзакция.

`copy_options.exact_floats = true` везёт колонки `float` и `double` шестнадцатеричной
записью C (`0x1.4522c9e2190c1p-986`), которую `strtod` любого postgres
разбирает бит в бит; десятичную запись Greenplum 6 для редких значений
(`1.942e-297`, одно на несколько тысяч случайных) округляет на одну ULP.
Запись считается pyarrow.compute по битам значения, без Python на каждое
значение. Только в колонки `real` и `double precision`: в `numeric` или
`text` hex-текст не ляжет.

Ответ насосов postgres — не только счётчик: статус сервера (`COPY 2000`),
выполненный стейтмент, pid и версия сервера, а также всё, что сервер сообщил
за время команды — notices (RAISE NOTICE, предупреждения) и уведомления
NOTIFY, если сессия их слушала. Насосы Oracle отдают число строк,
координаты сессии (sid, serial, instance, db, service, версия) и
предупреждения драйвера; насосы ClickHouse — сводку сервера
(прочитано/записано/результат, время), id запроса, имя сервера, часовой
пояс и формат; у потоковой выгрузки сводка снята с заголовков в начале
ответа.

Что ложится куда:

| Тип в схеме Arrow | Тип postgres |
|---|---|
| `int*`, `uint*` | smallint, integer, bigint, numeric |
| `decimal128`, `decimal256` | numeric(p, s) |
| `float`, `double` (с NaN и inf) | real, double precision |
| `bool` | boolean |
| `string`, `large_string` | text и любой тип с текстовым вводом: uuid, json, inet, interval, массив `{1,2}`, bytea `\x00ff` |
| `date32` | date |
| `timestamp[us]` | timestamp |
| `timestamp[us, tz=UTC]` | timestamptz; в `timestamp` ляжет настенное время UTC |
| `binary`, `list`, `struct`, `map` | **отказ до загрузки**: CSV их не несёт, источник отдаёт текст |

Ловушки:

- **Двоичные данные в bytea** едут только hex-текстом с префиксом `\x`: из
  ClickHouse — `concat('\\x', hex(col))`, из Oracle — `'\x' || rawtohex(col)`.
  Голый hex без префикса ляжет в bytea как текст.
- **Массивы** едут текстом postgres: из ClickHouse —
  `concat('{', arrayStringConcat(arr, ','), '}')`; `Array` в схеме потока
  провод arrow у `pg_stream_in` отвергает.
- **timestamptz в Oracle**: драйвер Oracle отбрасывает смещение и хранит
  настенное время в поясе сессии. Выгружайте `col at time zone 'UTC'` в
  `timestamp(6)` Oracle.
- **bytea в Oracle**: `encode(col, 'hex')` без префикса — Oracle сам
  приводит hex-строку к RAW.
- **LOB-колонки в Oracle последними** (CLOB, JSON): ORA-24816 при длинном
  bind после LOB.
- **Greenplum 6 и очень малые double** (`1e-297`) — ошибка в младшем бите
  при разборе десятичной записи; `exact_floats = true` кладёт их бит в бит.
- **Greenplum 6 и double у самого максимума** (`1.7976931348623157e308`,
  `0x1.ffffffffffffep+1023` и выше) — сегменты отвергают такую строку COPY
  как `value out of range: overflow` в любой записи; это предел сервера,
  `exact_floats` не помогает.

### Что принимает ch_stream_in с потока arrow

`ch_stream_in(database, table_name, schema_strategy, delete_strategy,
insert_strategy, rules, unknown_types, create_table, before, after)`
принимает поток arrow с контрактом от любого `*_sync_out`: `pg_stream_out` с
`wire = arrow`, `ch_stream_out`, `ora_stream_out`. Стратегии те же, что у
`pg_stream_in`.

Типы колонок, которые приёмник создаёт по контракту:

| Семейство потока | Колонка ClickHouse |
|---|---|
| целые | `Int8`…`Int256`, `UInt8`…`UInt256` по ширине и знаку |
| float | `Float32`, `Float64` |
| decimal | `Decimal(p, s)`; без точности — тип без пары |
| boolean | `Bool` |
| строки | `String` |
| date | `Date32` |
| timestamp | `DateTime64(p)`, с поясом — `DateTime64(p, 'UTC')` |
| uuid | `UUID` |
| json, inet, interval, money, xml, bit, bytea | `String` |
| time, массивы, диапазоны, геометрия | пары нет: ошибка или `String` по `fallback_as_varchar` |

Nullable колонка потока становится `Nullable(...)`, объявленная not null —
обычной колонкой. `rules.column_types` пишутся типом ClickHouse и
проверяются сервером приёмника до любого DDL.

Транзакций у ClickHouse нет, поэтому приёмник грузит в двойник
`<table>__ex` и меняет таблицы местами `exchange tables`. Читатели не видят
частичной загрузки, прежняя версия остаётся в `__ex` до следующей загрузки.
Стратегия удаления решает, какие прежние строки перенести в двойник до
потока:

- `nothing` — все, поток дописывается к ним;
- `truncate`, `delete_all` — ни одной;
- `delete_where` — все, кроме подпавших под условие.

`exchange tables` работает только в базе с движком `Atomic`; для базы
`Ordinary` приёмник отказывает до любого DDL.

Поток вставляется через `input()`: `insert into db.t__ex (колонки таблицы)
select поля потока from input('структура') format ArrowStream`. Сервер
сопоставляет поля потока по именам, `select` переименовывает их в колонки
таблицы по `rename_columns`, без перекодирования пачек.

Таблицу по умолчанию приёмник создаёт реплицируемой:

```sql
create table {database}.{table_name}[ on cluster {cluster}] ({columns})
engine = ReplicatedMergeTree order by {order_by}
```

Переменные шаблона ClickHouse:

- `{database}`, `{table_name}` — база и имя, экранированные драйвером;
- `{columns}` — колонки с типами из плана приёмника;
- `{order_by}` — ключ сортировки из параметра `order_by`: `id`, `(dt, id)`,
  по умолчанию `tuple()`;
- `[ on cluster {cluster}]` — необязательная часть: текст в квадратных
  скобках выпадает целиком, если параметр `cluster` пуст.

Обязательные переменные стоят вне квадратных скобок, `{cluster}` — только
внутри; каждая обязана встретиться. Литеральные фигурные и квадратные
скобки удваиваются: `{{`, `}}`, `[[`, `]]`.

`ReplicatedMergeTree` без аргументов берёт путь в Keeper из настроек
сервера (`/clickhouse/tables/{uuid}/{shard}`), а макрос `{uuid}` сервер
подставляет только в запросе `on cluster`. Поэтому шаблон по умолчанию
требует `cluster`; для сервера без Keeper передайте шаблон с `MergeTree`:

```sql
create table {database}.{table_name}[ on cluster {cluster}] ({columns})
engine = MergeTree order by {order_by} partition by toYYYYMM(dt)
```

С `cluster` приёмник выполняет `on cluster` всё DDL: создание, `drop`,
`rename`, двойник и `exchange tables`. Вставка и выборки идут на узел
соединения, остальные реплики шарда получают данные репликацией. Кластер
проверяется по `system.clusters` до любого DDL. Для кластера из нескольких
шардов приёмник кладёт строки только в шард узла соединения.

Колонки `order_by` не могут быть `Nullable`: объявите их not null у
источника (`columns` у `pg_stream_out`).

### Скорость Arrow из PostgreSQL

Сам `copy ... to stdout (format csv)` отдаёт около 500 тысяч строк в секунду
на узкой таблице, читатель CSV pyarrow разбирает 5 миллионов, поэтому
выгрузка упирается в сервер: около 400 тысяч строк в секунду. Загрузка —
около 880 тысяч (писатель CSV pyarrow и `copy ... from stdin`). Цепочка
pg -> pg целиком — около 300 тысяч на узкой таблице и 160 тысяч на широкой
с массивами, uuid, json и bytea.

## Несколько стейтментов в одном вызове

Насос выполняет одну команду: COPY, `INSERT ... FORMAT`, executemany,
выборку источника. Вызов
инструмента открывает соединение и закрывает его на выходе, поэтому всё, что
живёт в сессии, — временная таблица, `set local`, `alter session`, `SET` —
вместе с вызовом и умирает. Чтобы загрузка во временную таблицу и её разбор
были одним вызовом, у каждого источника и приёмника sync есть аргументы
`before` и `after`:
списки стейтментов, которые идут по порядку в той же сессии, `before` — до
команды насоса, `after` — после. По трубе при этом едут только данные
команды насоса; строки выборок из `before` и `after` никуда не
возвращаются, в отчёт попадает итог каждого шага.

Что отчёт показывает по шагу, зависит от базы: PostgreSQL — статус сервера
(`CREATE TABLE`, `DELETE 1`, `INSERT 0 2`), Oracle — число затронутых строк
(у DDL и блока PL/SQL это 0), ClickHouse — счётчики сводки у команды или
значение у выборки.

```
2 rows written into dwh.stage_tmp
schema: create (table is missing)
after:
- DELETE 1: delete from dwh.target t using dwh.stage_tmp s where t.id = s.id
- INSERT 0 2: insert into dwh.target select id, v from dwh.stage_tmp
- DROP TABLE: drop table dwh.stage_tmp
```

Проверка, которая должна остановить насос, пишется ошибкой на стороне
сервера, а не выборкой: PostgreSQL — `do $$ begin if ... then raise
exception '...'; end if; end $$`, Oracle — `begin if ... then
raise_application_error(-20001, '...'); end if; end;`, ClickHouse —
`select throwIf(count() > 0, 'target is not empty') from db.t`.

### Что делает каждая база

PostgreSQL: весь вызов — одна транзакция. Ошибка любого шага, включая
последний в `after`, откатывает всё: и COPY, и предыдущие шаги. COPY не
умеет upsert, поэтому стандартная схема — `pg_stream_in` в staging-таблицу и
разбор её в `after`:

```json
{
  "schema_name": "dwh",
  "table_name": "stage_tmp",
  "schema_strategy": {"kind": "create_if_not_exists"},
  "after": [
    "delete from dwh.target t using dwh.stage_tmp s where t.id = s.id",
    "insert into dwh.target select id, v from dwh.stage_tmp",
    "drop table dwh.stage_tmp"
  ]
}
```

`insert ... on conflict` тоже подходит, но только с PostgreSQL 9.5 и выше,
а Greenplum 6 его не знает; пара `delete` + `insert` работает везде.
Источник тоже видит `before`: `pg_stream_out` с `before = ["create temp table
snap as select ..."]` и `sql = "select id, v from snap"` отдаёт снимок.

Oracle: каждый элемент — одна команда без `;` в конце либо один анонимный
блок PL/SQL. DML из `before`, загрузка и DML из `after` — одна транзакция с
одним commit после `after`; ошибка до commit откатывает всё. DDL
(`truncate`, `exchange partition`, `rename`) Oracle фиксирует сам, и
вместе с ним фиксируется всё, что было в транзакции до него, — это не
ошибка, а способ работы базы. Временная таблица здесь глобальная и
создаётся заранее, один раз; `ora_stream_in` грузит в неё, как в обычную:

```json
{
  "schema_name": "HR",
  "table_name": "stage_tmp",
  "schema_strategy": {"kind": "error_if_not_exists"},
  "before": ["delete from stage_tmp"],
  "after": [
    "delete from target where id in (select id from stage_tmp)",
    "insert into target select id, v from stage_tmp"
  ]
}
```

У `ora_stream_out` так же: строки, вставленные в глобальную временную
таблицу шагом `before`, видит только запрос этой сессии, commit один после
`after`.

ClickHouse: транзакций нет, но у насоса одна сессия сервера на весь вызов,
поэтому `SET` и `create temporary table` из `before` доживают до команды
насоса и `after`. Отката нет: ошибка шага `after` оставляет уже загруженные
строки на месте. Приёмник грузит только в таблицу базы `Atomic`, временная
таблица годится для служебных шагов:

```json
{
  "database": "dwh",
  "table_name": "staged",
  "schema_strategy": {"kind": "error_if_not_exists"},
  "before": [
    "set max_insert_block_size = 1000",
    "create temporary table seen (n UInt64)"
  ],
  "after": [
    "insert into seen select count() from dwh.staged",
    "insert into dwh.target select id, upper(v) from dwh.staged"
  ]
}
```

У `ch_stream_out` запрос видит временную таблицу из своего `before`:
`before = ["create temporary table snap (id UInt64, v String)", "insert into
snap select ..."]` и `sql = "select id, v from snap"`.

## Staging и атомарная подмена

Транзакция живёт в соединении, соединение живёт ровно один вызов
инструмента, а между базами распределённых транзакций нет. Поэтому
загрузка, которая должна подменить таблицу или партицию целиком, строится
так: тяжёлая работа идёт в промежуточную таблицу отдельными вызовами, а
целевую таблицу меняет один дешёвый последний шаг, который база выполняет
атомарно. Падение посередине оставляет мусор в staging, но целевую таблицу
не трогает.

PostgreSQL: подготовка и подмена — `pg_query`, который выполняет несколько
команд через `;` одной транзакцией; загрузка — насос.

```sql
-- pg_query: подготовка
create table dwh.sales_new (like dwh.sales including all);
-- pg_stream_in: table = "dwh.sales_new", schema error_if_not_exists
-- pg_query: подмена одной транзакцией
alter table dwh.sales rename to sales_old;
alter table dwh.sales_new rename to sales;
drop table dwh.sales_old;
```

Oracle: подмена партиции — `exchange partition`, он атомарен сам по себе и
может идти шагом `after` того же насоса, что грузил staging:

```json
{
  "table_name": "stage_part",
  "schema_strategy": {"kind": "error_if_not_exists"},
  "before": ["truncate table stage_part"],
  "after": ["alter table part_target exchange partition p_202409 with table stage_part"]
}
```

ClickHouse: `replace partition` подменяет партицию целиком, `exchange
tables` — таблицу; оба атомарны и идут шагом `after`:

```json
{
  "database": "dwh",
  "table_name": "stage",
  "schema_strategy": {"kind": "error_if_not_exists"},
  "before": ["truncate table dwh.stage"],
  "after": ["alter table dwh.part_target replace partition 202409 from dwh.stage"]
}
```

Таблица staging обязана повторять раскладку целевой: у Oracle — колонки и
типы, у ClickHouse — ещё и ключ партиционирования с ключом сортировки.

## Пары движков: загрузка родными типами без нейтрального формата

Специфичная загрузка между двумя конкретными движками описывает только
себя: контракт колонок едет как его отдал источник, сверку и DDL делает
пара «источник → приёмник» в своём пакете, тела идут текстовым COPY без
перекодирования. Нейтральные семейства типов и Arrow сюда не входят.

### postgres → postgres

| Инструмент | Что делает |
|---|---|
| `pg_stream_out(sql, wire, columns, copy_options)` | колонки выборки от libpq (`PQprepare` + `PQdescribePrepared`, без планирования и выполнения): имя, OID, typmod, текст типа как печатает `format_type`, версия сервера и `integer_datetimes` из стартового пакета; первый кадр — контракт с декларациями `columns` поверх, дальше байты `copy (<select>) to stdout` в раскладке `wire` либо поток arrow; COPY читается циклом libpq (`PQgetCopyData`) в рабочем потоке с накоплением порций в C, на уровне `psql`; о приёмнике источник не знает ничего |
| `pg_stream_in(schema_name, table_name, schema_strategy, delete_strategy, insert_strategy, rules, unknown_types, create_table, copy_options)` | приёмник: по `source_engine` первого кадра берёт пару из реестра `boba.transfer.postgres`; провод arrow любого источника идёт нейтральным путём по семействам типов |

Рычаг `wire` у источника обязателен, значения `csv`, `tsv`, `binary`,
`arrow`: `arrow` понимает любой приёмник и узлы преобразования потока;
`csv` — приёмник postgres; `tsv` — приёмник ClickHouse; `binary` — только
приёмник postgres той же мажорной версии с `integer_datetimes = on` и
только встроенные типы колонок. Совместимость проверяет приёмник по кадру
`schema`, где источник оставил версию и `integer_datetimes` своего сервера:
несовместимый `binary` — отказ до любого DDL с подсказкой перезапустить
`pg_stream_out` с `wire = csv`. Приёмник без пары для движка источника тоже
отказывает: ему подходит только `arrow`.

Декларации у источника (`columns`): `nullable`, потому что серверу у выборки
он неизвестен, и `type_text` — текст типа, когда сервер отдал только OID
(enum, composite, расширения), схему указывать явно: `sales.mood`.

Правила у приёмника (`rules`): `rename_columns` (колонка приёмника: поле
потока) и `column_types` (колонка приёмника: тип postgres текстом как есть).
Тексты `column_types` разбирает сам сервер приёмника описанием
`select null::<тип>`, неизвестный тип — ошибка до любого DDL.

Шаблон таблицы (`create_table`), когда стратегия схемы создаёт таблицу:
цельный стейтмент с переменными `{schema_name}`, `{table_name}` и
`{columns}`, каждая обязательна, других подстановок нет, литеральные
фигурные скобки удваиваются. Приёмник подставляет схему и имя отдельными
экранированными идентификаторами и колонки из плана с типами после
`column_types` и `unknown_types`. По умолчанию
`create table {schema_name}.{table_name} ({columns})`; особенности таблицы
пишутся в шаблон:

```sql
create table {schema_name}.{table_name} ({columns}) with (fillfactor = 70)
create table {schema_name}.{table_name} ({columns})
    with (appendonly = true, orientation = column) distributed by (id)
create unlogged table {schema_name}.{table_name} ({columns})
```

Шаблон без обязательной переменной или с чужой отклоняется до любого DDL;
ошибку сервера в опциях таблицы приёмник показывает как есть.

Сверка с существующей таблицей идёт по OID и typmod из `pg_attribute`:

| Случай | Вердикт |
|---|---|
| другой OID: `uuid` в `text`, `json` в `jsonb`, `inet` в `cidr`, `int4range` в `int8range` | ошибка `type differs` |
| numeric: scale таблицы меньше или целых разрядов меньше | ошибка; шире — предупреждение |
| varchar, bpchar, bit, varbit: длина таблицы меньше | ошибка; длиннее — предупреждение |
| timestamp, time: точность таблицы грубее | ошибка; тоньше — предупреждение |
| один OID, другой typmod у прочих типов | предупреждение |
| пользовательский тип (enum, composite, расширение) с именем в `columns[].type_text` или `rules.column_types` | имя разбирается на приёмнике `select null::<тип>`, дальше по его OID: другой тип — ошибка |
| пользовательский тип без имени | предупреждение, сверить нельзя: OID источника на приёмнике не значит ничего |
| поток nullable, колонка not null | ошибка; наоборот — предупреждение |

DDL: текст типа источника как есть, `column_types` перекрывает; у колонки
без текста типа решает `unknown_types`: `fail_on_unknown` (по умолчанию) —
ошибка с OID и подсказкой, `fallback_as_varchar` — `varchar`. Стратегии
схемы, удаления и вставки те же, что у семейства sync ниже. Всё одной
транзакцией приёмника. Стенд: `test_pg_transfer.py` по всем postgres и
Greenplum, `test_pg_realistic.py` между двумя серверами.

Другие пары (`ch → pg`, `ora → pg`, `pg → ch`) добавляются пакетами
`packages/infra/stream/boba-stream-<src>-to-<dst>` с entry point на движок
источника; приёмник без установленной пары отвечает понятной ошибкой.

### clickhouse → clickhouse

| Инструмент | Что делает |
|---|---|
| `ch_stream_out(sql, wire, columns, chunk_bytes)` | `wire = tsv`: запрос выполняется один раз в `TabSeparatedWithNamesAndTypes`, две строки шапки уходят кадром `schema` как контракт с текстами типов ClickHouse как их печатает сервер, остальные байты — кадрами `rows` как `TabSeparated`; `wire = arrow`: поток Arrow IPC с нейтральным контрактом и декларациями `columns` для приёмников других движков |
| `ch_stream_in(database, table_name, ...)` | по `source_engine = clickhouse` и `wire = tsv` берёт пару из реестра `boba.transfer.clickhouse`: тексты типов сравниваются с `system.columns` приёмника без обёрток `Nullable` и `LowCardinality`, DDL строится текстом типа источника, `column_types` нормализует сервер приёмника, тела идут в `input()` двойника как `TabSeparated` без перекодирования |

Так `LowCardinality`, `DateTime64` с поясом, `Enum8`, `FixedString`, `Array`,
`Map`, `Decimal` любой точности, `IPv6` и `UUID` доезжают тем же типом; по
arrow часть из них стала бы строками или потеряла пояс. `columns` у `tsv`
не принимаются: типы ClickHouse едут как есть.

Сверка с существующей таблицей по разобранным текстам типов, обёртки
`Nullable` и `LowCardinality` сняты:

| Случай | Вердикт |
|---|---|
| тексты совпадают | ok |
| разные семейства: `Int64` в `String`, `Array` в `Map` | ошибка |
| целые и float: приёмник уже (`Int64` в `Int32`) или теряет знак (`Int64` в `UInt64`) | ошибка; шире — предупреждение |
| `Decimal`: scale или целые разряды приёмника меньше | ошибка; больше — предупреждение |
| `DateTime`, `DateTime64`: другой пояс | ошибка: текст TabSeparated читается в поясе колонки приёмника |
| `DateTime64`: точность приёмника грубее | ошибка; тоньше — предупреждение |
| `FixedString(n)` короче | ошибка; длиннее или `String` — предупреждение |
| `Enum8`, `Enum16` в `String` | предупреждение; обратно или другие значения — ошибка |
| `Date` в `Date32` | предупреждение; обратно — ошибка |
| `Array`, `Map`, `Tuple`, `UUID`, `IPv4`, `IPv6` | только точное совпадение текста |
| nullable поле в колонку без `Nullable` | ошибка; обратное — предупреждение |

`Tuple` с именованными полями новые серверы печатают в несколько строк,
старые в одну: пробелы и переводы строк перед сверкой схлопываются.

### postgres → clickhouse

`pg_stream_out` с `wire = tsv` и `ch_stream_in`: байты `copy ... (format text)`
совпадают с `TabSeparated` по экранированию `\t`, `\n`, `\\` и по `\N`
для NULL, поэтому тела идут в `input()` двойника как есть. Приёмник берёт
пару из реестра `boba.transfer.clickhouse` по `source_engine = postgres` и
переводит контракт postgres (OID и typmod) в типы ClickHouse:

| postgres | ClickHouse | Как читается |
|---|---|---|
| `int2`, `int4`, `int8`, `oid` | `Int16`, `Int32`, `Int64`, `UInt32` | |
| `float4`, `float8` | `Float32`, `Float64` | `NaN`, `Infinity` читаются |
| `numeric(p, s)`, p ≤ 76 | `Decimal(p, s)` | `NaN` numeric — ошибка сервера |
| `numeric` без точности, `numeric(p, s)` при p > 76 | типа нет | `::numeric(p, s)` в запросе или `String` по `fallback_as_varchar` |
| `bool` | `Bool` | `t` и `f` через настройки чтения |
| `text`, `varchar`, `bpchar`, `name`, `char` | `String` | `bpchar` едет с пробелами до длины |
| `bytea` | `String` | hex-текст `\x...` как есть |
| `date` | `Date32` | вне 1900–2299 сервер молча прижимает к границе; даты до нашей эры — ошибка |
| `timestamp(p)` | `DateTime64(p)` | те же границы |
| `timestamptz(p)` | `DateTime64(p, 'UTC')` | сессия COPY в UTC, суффикс `+00` читается |
| `uuid` | `UUID` | |
| `json`, `jsonb` | `JSON`, где сервер его умеет: до 24 — `Object('json')` с настройкой, 24 — `JSON` с настройкой, с 25 — `JSON`; nullable колонка — `Nullable(JSON)` только с 25, иначе `String` | в `JSON` читаются только объекты, массив или скаляр на верхнем уровне — ошибка сервера |
| `inet` | `IPv6` | IPv4 хранится как `::ffff:a.b.c.d`; значение с маской — ошибка сервера, берите `host(ip)` |
| `cidr`, `macaddr`, `money`, `bit`, `xml`, `time`, `timetz`, `interval`, `tsvector`, геометрия, диапазоны | `String` | текст postgres как есть |
| массивы любой размерности, enum, composite, расширения | типа нет | `column_types` с типом ClickHouse и литерал в запросе, например `'[' \|\| array_to_string(a, ',') \|\| ']'`, или `String` по `fallback_as_varchar` |

Nullable — по контракту: колонка без декларации `nullable: false` у
`pg_stream_out` становится `Nullable(...)`. Сверка с существующей таблицей —
теми же правилами расширения, что у пары clickhouse → clickhouse.
`column_types` перекрывает перевод, тип пишется текстом ClickHouse.

Ошибка при самой загрузке терминальна: поток читается один раз, повторить
его строкой нельзя. Приёмник не делает `exchange tables`, таблица не
меняется, в ответе имя колонки и текст сервера; перезапуск — с
`column_types[col] = "String"` или с `cast` в запросе.

## Семейство sync: приёмник со стратегиями

Насосы выше гонят байты в стейтмент, который написал вызывающий. Семейство
`*_sync_out` -> `*_sync_in` — другое: приёмнику говорят, куда положить
данные и по каким стратегиям, а контракт колонок он получает из самого
потока и дальше сам создаёт, сверяет, пересоздаёт таблицу и грузит.

### Провод и контракт

Поток — кадры: первый `schema` (формат тел, движок источника, контракт
колонок), дальше `rows`. Источник запрос не переписывает: `select` пишет
LLM, инструмент выполняет его как есть. Контракт — описание колонок от
драйвера (postgres: тип и typmod, nullable серверу неизвестен; Oracle: тип,
точность, null_ok; ClickHouse: типы с Nullable) с декларациями `columns`
поверх:

```json
{"sql": "select id, amount, note from sales.orders o left join sales.notes n using (id)",
 "columns": [
   {"name": "id",     "nullable": false},
   {"name": "amount", "family": "decimal", "precision": 20, "scale": 6},
   {"name": "note",   "family": "string", "char_length": 200}
 ]}
```

Заданное перекрывает найденное, незаданное остаётся от драйвера; имя,
которого нет в ответе, — ошибка. Так задаётся `not null` у колонки из
`left join`, точный decimal, длина строки или `source_type` — имя типа,
когда драйвер отдал только OID (`sales.mood`, `hstore`, `vector(3)`).

### Алгоритм типов: один на оба провода

1. Источник получает колонки от драйвера и сливает их с `columns` от LLM.
   Это контракт, он едет в кадре `schema` в обоих режимах. У postgres это
   два вызова libpq до COPY: `PQprepare` (сообщение Parse с текстом select,
   сервер разбирает его и резолвит имена по каталогу) и
   `PQdescribePrepared` (сообщение Describe, ответ RowDescription с именем,
   OID типа и typmod каждой колонки). Bind и Execute не отправляются:
   планирования и чтения данных нет, около миллисекунды даже для запроса на
   часы выполнения, счётчики `pg_stat_database` не растут. Данные читает
   только COPY. У Oracle это `FetchInfo` курсора после `parse`, у ClickHouse
   схема ArrowStream.
2. В Arrow-режиме схема потока строится из этого же контракта: декларация
   precision/scale у `numeric` без точности даёт `decimal128(p, s)`, смена
   семейства на string — `large_string`. Значение шире объявленного типа
   читатель Arrow отвергает.
3. Приёмник берёт контракт из кадра `schema`, применяет `rules`:
   `rename_columns` (колонка приёмника: поле потока) и `column_types`
   (колонка приёмника: тип текстом, `{"v": "vector(3)", "amount":
   "numeric(20,6)"}`). Тексты `column_types` приёмник разбирает сам, через
   описание `select null::<тип>` у своего сервера, и подменяет ими тип
   колонки потока. Неизвестный серверу тип — ошибка до любого DDL.
4. Полный набор сверяется с колонками таблицы, если она есть, и из него же
   строится `create table`.

Сверка идёт по семейству и параметрам (ширина, precision/scale, длина
строки и bit, единица и пояс времени, nullable), а при одном движке
дополнительно по тексту типа: `json` в `jsonb` и `inet` в `cidr` —
предупреждение, `uuid` в `text` — ошибка семейства. Семейства «только по
имени» (array, range, geometry, textsearch, system, other) при одном движке
требуют совпадения имени: `int4range` в `int8range` — ошибка; между
движками сверить нельзя — предупреждение. Все 76 имён встроенного реестра
psycopg разложены по семействам, сторож `TestRegistryCoverage` не даёт
таблице отстать.

### Типы postgres в контракте

| Тип источника | Семейство и параметры | DDL pg -> pg | DDL из другого движка |
|---|---|---|---|
| int2 / int4 / int8, oid | integer 16 / 32 / 64, oid — unsigned 32 | как у источника | smallint / integer / bigint, unsigned на ступень шире |
| float4 / float8 | float 32 / 64 | как у источника | real / double precision |
| numeric(p,s) / numeric | decimal(p,s) / без точности | как у источника | numeric(p, s) / numeric |
| bool, date | boolean, date | как у источника | boolean, date |
| timestamp(p) / timestamptz(p) | timestamp, единица s / ms / us по p, пояс | как у источника | timestamp(p) [with time zone] |
| time(p) / timetz(p) | time, единица и пояс | как у источника | time(p) [with time zone] |
| text, varchar(n), bpchar(n), name | string, длина n | как у источника | character varying(n) / text |
| bytea | binary | как у источника | bytea |
| uuid | uuid | как у источника | uuid |
| json / jsonb / jsonpath | json | как у источника | jsonb |
| interval | interval | как у источника | interval |
| inet / cidr / macaddr / macaddr8 | network | как у источника | inet |
| bit(n) / varbit(n) | bit, длина в битах | как у источника | bit varying(n) |
| money | money | как у источника | money |
| xml | xml | как у источника | xml |
| point / line / lseg / box / path / polygon / circle | geometry, только по имени | как у источника | `column_types`, иначе стратегия |
| int4range … datemultirange | range, только по имени | как у источника | то же |
| tsvector / tsquery / gtsvector | textsearch, только по имени | как у источника | то же |
| массивы любого типа | array, только по имени (`integer[]`) | как у источника | то же |
| oid-подобные: regclass, xid, tid, pg_lsn, aclitem, refcursor … | system, только по имени | как у источника | то же |
| record | other под именем | как у источника | то же |
| OID вне реестра psycopg: enum, composite, hstore, vector | other без имени (`oid N`) | `columns[].source_type` или `column_types`, иначе стратегия | то же |

Стратегия `unknown_types` у `pg_stream_in` решает судьбу колонок семейства
other, для которых типа нет ни в контракте, ни в `column_types`:

| kind | Неизвестный тип |
|---|---|
| `fail_on_unknown` (по умолчанию) | ошибка с тем, что о типе известно (имя от драйвера или голый OID) и подсказкой объявить тип в `rules.column_types` или взять `fallback_as_varchar`; LLM решает сам |
| `fallback_as_varchar` | строковый тип движка без предела длины: `varchar` у postgres, `String` у ClickHouse |

Схему в имени типа указывать (`sales.mood`): search_path приёмника не тот,
что у сессии LLM. Расширение должно быть у приёмника, иначе `create table`
упадёт с откатом всей загрузки.

### Раскладки и кто их принимает

| Раскладка | Кто отдаёт | Кто принимает | Что сохраняется |
|---|---|---|---|
| `csv` | `pg_stream_out` | `pg_stream_in` | всё, что печатает COPY: `infinity`, `NaN` у numeric, `numeric(999,5)`, enum, составные, диапазоны |
| `tsv` | `pg_stream_out`, `ch_stream_out` | `pg_stream_in`, `ch_stream_in` | COPY text и TabSeparated — одна раскладка; с контрактом типов источника |
| `binary` | `pg_stream_out` | `pg_stream_in` | COPY binary, только postgres одной мажорной версии |
| `arrow` | `pg_stream_out`, `ora_stream_out`, `ch_stream_out` | `pg_stream_in`, `ch_stream_in`, `ora_stream_in` | типы Arrow; чего Arrow не несёт — `::text` в запросе |

### Стратегии

Стратегия — объект с `kind`: `schema_strategy` — `create_if_not_exists`,
`error_if_not_exists`, `error_if_schema_changed`,
`drop_and_create_if_schema_changed` (`cascade`),
`backup_and_create_if_schema_changed`, `drop_and_create`, `backup_and_create`,
`do_nothing`; `delete_strategy` — `nothing`, `truncate`, `delete_all`,
`delete_where` (`where` как в SQL приёмника); `insert_strategy` — `full`,
`nothing`.

```json
{"schema_name": "dwh", "table_name": "orders",
 "schema_strategy": {"kind": "backup_and_create_if_schema_changed"},
 "delete_strategy": {"kind": "delete_where", "where": "dt >= date '2024-01-01'"},
 "insert_strategy": {"kind": "full"}}
```

Сверка поколоночная: сужение (decimal с меньшей scale, varchar короче,
timestamp грубее, nullable в not null, uint64 в bigint) — ошибка, расширение
— предупреждение; колонка потока без колонки в таблице или наоборот —
ошибка. Бэкап — переименование с суффиксом `_bak_YYYYMMDD_HHMMSS_ffffff`.
DDL приёмника: тексты типов postgres как есть, если источник — postgres
(включая enum и составные — они должны существовать в базе приёмника),
иначе по нейтральному типу: `numeric(p, s)`, `character varying(n)`,
`timestamp(p)`, `bigint`, uint64 — `numeric(20)`. Вся загрузка — одна
транзакция: NULL в `not null` или строка длиннее заявленной откатывает всё.

Ответ приёмника: сколько строк, что сделано со схемой и почему, сверка по
колонкам, что удалено:

```text
1000 rows written into dwh.orders
schema: backup_then_create (column amount: table numeric(10,2) truncates the scale of stream decimal(18, 4))
backup: orders_bak_20260926_101502_118273
columns:
- ok id: ok
- error amount: column amount: table numeric(10,2) truncates the scale ...
deleted: 0 rows by truncate table "dwh"."orders"
```

## Скорость

Замер на стенде: 300 тысяч строк из 8 колонок (NUMBER, NUMBER(14,2),
BINARY_DOUBLE, две VARCHAR2, DATE, TIMESTAMP(6), NUMBER(1)), Oracle 12.2 и
23 в одном контуре с хостом.

| Путь | Oracle 12.2 | Oracle 23 |
|---|---|---|
| чтение `fetchmany`, объекты Python | 292 тыс. строк/с | 255 тыс. |
| чтение пачками Arrow (`ora_stream_out`) | 402 тыс. | 364 тыс. |
| чтение пачками Arrow с записью CSV pyarrow | 350 тыс. | 331 тыс. |
| запись `executemany` пачкой Arrow (`ora_stream_in`) | 30 тыс. | 154 тыс. |
| запись `executemany` строками Python | 20 тыс. | 80 тыс. |
| запись `direct_path_load` | 123 тыс. | 134 тыс. |

Чтение упирается в сервер: Arrow быстрее объектов Python на треть. Запись
пачкой Arrow принимает bind'ы прямо из массивов и на 23 быстрее прямого
пути. `direct_path_load` приёмник не использует: у него нет транзакции
(`rollback` ничего не откатывает), он невозможен при триггере на таблице
(ORA-26086) и рядом с другими стейтментами в одной транзакции (ORA-26085),
а дубликат по уникальному индексу проходит «успешно» и оставляет индекс
UNUSABLE.

Память не растёт с объёмом: в процессе живёт одна пачка Arrow и одна
порция трубы, остальное уже у приёмника.

## Где это проверяется

Пакет `packages/testing/boba-pump-stand` держит помощников стенда (в том
числе трубу ОС, через которую два насоса работают одновременно) и тесты:

- `test_ora_sync.py` — насосы Oracle на всех Oracle стенда: круг Oracle ->
  Oracle с типами источника как есть, Oracle -> postgres и ClickHouse с
  созданием таблиц приёмником, postgres и ClickHouse -> Oracle с типами
  Oracle по контракту, стратегии приёмника, отказы источника с
  подсказками;
- `test_ora_pg_realistic.py`, `test_ora_ch_realistic.py`,
  `test_pg_ora_realistic.py`, `test_ch_ora_realistic.py` — отчёт магазина
  между Oracle и postgres/Greenplum/ClickHouse в обе стороны на всех
  стендах запросами, какими их пишет LLM: первая попытка с типами, которых
  приёмник не берёт, и её исправление, загрузка и повторная сверка,
  инкремент месяца, витрина с rename и column_types, дрейф схемы с бэкапом,
  шаблон таблицы, подмена шагами after, сухой прогон, обратный путь
  агрегата;
- `test_ch_arrow.py` — ClickHouse -> PostgreSQL и Oracle
  (`ora_stream_in` в заранее созданную таблицу) потоком Arrow на всей матрице
  версий: каждый тип ClickHouse, включая Int128, UInt256, Enum, IPv6, Map и
  Nested, едет как есть или текстом;
- `test_pg_sync.py`, `test_pg_sync_edges.py` — семейство sync: стратегии
  схемы на каждом postgres и Greenplum в обоих режимах провода (Arrow и
  COPY), декларации, пограничные типы, NULL, decimal, varchar, timestamp,
  потоки из ClickHouse и Oracle;
- `test_arrow_ports.py` — Arrow-порты toolkit над трубой ОС без баз;
- `test_pg_arrow.py` — PostgreSQL -> PostgreSQL, ClickHouse и Oracle
  (`ch_stream_in` и `ora_stream_in` в заранее созданную таблицу) потоком Arrow на всей матрице
  версий, обратные пути и ловушки;
- `test_arrow_ch_sync.py` — `ch_stream_in` на потоке arrow из postgres,
  Greenplum, Oracle и ClickHouse: широкая таблица типов, типы без пары, сверка
  шире/уже, двойник, ловушки Date/DateTime/Bool в Arrow ClickHouse;
- `test_pg_ch_sync.py` — пара postgres -> ClickHouse по tsv: типы и значения,
  JSON по версиям, отказы и `String` для типов без пары, ловушки сервера
  (маска inet, массив json, прижатые даты), витрина;
- `test_ch_pg_sync.py` — пара ClickHouse -> postgres по tsv на pg-16 и
  Greenplum 7: типы и значения, JSON с 24.x, отказы и `varchar` для типов без
  пары, `column_types`, сверка шире/уже, NUL в FixedString откатывает
  транзакцию, витрина;
- `test_ch_sync.py` — приёмник `ch_stream_in`: поток arrow из postgres и
  ClickHouse, пара ClickHouse -> ClickHouse по tsv с типами как есть, типы,
  двойник и `exchange tables`, стратегии, rename, шаблон, `ReplicatedMergeTree`
  on cluster, отказ базы не `Atomic`;
- `test_pg_transfer.py`, `test_pg_realistic.py` — пара postgres -> postgres:
  раскладки `wire`, стратегии, типы, отказ приёмника на несовместимый
  binary, стоимость описания;
- `test_pump_scripts.py` — `before` и `after` у источников и приёмников на
  каждой базе стенда: временная таблица в сессии источника и приёмника,
  upsert через staging, откат по ошибке шага `after` (PostgreSQL, Oracle) и
  его отсутствие (ClickHouse), `replace partition` и `exchange partition`
  из staging.

Запускаются из `compose/chainlit` с окружением из `launch.json` и маркером
`integration`. Стенды общие: тесты создают свои схемы и базы и сносят их по
завершении, пробы вне тестов должны делать то же самое.
