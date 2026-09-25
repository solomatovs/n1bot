# Перекачка данных между PostgreSQL, ClickHouse и Oracle

Этот документ описывает, как переливать данные между базами насосами boba:
какой поток байт отдаёт и принимает каждый насос, как в этом потоке выглядит
каждый тип, как стыковать два конца и где данные теряются молча. Всё, что
здесь написано, снято с живого стенда и проверено тестами: PostgreSQL от 9.0
до 19, Greenplum 6 и 7, ClickHouse 22.12, 23.12, 24.12, 25.12 и 26.7,
Oracle 12.2 Enterprise, 18 XE, 21 XE и 23 Free.

## Как устроена перекачка

Перекачка собирается из двух инструментов, соединённых трубой. Насос выгрузки
пишет байты в свой выходной порт, насос загрузки читает байты из входного
порта, хост соединяет порты, и оба насоса работают одновременно. Между ними
никто ничего не разбирает: какие байты выдал источник, такие получит
приёмник. Поэтому главное, что надо знать, — как выглядит поток на выходе
одного насоса и что умеет прочитать другой.

| База | Выгрузка | Загрузка | Формат потока |
|---|---|---|---|
| PostgreSQL | `pg_stream_out(sql)` | `pg_stream_in(sql, chunk_bytes)` | тот, что задан в `COPY ... WITH (...)` |
| ClickHouse | `ch_stream_out(sql, chunk_bytes)` | `ch_stream_in(sql, chunk_bytes)` | тот, что задан словом `FORMAT` в запросе |
| ClickHouse, Arrow | `ch_arrow_out(sql, chunk_bytes)` | `ch_arrow_in(sql, chunk_bytes)` | Arrow IPC: `ch_stream_*`, где формат ArrowStream уже выбран |
| PostgreSQL, Arrow | `pg_arrow_out(sql, chunk_bytes)` | `pg_arrow_in(sql, chunk_bytes, exact_floats)` | поток Arrow IPC, раздел ниже |
| Oracle | `ora_csv_out(sql)` | `ora_csv_in(sql, chunk_bytes)` | только CSV, правила ниже |
| Oracle, Arrow | `ora_arrow_out(sql)` | `ora_arrow_in(sql, chunk_bytes)` | поток Arrow IPC, раздел ниже |

У PostgreSQL и ClickHouse формат потока выбирает сам стейтмент, и сервер
пишет и читает его сам; инструменты в текст не заглядывают. У Oracle
серверного потока нет, поэтому насос делает CSV сам, и формат у него один —
отсюда и имя `ora_csv_*`.

`chunk_bytes` — размер порции между насосом и трубой, по умолчанию 256 КиБ:
крупнее — меньше системных вызовов на больших объёмах, мельче — раньше
первые данные у приёмника.

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

Именно такой поток ждёт `ora_csv_in`.

### Сессия COPY зафиксирована

Текст COPY зависит от настроек сессии, и без их фиксации один и тот же
запрос на двух серверах печатает разное. Все четыре насоса PostgreSQL
(`pg_stream_*` и `pg_arrow_*`) поднимают соединение с зафиксированными GUC
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

Эти значения — умолчания аргумента `session` у `pg_stream_out`,
`pg_stream_in`, `pg_arrow_out` и `pg_arrow_in`: вызов меняет любое из них,
когда поток нужен другим — приёмник ждёт `WIN1251`, деньги нужны в локали,
float короче или интервалы в `iso_8601`. Значения из вызова перекрывают
профиль соединения.

```json
{"session": {"client_encoding": "WIN1251", "extra_float_digits": 0}}
```

### Что принимает pg_stream_in

`COPY t FROM STDIN` с теми же опциями, что у выгрузки, читает ровно такой
поток. Значение каждого поля разбирает сервер по типу колонки, поэтому
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

При вставке такой поток сопоставляет колонки по именам, лишние колонки
шапки молча пропускает, а тип в шапке обязан совпасть с типом колонки
полностью: `UInt32` против `UInt64` — отказ, а не приведение. PostgreSQL
такую шапку не пишет, поэтому формат годится для ClickHouse -> ClickHouse.

### CSV

Тот же `select` с `format CSV`:

```text
1,12.5,"tab	new
line \ back ""q"", semi;",\N,true,0.3333333333333333,nan,"2024-02-29","2024-02-29 13:14:15.123456","[1,2]","{'k':1}",1,"x","a1b2c3d4-0000-0000-0000-000000000001"
```

| # | Колонка | Текст поля | Что это |
|---|---|---|---|
| 1 | id | `1` | числа без кавычек |
| 2 | amount | `12.5` | |
| 3 | note | `"tab⇥new↵line \ back ""q"", semi;"` | как CSV PostgreSQL |
| 4 | empty | `\N` | **NULL — `\N`, а не пустое поле**, как у PostgreSQL |
| 5 | flag | `true` | без кавычек |
| 6 | ratio | `0.3333333333333333` | |
| 7 | nan | `nan` | |
| 8 | d | `"2024-02-29"` | **даты в кавычках** |
| 9 | ts | `"2024-02-29 13:14:15.123456"` | |
| 10 | arr | `"[1,2]"` | |
| 11 | m | `"{'k':1}"` | |
| 12, 13 | t | `1`, `"x"` | **кортеж развернулся в два поля**: полей в записи на одно больше, чем колонок |
| 14 | u | `"a1b2c3d4-0000-0000-0000-000000000001"` | |

### Что принимает ch_stream_in

`insert into t [(колонки)] format <формат>` читает поток любого формата
ClickHouse; разбирает его сервер. Если значение надо привести до записи,
используется `input()` — табличная функция над телом запроса:

```sql
insert into dwh.events
select
    toUInt64(c1)                as id,
    upper(c2)                   as name,
    parseDateTimeBestEffort(c3) as created_at
from input('c1 String, c2 String, c3 String')
settings precise_float_parsing = 1
format TabSeparated
```

Настройки пишутся перед `format`. Это единственный способ загрузить в
ClickHouse данные с преобразованием одним стейтментом, и он нужен почти
всегда, когда источник — не ClickHouse.

При чтении CSV и TabSeparated ClickHouse ведёт себя так:

| Поле в потоке | Колонка `Nullable(...)` | Обычная колонка |
|---|---|---|
| `\N` | NULL | ошибка |
| *(пусто)*, без кавычек | NULL | **значение по умолчанию** (`0`, `''`), без ошибки |
| `""` | `''` | `''` |

У новых серверов (23+) включено угадывание шапки CSV: если первая запись
похожа на имена колонок, она будет молча пропущена. Отключается
`settings input_format_csv_detect_header = 0`; на 22.12 этой настройки нет, и
её передача — ошибка.

## Поток Oracle

### Что отдаёт ora_csv_out

У Oracle нет серверного текстового потока. `ora_csv_out` принимает `select`
целиком, драйвер python-oracledb отдаёт ответ пачками Arrow (по `arraysize`
строк профиля соединения), а pyarrow пишет каждую пачку в CSV. Типы колонок
насос узнаёт у сервера разбором стейтмента (`parse`, без выполнения), чтобы
запросить у драйвера точные типы NUMBER; сам запрос выполняется один раз.

```sql
select
    1                                                               as id,
    cast(12.5 as number(10,2))                                      as amount,
    'tab' || chr(9) || 'new' || chr(10) || 'line \ back "q", semi;' as note,
    cast(null as varchar2(10))                                      as empty,
    to_binary_double(1) / 3                                         as ratio,
    binary_double_nan                                               as nan,
    to_date('2024-02-29 13:14:15', 'yyyy-mm-dd hh24:mi:ss')         as d,
    cast(timestamp '2024-02-29 13:14:15.123456' as timestamp(6))    as ts,
    from_tz(timestamp '2024-02-29 13:14:15', '+03:00')              as tstz_raw,
    to_char(from_tz(timestamp '2024-02-29 13:14:15', '+03:00'),
            'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')                     as tstz,
    rawtohex(hextoraw('00FF'))                                      as bin,
    cast(123 as number)                                             as n_int,
    to_char(cast(1/7 as number), 'TM9')                             as n_free,
    cast('ab' as char(5))                                           as c5,
    true                                                            as flag
from dual
```

Поток:

```text
1,12.50,"tab	new
line \ back ""q"", semi;",,0.3333333333333333,nan,2024-02-29 13:14:15,2024-02-29 13:14:15.123456,2024-02-29 13:14:15.000000000,"2024-02-29 13:14:15.000000+03:00","00FF",123,".1428571428571428571428571428571428571429","ab   ",true
```

Шапки нет, разделитель — запятая, запись заканчивается переводом строки. **Любая
строка всегда в двойных кавычках**, числа, даты и булевы значения — без.

| # | Колонка | Тип Oracle | Текст поля | Что это |
|---|---|---|---|---|
| 1 | id | NUMBER | `1` | целое |
| 2 | amount | NUMBER(10,2) | `12.50` | со своим масштабом, без экспоненты |
| 3 | note | VARCHAR2 | `"tab⇥new↵line \ back ""q"", semi;"` | в кавычках; табуляция, перевод строки, `\r` и `\` — настоящие байты, кавычка удвоена |
| 4 | empty | VARCHAR2 | *(пусто)* | NULL — пустое поле; пустая строка Oracle и есть NULL, `""` не бывает |
| 5 | ratio | BINARY_DOUBLE | `0.3333333333333333` | кратчайший точный текст |
| 6 | nan | BINARY_DOUBLE | `nan` | строчными: `nan`, `inf`, `-inf` |
| 7 | d | DATE | `2024-02-29 13:14:15` | у DATE всегда есть время |
| 8 | ts | TIMESTAMP(6) | `2024-02-29 13:14:15.123456` | знаков столько, сколько у типа |
| 9 | tstz_raw | TIMESTAMP WITH TIME ZONE | `2024-02-29 13:14:15.000000000` | **смещение потеряно**: настенное время, девять знаков |
| 10 | tstz | то же через `to_char` | `"2024-02-29 13:14:15.000000+03:00"` | правильная запись: строка со смещением |
| 11 | bin | RAW через `rawtohex` | `"00FF"` | hex заглавными, в кавычках — это строка |
| 12 | n_int | NUMBER без точности | `123` | целые едут как есть |
| 13 | n_free | NUMBER без точности через `to_char(..., 'TM9')` | `".1428571428571428571428571428571428571429"` | строка, **без ведущего нуля** |
| 14 | c5 | CHAR(5) | `"ab   "` | с пробелами дополнения |
| 15 | flag | BOOLEAN (23ai) | `true` | без кавычек |

Этот поток — ровно то, что читает `copy ... from stdin (format csv)`
PostgreSQL без опций и `format CSV` ClickHouse.

### Что выгрузка не пропустит

Часть типов драйвер не отдаёт в Arrow, и `ora_csv_out` падает. Такие типы
приводятся в самом `SELECT`; справа — что при этом окажется в потоке:

| Тип Oracle | Ошибка без приведения | Что писать в SELECT | В потоке |
|---|---|---|---|
| NUMBER без точности с дробью | DPY-4042 при чтении | `to_char(col, 'TM9')` | `".1428571428571428571428571428571428571429"` |
| NUMBER больше 38 знаков | DPY-4042 при чтении | `to_char(col, 'TM9')` | `"9.99E+125"` |
| RAW | binary needs rawtohex, при чтении | `rawtohex(col)` | `"00FF"` |
| BLOB | binary needs rawtohex, при чтении | куски `rawtohex(dbms_lob.substr(...))`, см. ниже | `"00FF..."` |
| INTERVAL YEAR TO MONTH | cannot be fetched as arrow, до выполнения | `to_char(col)` | `"-01-02"` |
| INTERVAL DAY TO SECOND | cannot be fetched as arrow, до выполнения | `to_char(col)` или число секунд | `"+03 04:05:06.000000"` |
| XMLTYPE | cannot be fetched as arrow, до выполнения | `xmlserialize(document col as clob)` | `"<a b=""1""/>"` |
| JSON (21c+) | cannot be fetched as arrow, до выполнения | `json_serialize(col returning clob)` | `"{""a"":1}"` |
| VECTOR (23ai) | cannot be fetched as arrow, до выполнения | `from_vector(col)` | `"[1.05E+002,1.5E+000]"` |
| ROWID, UROWID | cannot be fetched as arrow, до выполнения | `rowidtochar(col)` | `"AAAR..."` |
| TIMESTAMP WITH TIME ZONE с именем пояса | DPY-3022 | `to_char(col, '... tzh:tzm')` | `"2024-02-29 13:14:15+03:00"` |
| DATE до нашей эры | year out of range | `to_char(col, 'syyyy-mm-dd hh24:mi:ss')` | `"-4000-01-01"` |

Любое значение, прошедшее через `to_char`, становится строкой и едет в
кавычках. NUMBER без точности с целыми значениями и `NUMBER(p, -s)` едут без
приведения. Результат функций над NUMBER (`round`, `trunc`, арифметика) —
тоже NUMBER без точности: дробный результат надо обернуть в
`cast(... as number(p, s))`. Ошибка «cannot be fetched as arrow» приходит
сразу, по описанию стейтмента, запрос при этом не выполняется.

`to_char(col, 'TM9')` зависит от `NLS_NUMERIC_CHARACTERS` сессии. На стенде
разделитель — точка; если у сервера другая территория, надёжнее
`to_char(col, 'TM9', 'NLS_NUMERIC_CHARACTERS=''.,''')`. `TM9` пишет число
без ведущего нуля (`.1428`) и переходит на экспоненту у очень больших и
малых значений (`1E-130`).

### Что выгрузка пропустит, но потеряет

- **TIMESTAMP WITH TIME ZONE** и **WITH LOCAL TIME ZONE** — смещение
  выбрасывается: `from_tz(timestamp '2024-02-29 13:14:15', '+03:00')` едет как
  `2024-02-29 13:14:15.000000000`. Правильно:
  `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')`, для локального пояса —
  то же поверх `cast(col as timestamp with time zone)`.
- **TIMESTAMP(9)** — в тексте девять знаков, но драйвер уже обрезал до
  микросекунд: `.123456789` едет как `.123456000`. Нужны наносекунды —
  `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff9')`.
- **FLOAT(p)** — это NUMBER, но драйвер отдаёт его как double:
  `cast(1/3 as float(126))` едет как `0.3333333333333333` вместо 38 знаков.
  Точно — `to_char(col, 'TM9')`: `".33333333333333333333333333333333333333"`.
- **Однобайтовая кодировка базы.** Oracle 12.2 стенда живёт в `WE8DEC`:
  кириллица и иероглифы в VARCHAR2 и CLOB уже в базе хранятся как `¿`, и
  поток повторит это. Юникод в такой базе живёт только в NVARCHAR2 и NCLOB, и
  они выгружаются правильно.
- **`json()` до 21c** — типа JSON нет, а вызов `json('...')` молча даёт NULL.

### BLOB больше 2000 байт

`rawtohex` принимает только RAW, а RAW в SQL ограничен 2000 байтами. BLOB
выгружается кусками, склеенными в CLOB:

```sql
to_clob(rawtohex(dbms_lob.substr(b, 2000, 1)))
  || to_clob(rawtohex(dbms_lob.substr(b, 2000, 2001)))
  || to_clob(rawtohex(dbms_lob.substr(b, 2000, 4001)))
```

Кусков должно хватать на самый длинный BLOB: хвост за последним куском
пропадёт без ошибки.

### Что принимает ora_csv_in

`ora_csv_in(sql, chunk_bytes)` читает CSV без шапки и пишет его пачками
`executemany` одной транзакцией в стейтмент INSERT, который написан в
вызове: bind'ы `:1..:n` идут в порядке полей CSV. Каждое поле уходит
строкой как есть, `\N` — NULL для любого типа; приводит значения сам
стейтмент, поэтому числа, даты и RAW пишутся с явным форматом:

```sql
insert into hr.sink (id, email, balance, created_at, note, photo) values (
    to_number(:1),
    :2,
    to_number(:3),
    to_timestamp(:4, 'yyyy-mm-dd hh24:mi:ss.ff6'),
    :5,
    hextoraw(substr(:6, 3))
)
```

Формат потока отличается от выхода `ora_csv_out` в двух местах: NULL —
`\N`, а бинарное поле — шестнадцатеричная строка с префиксом `\x`. Это
поток `copy ... to stdout (format csv, null '\N')` PostgreSQL:

```text
1,12.50,"tab	new
line \ back ""q"", semi;",\N,2024-02-29 13:14:15.123456,\x00ff
```

| Текст поля | Что писать в стейтменте | Значение |
|---|---|---|
| `\N` | любой bind | NULL |
| `12.50` | `to_number(:k)` или bind в NUMBER-колонку | Oracle приводит текст сам |
| `0.3333333333333333`, `nan` | `to_binary_double(:k)` | BINARY_DOUBLE; `nan` и `inf` Oracle читает |
| `2024-02-29 13:14:15.123456` | `to_timestamp(:k, 'yyyy-mm-dd hh24:mi:ss.ff6')` | доли секунд сохраняются |
| `\x00ff` | `hextoraw(substr(:k, 3))` | RAW, BLOB; префикс `\x` срезается |
| `"tab⇥new..."` | `:k` | строка как есть |
| *(пусто)* | `:k` | NULL: Oracle хранит пустую строку и пустой RAW как NULL |

Выход `ora_csv_out` подаётся в `ora_csv_in` только если в числовых и
временных колонках нет NULL: `ora_csv_out` пишет NULL пустым полем, а
`to_number('')` и `to_timestamp('')` дают NULL, но пустое поле числового
CSV-потока postgres — это ошибка разбора на стороне postgres, не Oracle.

## PostgreSQL <-> ClickHouse

### Стейтменты

PostgreSQL -> ClickHouse, `pg_stream_out` и `ch_stream_in`:

```sql
-- pg_stream_out
copy (
  select id, name, created_at
  from public.users
  order by id
) to stdout

-- ch_stream_in
insert into dwh.users (id, name, created_at)
format TabSeparated
```

ClickHouse -> PostgreSQL, `ch_stream_out` и `pg_stream_in`:

```sql
-- ch_stream_out
select id, name, created_at
from dwh.users
order by id
format TabSeparated

-- pg_stream_in
copy public.users (id, name, created_at) from stdin
```

### Формат

Основная пара — текстовый COPY и `TabSeparated`: как видно по образцам выше,
разделители, экранирование и `\N` у них одинаковые, и поток одного читается
другим без единого преобразования. Порядок полей — порядок колонок в списке
стейтмента; можно и `TabSeparatedWithNames`, тогда ClickHouse сопоставит поля
по именам (со стороны PostgreSQL шапку понимает только CSV с `HEADER`).

CSV тоже стыкуется, но NULL у ClickHouse — `\N`, а PostgreSQL в CSV ждёт
пустое поле. При загрузке из ClickHouse нужно
`COPY t FROM STDIN WITH (FORMAT CSV, NULL '\N')`, иначе в колонку ляжет строка
из двух символов. В обратную сторону пустое поле ClickHouse читает как NULL
только в `Nullable` колонке. И помните про кортежи: в CSV ClickHouse
раскладывает их на несколько полей.

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

У ClickHouse больше типов, и часть значений переписывается при выгрузке.
Колонка «При выгрузке» — что писать в `SELECT` для `ch_stream_out`,
«При возврате» — как собрать значение через `input()`, если оно поедет назад.

| Тип ClickHouse | В потоке как есть | Тип PostgreSQL | При выгрузке | При возврате |
|---|---|---|---|---|
| Int8..Int64, UInt8..UInt32 | `42` | smallint, integer, bigint | как есть | как есть |
| UInt64, Int128..UInt256 | `18446744073709551615` | numeric | как есть | как есть |
| Decimal32..Decimal256 | `12.5` | numeric(p, s) | как есть | как есть |
| Float32, Float64 | `0.3333333333333333`, `nan` | real, double precision | как есть | `toFloat64(f)` из `String`, см. точность |
| String | `tab\tnew` | text | как есть | как есть |
| String с нулевым байтом, FixedString | `a\0b` | bytea | `concat('\\x', hex(col))` | `unhex(substring(col, 3))` |
| LowCardinality(String) | `x` | text | как есть | как есть |
| Date, Date32 | `2024-02-29` | date | как есть | как есть |
| DateTime | `2024-02-29 13:14:15` | timestamp(0) | как есть | как есть |
| DateTime64(≤6) | `2024-02-29 13:14:15.123456` | timestamp(6) | как есть | как есть |
| DateTime64(9) | `...15.123456789` | text | `toString(col)` | `toDateTime64(col, 9, 'UTC')` |
| Enum8, Enum16 | `x` | text | как есть | как есть |
| UUID | `a1b2c3d4-...` | uuid | как есть | как есть |
| IPv4, IPv6 | `10.0.0.1` | inet | как есть | как есть |
| Bool | `true` | boolean | как есть | как есть |
| Array | `[1,2]` | jsonb | `toJSONString(col)` | `JSONExtract(col, 'Array(Int64)')` |
| Tuple с именами | `(1,'x')` | jsonb | `toJSONString(col)` | `tuple(JSONExtractInt(col, 'a'), JSONExtractString(col, 'b'))` |
| Map | `{'k':1}` | jsonb | `toJSONString(col)` | `CAST(JSONExtractKeysAndValues(col, 'Int64'), 'Map(String, Int64)')` |
| Point, Ring, Polygon | `(1,0.5)` | jsonb | `toJSONString(col)` | `JSONExtract` в `Tuple(Float64, Float64)` и массивы |
| Nullable(...) | `\N` | тот же тип | как есть | как есть |

На PostgreSQL старше 9.4 вместо `jsonb` подойдёт `text`. Кортеж и Map
собираются по частям потому, что на ClickHouse 22.12 `JSONExtract` не
возвращает ни Map, ни именованный кортеж. Нулевой байт PostgreSQL не хранит
ни в `text`, ни в `varchar`, поэтому такие строки едут через `bytea`.

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

`ora_csv_out` и `pg_stream_in`:

```sql
-- ora_csv_out
select id, amount, created_at
from sales.orders
order by id

-- pg_stream_in
copy dwh.orders (id, amount, created_at) from stdin (format csv)
```

Поток Oracle PostgreSQL читает без опций: пустое поле — NULL, строки в
кавычках, `nan`/`inf` и ISO-даты он понимает.

### Типы

| Тип Oracle | SELECT для ora_csv_out | В потоке | Тип PostgreSQL |
|---|---|---|---|
| NUMBER(p), NUMBER(p, s) | как есть | `12.50` | numeric(p, s), bigint |
| NUMBER без точности | `to_char(col, 'TM9')`, целые как есть | `".1428..."` | numeric |
| NUMBER(p, -s) | как есть | `12300` | bigint, numeric |
| FLOAT(p) | `to_char(col, 'TM9')` | `".3333...3"` | numeric |
| BINARY_FLOAT | как есть | `0.6666667`, `inf` | real |
| BINARY_DOUBLE | как есть | `0.2857142857142857`, `nan` | double precision |
| VARCHAR2, NVARCHAR2, CLOB, NCLOB | как есть | `"текст"` | text |
| CHAR(n) | как есть | `"ab   "` | char(n) |
| DATE | как есть | `2024-02-29 13:14:15` | timestamp(0) |
| TIMESTAMP(0..6) | как есть | `2024-02-29 13:14:15.123456` | timestamp(6) |
| TIMESTAMP(9) | `to_char(cast(col as timestamp(6)), 'yyyy-mm-dd hh24:mi:ss.ff6')` | `"...15.123457"` | timestamp(6) |
| TIMESTAMP WITH TIME ZONE | `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')` | `"...15.123456+03:00"` | timestamptz |
| TIMESTAMP WITH LOCAL TIME ZONE | то же поверх `cast(col as timestamp with time zone)` | `"...+03:00"` | timestamptz |
| INTERVAL YEAR TO MONTH | `to_char(col)` | `"-01-02"` | interval |
| INTERVAL DAY TO SECOND | число секунд, см. ниже | `"-1000000.5"` | interval |
| RAW(16) | `rawtohex(col)` | `"00FF...10"` (32 цифры) | uuid |
| RAW(n) | `case when col is not null then '\x' \|\| rawtohex(col) end` | `"\x00FF"` | bytea |
| BLOB | то же с кусками `dbms_lob.substr` | `"\x00FF..."` | bytea |
| CLOB IS JSON | как есть | `"{""a"": 1}"` | jsonb (json до 9.4, text до 9.2) |
| JSON (21c+) | `json_serialize(col returning clob)` | `"{""a"":1}"` | jsonb |
| XMLTYPE | `xmlserialize(document col as clob)` | `"<r id=""1""/>"` | xml |
| BOOLEAN (23ai) | как есть | `true` | boolean |
| VECTOR (23ai) | `translate(from_vector(col), '[]', '{}')` | `"{1.05E+002,1.5E+000}"` | real[] |

### Особенности

**INTERVAL DAY TO SECOND со знаком.** `to_char` пишет знак один раз на всё
значение: `"-000000011 13:46:40.500000000"`. Oracle имеет в виду минус
(11 суток и 13 часов), а PostgreSQL относит минус только к суткам и
получает `-11 days +13:46:40.5` — другое значение. Поэтому интервал едет
числом секунд, а голое число PostgreSQL принимает в `interval` как секунды:

```sql
to_char(
    extract(day    from iv) * 86400
  + extract(hour   from iv) * 3600
  + extract(minute from iv) * 60
  + extract(second from iv),
  'TM9'
) as iv
```

В потоке `"-1000000.5"`, в PostgreSQL — `-277:46:40.5`, как и в Oracle.

**Префикс `\x` и NULL.** В Oracle `'\x' || NULL` — это `'\x'`, и в потоке
будет `"\x"` вместо пустого поля: PostgreSQL положит пустой bytea вместо
NULL. Префикс ставится только непустому значению через `case`.

**Округление наносекунд.** PostgreSQL округляет лишние знаки времени к
чётному, Oracle при `cast(... as timestamp(6))` — вверх: `.123468500` у
PostgreSQL станет `.123468`, у Oracle — `.123469`. Поэтому микросекунды
считает Oracle.

**DATE в колонку date.** DATE Oracle всегда со временем, и колонка `date`
PostgreSQL молча его отбрасывает: `2024-02-29 13:14:15` станет `2024-02-29`.
Если время есть, приёмник — `timestamp(0)`.

**Лишние знаки numeric** PostgreSQL округляет: `-0.14286` в `numeric(18, 4)`
станет `-0.1429`.

**Greenplum 6 и очень малые double.** Число `1.942e-297` Greenplum 6
разбирает с ошибкой в младшем бите (`1.9419999999999998e-297`), хотя
PostgreSQL 9.4 того же поколения и Greenplum 7 читают его точно. Если такие
значения важны до бита, на Greenplum 6 их везёт `pg_arrow_in` с
`exact_floats = true` (hex-запись float, см. раздел Arrow): `numeric` не
поможет, преобразование `numeric -> float8` идёт через тот же разбор
текста.

## Oracle -> ClickHouse

### Стейтменты

`ora_csv_out` и `ch_stream_in`. `input()` нужен почти всегда: float, двоичные
данные и векторы приводятся только в нём.

```sql
-- ora_csv_out
select
    id,
    amount,
    created_at,
    ratio,
    rawtohex(payload) as payload
from sales.orders

-- ch_stream_in
insert into dwh.orders (id, amount, created_at, ratio, payload)
select
    id,
    amount,
    created_at,
    toFloat64(ratio) as ratio,
    unhex(payload)   as payload
from input('
    id         Int64,
    amount     Decimal(18, 2),
    created_at DateTime64(6, ''UTC''),
    ratio      String,
    payload    Nullable(String)
')
settings precise_float_parsing = 1
format CSV
```

Каждая колонка, где в Oracle бывает NULL, объявляется `Nullable` и в
таблице, и в `input()`, кроме массивов: `Nullable(Array)` не бывает.

### Типы

| Тип Oracle | SELECT для ora_csv_out | В потоке | Тип ClickHouse | В input() |
|---|---|---|---|---|
| NUMBER(p, s) | как есть | `12.50` | Decimal(p, s) | как есть |
| NUMBER без точности | `to_char(col, 'TM9')` | `".1428..."` | Decimal(76, 40) | как есть |
| NUMBER(p, -s), целый NUMBER | как есть | `12300` | Int64 | как есть |
| FLOAT(p) | `to_char(col, 'TM9')` | `".3333...3"` | Decimal(76, 40) | как есть |
| BINARY_FLOAT | как есть | `4.4999997e+30` | Float32 | `toFloat32(toFloat64(col))` из `String` |
| BINARY_DOUBLE | как есть | `0.2857142857142857` | Float64 | `toFloat64(col)` из `String` |
| VARCHAR2, NVARCHAR2, CHAR, CLOB, NCLOB | как есть | `"текст"` | String | как есть |
| DATE | как есть | `2024-02-29 13:14:15` | DateTime('UTC') | как есть |
| DATE как дата | `to_char(col, 'yyyy-mm-dd')` | `"2024-02-29"` | Date32 | как есть |
| TIMESTAMP(0..6) | как есть | `2024-02-29 13:14:15.123456` | DateTime64(6, 'UTC') | как есть |
| TIMESTAMP(9) | `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff9')` | `"...15.123456789"` | DateTime64(9, 'UTC') | как есть |
| TIMESTAMP WITH [LOCAL] TIME ZONE | `to_char(col, '...ff6tzh:tzm')` | `"...+03:00"` | DateTime64(6, 'UTC') | как есть, смещение учитывается |
| INTERVAL YEAR TO MONTH | `extract(year ...) * 12 + extract(month ...)` | `-986` | Int32 (месяцы) | как есть |
| INTERVAL DAY TO SECOND | число секунд, как для PostgreSQL | `"-1000000.5"` | Decimal(18, 6) | как есть |
| RAW(16) | `rawtohex(col)` | `"00FF...10"` | UUID | как есть |
| RAW(n), BLOB | `rawtohex(col)` / куски | `"00FF..."` | String | `unhex(col)` |
| CLOB IS JSON, JSON | как есть / `json_serialize(...)` | `"{""a"":1}"` | String | как есть |
| XMLTYPE | `xmlserialize(document col as clob)` | `"<r/>"` | String | как есть |
| BOOLEAN (23ai) | как есть | `true` | Bool | как есть |
| VECTOR (23ai) | `from_vector(col)` | `"[1.05E+002,1.5E+000]"` | Array(Float32) | `CAST(JSONExtract(col, 'Array(Float64)'), 'Array(Float32)')` |

### Особенности

У ClickHouse все эти ловушки тихие: ошибки нет, значение другое.

**NULL в обычной колонке.** Пустое поле потока в колонке без `Nullable`
становится `0` у чисел и `''` у строк.

**Лишние знаки Decimal отбрасываются**, а не округляются: `-0.14286` в
`Decimal(18, 4)` — это `-0.1428` (PostgreSQL даст `-0.1429`). Если масштаб
приёмника меньше, округлите в Oracle: `cast(round(col, 4) as number(18, 4))`
— сам `round` возвращает NUMBER без точности.

**Экспонента в Decimal** обращает малое число в ноль: `"1E-130"` из
`to_char(..., 'TM9')` в `Decimal(38, 10)` становится `0`.

**Float32 из текста с экспонентой** разбирается неточно на всех версиях:
`1.05E+002` превращается в `104.99999` и при записи прямо в колонку, и (на
22.12) через `toFloat32`. Точно — через `Float64`:
`toFloat32(toFloat64(col))`. По той же причине VECTOR, который `from_vector`
пишет с экспонентой, разбирается `JSONExtract` в `Float64`.

**Даты вне диапазона.** `DateTime64` держит 1900–2299: `0001-01-01` и
`9999-12-31 23:59:59` молча становятся `1900-01-01 00:00:00` и
`2299-12-31 23:59:59`. Настройка `date_time_overflow_behavior` (её нет на
22.12) на вставку из CSV не действует. `DateTime` (32 бита) держит 1970–2106,
и 2200 год на 22–25 заворачивается в произвольную дату
`2063-11-24 17:31:44`, а на 26.7 зажимается в `2106-02-07 06:28:15`. Такие
даты храните строкой (`to_char` в Oracle, `String` в ClickHouse).

**Date32 не читает время.** `2024-02-29 00:00:00` в `Date32` — ошибка
(Code: 117); нужна `to_char(col, 'yyyy-mm-dd')`.

**Булево значение в кавычках** `Bool` не читает. BOOLEAN 23ai едет без
кавычек и проходит; строковое `"true"` из `to_char` — отказ.

## Поток Arrow

Arrow IPC — общий двоичный формат между концами, у которых текстовые форматы
не стыкуются или стыкуются с потерями: значения едут своими типами, без
перевода в текст и обратно. ClickHouse читает и пишет его сам: `ch_arrow_out`
дописывает к запросу `FORMAT ArrowStream` средствами драйвера, `ch_arrow_in`
ждёт `INSERT ... FORMAT ArrowStream` (это те же `ch_stream_*`, только с
выбранным форматом, чтобы LLM было проще ориентироваться). У Oracle поток
дают `ora_arrow_out` и `ora_arrow_in`: драйвер python-oracledb отдаёт и
принимает пачки Arrow напрямую, Python значений не видит. Поток — это схема,
затем пачки записей (у Oracle — по `arraysize` строк), затем конец потока;
байты между узлами идут как есть.

Насосы Oracle объявляют порты `ArrowOutbound` и `ArrowInbound` из
`boba.toolkit.arrow`: это сырые порты, которые сами понимают поток IPC, и
тело получает пачки записей, а не байты. На проводе они неотличимы от
`RawOutbound`/`RawInbound`, поэтому стыкуются с ClickHouse, который пишет и
читает байты сам.

### Что отдаёт ora_arrow_out

```sql
select
    1                                                            as id,
    cast(12.5 as number(10,2))                                   as amount,
    cast(123 as number)                                          as n_int,
    'tab' || chr(9) || 'x'                                       as note,
    cast(null as varchar2(5))                                    as empty,
    to_binary_double(1) / 3                                      as ratio,
    to_date('2024-02-29 13:14:15', 'yyyy-mm-dd hh24:mi:ss')      as d,
    cast(timestamp '2024-02-29 13:14:15.123456' as timestamp(6)) as ts,
    hextoraw('00FF10')                                           as bin,
    true                                                         as flag
from dual
```

Поток двоичный (1328 байт на одну строку), поэтому показана его схема и
значения первой записи, как их читает pyarrow:

| Колонка | Тип Oracle | Тип в схеме Arrow | Значение |
|---|---|---|---|
| ID | NUMBER | `decimal128(38, 0)` | `Decimal('1')` |
| AMOUNT | NUMBER(10,2) | `decimal128(10, 2)` | `Decimal('12.50')` |
| N_INT | NUMBER без точности | `decimal128(38, 0)` | `Decimal('123')` |
| NOTE | VARCHAR2 | `large_string` | `'tab\tx'` — настоящая табуляция, ничего не экранируется |
| EMPTY | VARCHAR2 | `large_string` | `None` — NULL это null-бит Arrow |
| RATIO | BINARY_DOUBLE | `double` | `0.3333333333333333` |
| D | DATE | `timestamp[s]` | `2024-02-29 13:14:15` |
| TS | TIMESTAMP(6) | `timestamp[us]` | `2024-02-29 13:14:15.123456` |
| BIN | RAW | `large_binary` | `b'\x00\xff\x10'` — байты как есть, без hex |
| FLAG | BOOLEAN (23ai) | `bool` | `True` |

Что здесь важно:

- **имена колонок заглавные**, как их хранит Oracle; строчные — алиас в
  кавычках: `col as "col"`;
- NUMBER без точности — `decimal128(38, 0)`, поэтому дробное значение в такой
  колонке — ошибка DPY-4042, как и в CSV: `cast(col as number(18, 6))` или
  `to_char`;
- TIMESTAMP(7..9) — `timestamp[ns]`, но драйвер обрезает до микросекунд;
  TIMESTAMP WITH TIME ZONE — `timestamp[ns]` без смещения: `sys_extract_utc(col)`;
- INTERVAL, XMLTYPE, JSON, VECTOR, ROWID в Arrow не отдаются — те же
  приведения, что для CSV (`to_char`, `xmlserialize`, `json_serialize`,
  `from_vector`, `rowidtochar`);
- FLOAT(p) едет как `double`, NUMBER(p, -s) — как `decimal128(p + s, 0)`.

### Что отдаёт ch_arrow_out

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

`FORMAT ArrowStream` дописывает инструмент; тот же поток даёт `ch_stream_out`
с `format ArrowStream` в тексте.

| Колонка | Тип ClickHouse | Тип в схеме Arrow | Что это |
|---|---|---|---|
| id | Int64 | `int64 not null` | |
| amount | Decimal(18, 2) | `decimal128(18, 2)` | |
| note | String | `string` (`binary` на 22.12 без `output_format_arrow_string_as_string`) | |
| empty | Nullable(String) | `string` | null-бит Arrow |
| ratio | Float64 | `double` | |
| d | Date | `date32[day]` | |
| dtm | DateTime | **`uint32`** | секунды с эпохи числом, не временем |
| ts | DateTime64(6, 'UTC') | `timestamp[us, tz=UTC]` | |
| bin | String с байтами | `string` | **невалидный UTF-8** в строке: читатель падает |
| flag | Bool | `bool` (`uint8` на 22.12) | |
| u | UUID | `extension<arrow.uuid>` (на 22.12 — ошибка UNKNOWN_TYPE) | |

### Oracle -> ClickHouse через Arrow

```sql
-- ora_arrow_out
select id, amount, created_at, note
from sales.orders

-- ch_arrow_in
insert into dwh.orders
settings input_format_arrow_case_insensitive_column_matching = 1
format ArrowStream
```

Преобразовывать нечего: `decimal128` ложится в `Decimal(p, s)`, `double` — в
`Float64` без потери бита, `timestamp[us]` — в `DateTime64(6)`,
`large_binary` — в `String`, `bool` — в `Bool`. Колонки сопоставляются по
именам, а Oracle отдаёт их заглавными: без
`input_format_arrow_case_insensitive_column_matching = 1` ClickHouse 22.12
отвечает `THERE_IS_NO_COLUMN`, а новые версии **молча пишут значения по
умолчанию** во все колонки. NULL в не-`Nullable` колонке — как и в CSV, `0`
или `''`.

| Тип Oracle | В схеме Arrow | Тип ClickHouse |
|---|---|---|
| NUMBER(p, s) | `decimal128(p, s)` | Decimal(p, s) |
| NUMBER целый, NUMBER(p, -s) | `decimal128(38, 0)`, `decimal128(p + s, 0)` | Int64, Decimal(38, 0) |
| BINARY_FLOAT, BINARY_DOUBLE | `float`, `double` | Float32, Float64 |
| VARCHAR2, CHAR, CLOB, NVARCHAR2, NCLOB | `large_string` | String |
| DATE | `timestamp[s]` | DateTime('UTC') |
| TIMESTAMP(0..6) | `timestamp[us]` | DateTime64(6, 'UTC') |
| TIMESTAMP(9) | `to_char(col, '... ff9')` -> `large_string` | DateTime64(9, 'UTC') |
| TIMESTAMP WITH TIME ZONE | `sys_extract_utc(col)` -> `timestamp[us]` | DateTime64(6, 'UTC') |
| INTERVAL YEAR TO MONTH | месяцы числом -> `decimal128(38, 0)` | Int32 |
| INTERVAL DAY TO SECOND | `cast(секунды as number(18, 6))` -> `decimal128(18, 6)` | Decimal(18, 6) |
| RAW, BLOB | `large_binary` | String |
| JSON, XMLTYPE, VECTOR | `json_serialize`, `xmlserialize`, `from_vector` -> `large_string` | String |
| BOOLEAN | `bool` | Bool |

### ClickHouse -> Oracle через Arrow

```sql
-- ch_arrow_out
select
    id,
    amount,
    toDateTime64(created_at, 0, 'UTC') as created_at,
    hex(payload)                       as payload,
    toUInt8(active)                    as active
from dwh.orders
settings output_format_arrow_string_as_string = 1

-- ora_arrow_in
insert into sales.orders (id, amount, created_at, payload, active)
values (:1, :2, :3, :4, :5)
```

`ora_arrow_in` вставляет каждую пачку одной командой `executemany` в
стейтмент из вызова: bind'ы `:1..:n` идут в порядке полей схемы потока,
значения драйвер берёт прямо из массивов Arrow. Что надо
привести на стороне ClickHouse:

| Тип ClickHouse | Как есть | Что писать в select | Тип Oracle |
|---|---|---|---|
| Int*, UInt* до UInt64 | `int64`, `uint64` | как есть | NUMBER(19), NUMBER(20) |
| Decimal(p, s) | `decimal128(p, s)` | как есть | NUMBER(p, s) |
| Float32, Float64 | `float`, `double` | как есть | BINARY_FLOAT, BINARY_DOUBLE |
| String, Nullable(String) | `string` | как есть | VARCHAR2, NVARCHAR2 |
| Date, Date32 | `date32` | как есть | DATE |
| DateTime | `uint32` | `toDateTime64(col, 0, 'UTC')` | DATE |
| DateTime64(6) | `timestamp[us]` | как есть | TIMESTAMP(6) |
| DateTime64(9) | `timestamp[ns]` | как есть, драйвер режет до микросекунд | TIMESTAMP(9) |
| Bool | `bool` | `toUInt8(col)` | NUMBER(1) |
| UUID | `arrow.uuid` | `hex(col)` | RAW(16) |
| String с байтами | невалидная строка | `hex(col)` | RAW(n) |

Ловушки этого пути:

- **DateTime как число.** `uint32` секунд Oracle в DATE не принимает
  (ORA-00932); `toDateTime64(col, 0, 'UTC')` даёт настоящий timestamp.
- **UUID и двоичные строки.** `arrow.uuid` Oracle не знает, а String с
  произвольными байтами уходит как строка Arrow с невалидным UTF-8, и
  читатель падает. Оба — через `hex()` в RAW.
- **LOB-колонки последними.** Oracle не принимает длинный bind (строка
  длиннее 4000 байт) после LOB-колонки в одном insert (ORA-24816), а порядок
  bind'ов — порядок полей схемы. CLOB, BLOB, JSON и XMLTYPE ставьте в конец
  списка `select`.
- **Юникод в однобайтовой базе.** Bind строки идёт в кодировке базы: в базе
  `WE8DEC` юникод не доедет даже до NVARCHAR2 (станет `¿`), тогда как CSV-путь
  через `ora_csv_in` его сохраняет.
- **UUID на 22.12** в Arrow не выгружается вовсе (UNKNOWN_TYPE): только `hex`.

### Oracle -> PostgreSQL через Arrow

```sql
-- ora_arrow_out
select
    id                                          as "id",
    amount                                      as "amount",
    sys_extract_utc(created_at)                 as "created_at",
    case when payload is not null
         then '\\x' || rawtohex(payload) end     as "payload",
    note                                        as "note"
from sales.orders

-- pg_arrow_in
copy dwh.orders (id, amount, created_at, payload, note) from stdin (format csv)
```

Колонки в `copy` перечисляются в порядке полей потока; их имена в схеме
Arrow роли не играют, алиасы нужны только читателю. Дальше всё
ложится через CSV сервера, поэтому двоичные типы едут hex-текстом с
префиксом `\\x`, а NULL в RAW/BLOB надо оставить NULL явно через `case`,
иначе `'\\x' || null` даст пустой bytea. CLOB, BLOB, JSON и XMLTYPE — в
конец списка `select`.

| Тип Oracle | Что писать в select | Тип PostgreSQL |
|---|---|---|
| NUMBER(p, s) | как есть | numeric(p, s) |
| NUMBER целый, NUMBER(p, -s) | как есть | bigint, numeric(38) |
| BINARY_FLOAT, BINARY_DOUBLE | как есть | real, double precision |
| VARCHAR2, NVARCHAR2, CHAR, CLOB | как есть | text, char(n) |
| DATE, TIMESTAMP(0..6) | как есть | timestamp(0), timestamp(6) |
| TIMESTAMP(9) | `to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff9')` | text; в timestamp(6) сервер округлит |
| TIMESTAMP WITH TIME ZONE | `sys_extract_utc(col)` | timestamp(6) в UTC |
| INTERVAL YEAR TO MONTH | месяцы числом | integer |
| INTERVAL DAY TO SECOND | `cast(секунды as number(18, 6))` | numeric(18, 6) |
| RAW, BLOB | `'\\x' \|\| rawtohex(col)` | bytea |
| BOOLEAN (23ai) | как есть | boolean |
| JSON, XMLTYPE, VECTOR | `json_serialize`, `xmlserialize`, `from_vector` | text, jsonb, xml |

На Greenplum 6 `double` из потока ложится с ошибкой в младшем бите у части
значений; `exact_floats = true` у `pg_arrow_in` везёт float и double
hex-записью и кладёт их бит в бит на любом сервере (см. «Что принимает
pg_arrow_in»).

### Что отдаёт pg_arrow_out

У PostgreSQL серверного потока Arrow нет, и `pg_arrow_out` собирает его из
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

Строка CSV обязана уместиться в один блок читателя: блок равен `chunk_bytes`,
но не меньше 1 MiB. Строка шире (большой `text`, `bytea`, `jsonb`)
отвергается с ошибкой `a row must fit into one block, raise chunk_bytes`;
`chunk_bytes` до 64 MiB решает.

### Что принимает pg_arrow_in

`pg_arrow_in(sql, chunk_bytes, exact_floats)` пишет каждую пачку писателем
CSV pyarrow в C, и блок уходит в стейтмент из вызова —
`copy dwh.orders (id, amount, note) from stdin (format csv)`; колонки в нём
перечисляются в порядке полей потока, шапки в теле нет (`HEADER` не
указывать), значения разбирает сервер по типу колонки. Одна транзакция.

`exact_floats = true` везёт колонки `float` и `double` шестнадцатеричной
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
  `pg_arrow_in` отвергает.
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

### Скорость Arrow из PostgreSQL

Сам `copy ... to stdout (format csv)` отдаёт около 500 тысяч строк в секунду
на узкой таблице, читатель CSV pyarrow разбирает 5 миллионов, поэтому
выгрузка упирается в сервер: около 400 тысяч строк в секунду. Загрузка —
около 880 тысяч (писатель CSV pyarrow и `copy ... from stdin`). Цепочка
pg -> pg целиком — около 300 тысяч на узкой таблице и 160 тысяч на широкой
с массивами, uuid, json и bytea.

## Скорость

Нагрузочный тест гонит миллион строк широкой таблицы (NUMBER, BINARY_DOUBLE,
строки с переводом строки и NULL, DATE, TIMESTAMP, TIMESTAMP WITH TIME ZONE
через `to_char`, RAW(16)) — это 219 МБ CSV. Насосы работают одновременно
через трубу ОС, `chunk_bytes` 256 КиБ, `arraysize` 2000.

| Цепочка | Время | Строк в секунду | Прирост пика памяти |
|---|---|---|---|
| Oracle 23 -> PostgreSQL 19 | 5.6 с | 180 тыс. | 18 МиБ |
| Oracle 23 -> ClickHouse 26.7 | 5.5 с | 183 тыс. | 0 МиБ |
| Oracle 12.2 -> PostgreSQL 19 | 4.6 с | 217 тыс. | 1 МиБ |
| Oracle 12.2 -> ClickHouse 26.7 | 4.8 с | 207 тыс. | 0 МиБ |

Память не растёт с объёмом: в процессе живёт одна пачка Arrow и одна
порция трубы, остальное уже у приёмника. Строки и агрегаты (суммы Decimal,
число NULL, длины строк, максимум времени) совпадают с источником.

## Где это проверяется

Пакет `packages/testing/boba-pump-stand` держит помощников стенда (в том
числе трубу ОС, через которую два насоса работают одновременно) и тесты:

- `test_pg_ch_pump.py` — PostgreSQL <-> ClickHouse на всей матрице версий и
  триста тысяч строк на новейшей паре;
- `test_ora_pump.py` — Oracle -> PostgreSQL и Oracle -> ClickHouse на всей
  матрице: таблица Oracle со всеми семействами типов, сверка поколоночно;
- `test_ora_pump_traps.py` — каждая ловушка этого документа: неправильный
  стейтмент, его поток и результат, и правильный вариант;
- `test_ora_pump_load.py` — миллион строк из Oracle в PostgreSQL и
  ClickHouse (маркер `load`);
- `test_ora_arrow.py` — Arrow IPC: Oracle -> ClickHouse, ClickHouse -> Oracle,
  Oracle -> PostgreSQL и круг Oracle -> Oracle на всей матрице, плюс ловушки
  Arrow-пути;
- `test_ch_arrow.py` — ClickHouse -> ClickHouse, PostgreSQL и Oracle потоком
  Arrow на всей матрице версий: каждый тип ClickHouse, включая Int128,
  UInt256, Enum, IPv6, Map и Nested, едет как есть или текстом;
- `test_arrow_ports.py` — Arrow-порты toolkit над трубой ОС без баз;
- `test_pg_arrow.py` — PostgreSQL -> PostgreSQL, ClickHouse и Oracle потоком
  Arrow на всей матрице версий, обратные пути и ловушки;
- `test_ch_pg_stream.py`, `test_ora_ch_stream.py` — короткие цепочки.

Запускаются из `compose/chainlit` с окружением из `launch.json` и маркером
`integration`. Стенды общие: тесты создают свои схемы и базы и сносят их по
завершении, пробы вне тестов должны делать то же самое.
