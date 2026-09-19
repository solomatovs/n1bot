create extension if not exists pg_trgm;
create extension if not exists vector;
create extension if not exists btree_gin;

create schema if not exists ix;

-- ============================================================================
-- Принципы проектирования
--
-- Система состоит из ядра (core) и поверхностей (surface).
--
-- Ядро: node, tree, edge, node_pagerank. Нода это то, что можно адресовать
-- в источнике: таблица в базе, колонка в таблице, страница в Confluence.
-- node.address это прямое указание, где искать объект, и по этому адресу до
-- объекта всегда можно достучаться. Ядро единственный источник связей между
-- объектами: принадлежность лежит в tree, смысловые связи в edge, и только по
-- ним строятся обход дерева, диаграммы и ранжирование.
--
-- Поверхность: одна таблица на вид ноды, node.kind буквально является именем
-- этой таблицы: pg_table, pg_column, pg_constraint, confluence_page. Поверхность
-- хранит атрибуты, раскрывающие адрес из node.address в деталях: размер таблицы,
-- владельца, комментарий, а также быстрый способ получить адрес внутри
-- источника (database_name, schema_name, table_name, column_name). Это
-- информационная часть о нодах и база для индексатора. По поверхности нельзя
-- строить запросы, описывающие связи, даже если имена в ней позволяют
-- склеить таблицу с колонкой: связи только в ядре. Оригиналы документов
-- поверхность не хранит: ни тело страницы, ни файл, ни DDL. Оригинал LLM
-- читает в источнике по node.address; адрес живёт только там, и поверхность
-- не дублирует его ни в каком виде (ни url, ни ссылка на скачивание), только
-- идентификаторы объекта как атрибуты. Текст, извлечённый индексатором,
-- живёт только в поисковых таблицах как content аспекта.
--
-- Отношение ядра и поверхности: node_id единственное, что их связывает.
-- Поверхность ссылается на ядро полем node_id и ничем больше; поверхности друг
-- на друга не ссылаются. Загрузчик пишет ноду и строку поверхности в одной
-- транзакции. Новая поверхность это новая таблица со связью к node_id и новое
-- значение node_kind_e с описанием в node_kind, больше ничего в ядре не меняется.
-- ============================================================================

-- Вид node: enum ix.node_kind_e, значение это имя таблицы поверхности, у которой
-- лежат атрибуты node. Enum, а не числовой словарь, потому что на значения
-- ссылаются предикаты частичных индексов: enum хранится в предикате ссылкой
-- на значение, и переименование через alter type rename value обновляет все
-- предикаты само. Значения только добавляются (alter type add value if not
-- exists) и переименовываются, удалить значение enum нельзя. Описание каждого
-- значения обязательно: таблица ix.node_kind с ключом enum, на неё ссылаются
-- node и поисковые таблицы, значение без описания отвергается внешним ключом.
do $$ begin
    create type ix.node_kind_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.node_kind_e add value if not exists 'pg_database';
alter type ix.node_kind_e add value if not exists 'pg_schema';
alter type ix.node_kind_e add value if not exists 'pg_table';
alter type ix.node_kind_e add value if not exists 'pg_column';
alter type ix.node_kind_e add value if not exists 'pg_view';
alter type ix.node_kind_e add value if not exists 'pg_index';
alter type ix.node_kind_e add value if not exists 'pg_sequence';
alter type ix.node_kind_e add value if not exists 'pg_routine';
alter type ix.node_kind_e add value if not exists 'pg_constraint';
alter type ix.node_kind_e add value if not exists 'confluence_space';
alter type ix.node_kind_e add value if not exists 'confluence_page';
alter type ix.node_kind_e add value if not exists 'confluence_attachment';
alter type ix.node_kind_e add value if not exists 'confluence_comment';

comment on type ix.node_kind_e is
    'Вид node: имя таблицы поверхности, в которой лежат его атрибуты. Значения только добавляются (alter type add value if not exists) или переименовываются; предикаты индексов следуют за переименованием.';

create table if not exists ix.node_kind (
    kind         ix.node_kind_e primary key,
    description  varchar        not null
);

insert into ix.node_kind (kind, description) values
    ('pg_database',           'database of a PostgreSQL source; root of its tree'),
    ('pg_schema',             'schema of a PostgreSQL database'),
    ('pg_table',              'table, including partitioned tables and partitions'),
    ('pg_column',             'column of a table, view or materialized view'),
    ('pg_view',               'view or materialized view'),
    ('pg_index',              'index of a table'),
    ('pg_sequence',           'sequence'),
    ('pg_routine',            'function, procedure, aggregate or window function; overloads are separate nodes'),
    ('pg_constraint',         'table constraint; a foreign key is the source of a references edge'),
    ('confluence_space',      'Confluence space; root of its tree'),
    ('confluence_page',       'Confluence page or blog post'),
    ('confluence_attachment', 'file attached to a page'),
    ('confluence_comment',    'inline or footer comment on a page')
on conflict (kind) do nothing;

-- Узел графа: любой объект, который можно адресовать в источнике. Главное поле
-- address, url строится из него детерминированно и служит уникальным ключом.
-- Роль address и url одна: адресовать объект; связи по ним не строятся.
-- address = {"scheme":"postgresql","host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}
-- url     = postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders
create table if not exists ix.node (
    id          bigserial   primary key,
    kind        ix.node_kind_e not null references ix.node_kind,
    address     jsonb       not null,
    url         varchar        not null unique,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- нода по адресу при загрузке и в API; типы значений в address фиксированы (port число):
-- select id from ix.node
-- where  address @> '{"host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}';
create index if not exists node__address__gin on ix.node using gin (address jsonb_path_ops);
create index if not exists node__kind on ix.node using btree (kind);

-- Дерево принадлежности: колонка лежит в таблице, таблица в схеме, схема в базе,
-- вложение в странице, страница в спейсе. Это единственная связь, которая есть
-- у всех источников, и единственная с формой дерева: у ноды не больше одного
-- родителя, что обеспечивает первичный ключ. У корня (база, спейс) строки нет.
-- Хранится отдельно от edge, чтобы ранжирование не исключало её каждый раз
-- и чтобы форма «один родитель» держалась ключом, а не дисциплиной загрузчика.
create table if not exists ix.tree (
    node_id    bigint primary key references ix.node on delete cascade,
    parent_id  bigint not null references ix.node on delete cascade
);

-- дети ноды: select node_id from ix.tree where parent_id = $1;
-- всё под нодой:
-- with recursive sub as (
--     select $1::bigint as id
--     union all
--     select t.node_id from ix.tree t join sub on t.parent_id = sub.id)
-- select id from sub;
-- При удалении родителя каскад убирает строки tree, но не ноды детей: поддерево
-- удаляет загрузчик этим же рекурсивным запросом.
create index if not exists tree__parent on ix.tree using btree (parent_id, node_id);

-- Вид edge: enum ix.edge_kind_e, имя читается как фраза «source <глагол> target»
-- в направлении самого edge. Описания в ix.edge_kind, ключ enum.
do $$ begin
    create type ix.edge_kind_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.edge_kind_e add value if not exists 'references';
alter type ix.edge_kind_e add value if not exists 'reads_from';
alter type ix.edge_kind_e add value if not exists 'writes_to';
alter type ix.edge_kind_e add value if not exists 'queried_with';
alter type ix.edge_kind_e add value if not exists 'shares_key_with';
alter type ix.edge_kind_e add value if not exists 'refers_to';

comment on type ix.edge_kind_e is
    'Вид edge, читается как «source <глагол> target» в направлении самого edge. Значения только добавляются или переименовываются.';

create table if not exists ix.edge_kind (
    kind         ix.edge_kind_e primary key,
    description  varchar        not null
);

insert into ix.edge_kind (kind, description) values
    ('references',      'foreign key constraint references a table'),
    ('reads_from',      'view or ETL job reads from a table'),
    ('writes_to',       'ETL job writes into a table'),
    ('queried_with',    'tables appear in the same query; written in both directions'),
    ('shares_key_with', 'tables share a key: a column matches the primary key of another table'),
    ('refers_to',       'document refers to an object: a page to a page, an attachment or a table; how (hyperlink or name in text) is in origin')
on conflict (kind) do nothing;

-- Origin edge: enum ix.origin_e, как стало известно, что связь есть. Тип свидетельства,
-- не источник данных: view в Postgres и materialized view в ClickHouse читают
-- таблицу одинаково, и у обеих связей origin declared. Origin отвечает за три
-- вещи: писатель находит свои строки по origin и своим node; у каждого origin
-- своя шкала weight; пользователю объясняется, откуда связь, а откуда именно,
-- видно по kind node на концах.
-- declared      объявлена самим источником: foreign key, зависимость view
--               от таблицы, гиперссылка на странице, зависимость задач ETL.
--               weight всегда 1
-- observed      наблюдена в поведении: таблицы в одном запросе из
--               pg_stat_statements или system.query_log. weight = логарифм
--               числа наблюдений, нормированный по прогону
-- text_match    идентификатор объекта найден буквально в тексте другого:
--               страница упоминает dm.fact_orders, тело функции упоминает
--               таблицу. weight 1, объект назван явно
-- name_rule     предположена правилом по именам и типам: колонка совпала
--               с primary key другой таблицы, копия таблицы в другом
--               источнике с теми же колонками. weight = уверенность правила
-- llm           предположена моделью: describer вывел связь из комментариев
--               и соседей, vision назвал таблицы на схеме. weight =
--               уверенность модели
do $$ begin
    create type ix.origin_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.origin_e add value if not exists 'declared';
alter type ix.origin_e add value if not exists 'observed';
alter type ix.origin_e add value if not exists 'text_match';
alter type ix.origin_e add value if not exists 'name_rule';
alter type ix.origin_e add value if not exists 'llm';

comment on type ix.origin_e is
    'Как стало известно об edge: объявлен источником, наблюдён в использовании, найден буквально в тексте, предположен правилом по именам, предположен LLM. У каждого origin своя шкала weight.';

create table if not exists ix.origin (
    origin       ix.origin_e primary key,
    description  varchar     not null
);

insert into ix.origin (origin, description) values
    ('declared',   'declared by the source itself: foreign key, view dependency, hyperlink, ETL dependency; weight 1'),
    ('observed',   'observed in usage: tables in one query from pg_stat_statements or system.query_log; weight = log of observations, normalized per run'),
    ('text_match', 'identifier of an object found literally in the text of another; weight 1'),
    ('name_rule',  'inferred by a rule on names and types; weight = rule confidence'),
    ('llm',        'inferred by a model (describer, vision); weight = model confidence')
on conflict (origin) do nothing;

-- Смысловые связи между node, explicit и implicit вместе; по ним считается ранг
-- и строятся диаграммы. Принадлежности здесь нет, она в tree.
--
-- Направление: source это объект, которому нужен target. Ограничение
-- fact_orders_customer_fkey пишется как ограничение -> customers, представление,
-- читающее orders, как представление -> orders. PageRank считает входящие рёбра
-- голосами, поэтому высокий ранг означает, что от ноды зависят многие.
--
-- Внешний ключ это отдельная нода вида pg_constraint под таблицей-владельцем
-- в tree, и ребро references идёт от неё. Так каждая связь адресуема, и пять
-- ключей между одной парой таблиц остаются пятью рёбрами. Колонки ключа это
-- атрибуты, они в поверхности ограничения.
--
-- weight: сколько edge весит для ранга, от 0 до 1. У explicit связей 1,
-- у implicit меньше, нормируется внутри своего origin. Сплошную или пунктирную
-- линию на диаграмме выбирает kind, а не weight.
--
-- origin: откуда взят edge. Одна пара node и один kind могут лежать по строке
-- на origin: (orders, customers, shares_key_with, name_rule, 0.5) и
-- (orders, customers, shares_key_with, llm, 0.8). Для ранга и диаграммы пара
-- сворачивается в одно число: 1 - (1 - 0.5) * (1 - 0.8) = 0.9, два независимых
-- мнения усиливают друг друга. Подтверждение правила это запрос: пары, у которых
-- есть и shares_key_with от name_rule, и references от declared.
--
-- Повторный прогон писателя это diff, а не перезапись: найденное сравнивается
-- с его строками (по origin и своим node: загрузчик Postgres владеет declared
-- и observed строками с source_id в его базе, правило по именам всеми
-- name_rule, describer всеми llm), новые вставляются, у изменившихся
-- обновляется weight, удаляются только исчезнувшие. Массовых delete
-- и insert нет.
create table if not exists ix.edge (
    source_id  bigint   not null references ix.node on delete cascade,
    target_id  bigint   not null references ix.node on delete cascade,
    kind       ix.edge_kind_e not null references ix.edge_kind,
    origin     ix.origin_e    not null references ix.origin,
    weight     real     not null check (weight between 0 and 1),
    primary key (source_id, target_id, kind, origin)
);

-- кто зависит от node, пара свёрнута по origin:
-- select source_id, kind, 1 - exp(sum(ln(1 - weight))) as weight
-- from   ix.edge where target_id = $1 group by source_id, kind;
-- ER-диаграмма схемы: таблицы через tree, ключи как дети таблиц вида pg_constraint,
-- целевая таблица через edge
-- with t  as (select node_id as id from ix.tree where parent_id = $schema_id),
--      fk as (select tr.node_id as fk_id, tr.parent_id as table_id
--             from   ix.tree tr
--             join   t on t.id = tr.parent_id
--             join   ix.node n on n.id = tr.node_id and n.kind = 'pg_constraint')
-- select fk.table_id, fk.fk_id, e.target_id
-- from   fk join ix.edge e on e.source_id = fk.fk_id and e.kind = 'references';
create index if not exists edge__target_source_kind
    on ix.edge using btree (target_id, source_id, kind) include (origin, weight);
-- строки одного origin для diff при повторном прогоне
create index if not exists edge__origin_source on ix.edge using btree (origin, source_id);

-- PageRank node. Считается в коде: берутся edge выбранных kind, свёрнутые
-- по origin в один weight на пару, и ноды вида таблица, представление,
-- матвью; ребро от ноды ограничения стягивается через tree в таблицу-владельца. Результат записывается одной транзакцией.
-- value это сырое значение (сумма по всему графу равна 1), percentile это место
-- ноды среди остальных от 0 до 10000, его использует поиск как буст к текстовой
-- релевантности. Если строки нет, нода в прогоне не участвовала и буст у неё нулевой.
create table if not exists ix.node_pagerank (
    node_id      bigint           primary key references ix.node on delete cascade,
    value        double precision not null,
    percentile   smallint         not null check (percentile between 0 and 10000),
    computed_at  timestamptz      not null default now()
);

-- select node_id, value from ix.node_pagerank order by value desc limit 20;
create index if not exists node_pagerank__value on ix.node_pagerank using btree (value desc);

-- ============================================================================
-- Поверхности: атрибуты нод из источника. Каждая поверхность это плоская таблица
-- с единственной связью с ядром через node_id и полными именами объекта для
-- быстрого доступа внутри источника. Связей в ней нет и по ней они не строятся.
-- ============================================================================

-- ============================================================================
-- Источник PostgreSQL. Ноды: база, схема, таблица, колонка, представление
-- (view и matview один вид, различаются атрибутом), индекс, последовательность,
-- подпрограмма (function, procedure, aggregate, window один вид), ограничение.
-- tree: база -> схема -> таблица | представление | последовательность |
-- подпрограмма; таблица -> колонка | ограничение | индекс; представление ->
-- колонка. Индекс в адресе уникален в схеме, но в дереве лежит под таблицей,
-- которой принадлежит (pg_index.indrelid).
-- edge: references от ноды ограничения к таблице, на которую ссылается ключ;
-- reads_from от представления к таблицам и представлениям, которые оно
-- читает (pg_rewrite и pg_depend). Тела представлений и подпрограмм не
-- хранятся: LLM читает их в источнике по адресу.
-- Хэш таблицы покрывает её колонки и ограничения: при смене content_hash
-- таблицы переписываются поисковые строки таблицы, колонок и ограничений.
-- У базы, схемы и последовательности хэша нет, их атрибуты сравниваются
-- напрямую.
-- ============================================================================

-- Поверхность pg_table: оригинал метаданных таблицы PostgreSQL.
-- content_hash = sha256 текста, из которого строятся аспекты: имя, путь,
-- комментарий, колонки с типами и комментариями. Версии у объектов Postgres
-- нет, поэтому хэш здесь единственный признак изменения: совпал с прошлым
-- прогоном, поисковые строки ноды не трогаем; не совпал, переписываем все.
create table if not exists ix.pg_table (
    node_id          bigint primary key references ix.node on delete cascade,
    database_name    varchar   not null,
    schema_name      varchar   not null,
    table_name       varchar   not null,
    tablespace_name  varchar   not null default '',
    owner            varchar   not null,
    comment          varchar   not null default '',
    content_hash     bytea     not null
);

-- Поверхность pg_summary: описание объекта, сгенерированное LLM (describer).
-- Describer это источник, чьи оригиналы хранятся у нас: адреса, где перечитать
-- summary, не существует. indexer_hash играет роль version: md5 снимка
-- настроек прогона (модель, системный промпт, параметры генерации); при
-- повторном прогоне строки с текущим хэшем не трогаются, с чужим
-- генерируются заново. content_hash = md5 текста, по нему поисковые строки
-- аспекта summary понимают, надо ли переиндексировать. Пишут таблицы,
-- колонки, представления, подпрограммы.
create table if not exists ix.pg_summary (
    node_id       bigint      primary key references ix.node on delete cascade,
    content       varchar     not null,
    content_hash  bytea       not null,
    indexer_hash  bytea       not null,
    created_at    timestamptz not null default now()
);

-- очередь describer'а: ноды нужных видов без summary или с чужим хэшем
-- select n.id from ix.node n left join ix.pg_summary s on s.node_id = n.id
-- where  n.kind in ('pg_table', 'pg_column', 'pg_view', 'pg_routine') and (s.node_id is null or s.indexer_hash <> $current);
create index if not exists pg_summary__indexer_hash on ix.pg_summary using btree (indexer_hash);

-- Поверхность pg_database: pg_database, владелец pg_get_userbyid(datdba),
-- кодировка pg_encoding_to_char(encoding), collate_name = datcollate,
-- комментарий shobj_description.
create table if not exists ix.pg_database (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    owner          varchar not null,
    encoding       varchar not null,
    collate_name   varchar not null,
    comment        varchar not null default ''
);

-- Поверхность pg_schema: pg_namespace, комментарий obj_description.
create table if not exists ix.pg_schema (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    schema_name    varchar not null,
    owner          varchar not null,
    comment        varchar not null default ''
);

-- Поверхность pg_column: pg_attribute таблицы или представления.
-- relation_kind = table | view | matview, чтобы по строке было видно, чья
-- колонка. default_expr из pg_attrdef через pg_get_expr, generated =
-- attgenerated ('' обычная, 's' вычисляемая). Своего content_hash нет,
-- колонку покрывает хэш таблицы.
create table if not exists ix.pg_column (
    node_id        bigint   primary key references ix.node on delete cascade,
    database_name  varchar  not null,
    schema_name    varchar  not null,
    relation_name  varchar  not null,
    relation_kind  varchar  not null,
    column_name    varchar  not null,
    ordinal        smallint not null,
    data_type      varchar  not null,
    not_null       boolean  not null,
    default_expr   varchar  not null default '',
    generated      varchar  not null default '',
    comment        varchar  not null default ''
);

-- Поверхность pg_view: pg_class с relkind v | m, view_kind = view | matview.
-- Определение (pg_get_viewdef) не хранится, но входит в content_hash вместе
-- с колонками и комментарием: изменилось определение, переписываются
-- поисковые строки представления и его колонок.
create table if not exists ix.pg_view (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    schema_name    varchar not null,
    view_name      varchar not null,
    view_kind      varchar not null,
    owner          varchar not null,
    comment        varchar not null default '',
    content_hash   bytea   not null
);

-- Поверхность pg_index: pg_index с pg_class индекса и таблицы, метод доступа
-- из pg_am. columns = выражения ключа по порядку из pg_get_indexdef по
-- колонкам, predicate = условие частичного индекса из indpred.
-- content_hash = md5(pg_get_indexdef(indexrelid)): в хэш таблицы индексы
-- не входят.
create table if not exists ix.pg_index (
    node_id        bigint    primary key references ix.node on delete cascade,
    database_name  varchar   not null,
    schema_name    varchar   not null,
    table_name     varchar   not null,
    index_name     varchar   not null,
    is_unique      boolean   not null,
    is_primary     boolean   not null,
    access_method  varchar   not null,
    columns        varchar[] not null,
    predicate      varchar   not null default '',
    content_hash   bytea     not null
);

-- Поверхность pg_sequence: pg_sequence с pg_class. owned_by = колонка-владелец
-- schema.table.column из pg_depend (deptype a или i), пусто у свободной
-- последовательности; это атрибут для чтения, связью не является.
create table if not exists ix.pg_sequence (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    schema_name    varchar not null,
    sequence_name  varchar not null,
    data_type      varchar not null,
    start_value    bigint  not null,
    increment      bigint  not null,
    owned_by       varchar not null default '',
    comment        varchar not null default ''
);

-- Поверхность pg_routine: pg_proc. routine_kind = function | procedure |
-- aggregate | window (prokind f, p, a, w). arguments из
-- pg_get_function_identity_arguments, они же в адресе (перегрузки это разные
-- ноды), result из pg_get_function_result, language из pg_language.
-- Тело (prosrc) не хранится, но входит в content_hash с сигнатурой
-- и комментарием.
create table if not exists ix.pg_routine (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    schema_name    varchar not null,
    routine_name   varchar not null,
    routine_kind   varchar not null,
    arguments      varchar not null default '',
    result         varchar not null default '',
    language       varchar not null,
    owner          varchar not null,
    comment        varchar not null default '',
    content_hash   bytea   not null
);

-- Поверхность pg_constraint: pg_constraint таблицы. constraint_type =
-- primary | unique | foreign | check | exclusion (contype p, u, f, c, x).
-- columns = колонки ограничения по порядку conkey. Для внешнего ключа
-- ref_schema_name, ref_table_name, ref_columns по confkey, on_delete и
-- on_update из confdeltype и confupdtype; это атрибуты для чтения, сама
-- связь это ребро references от этой ноды. Выражение check не хранится.
-- Своего content_hash нет, ограничение покрывает хэш таблицы.
create table if not exists ix.pg_constraint (
    node_id          bigint    primary key references ix.node on delete cascade,
    database_name    varchar   not null,
    schema_name      varchar   not null,
    table_name       varchar   not null,
    constraint_name  varchar   not null,
    constraint_type  varchar   not null,
    columns          varchar[] not null,
    ref_schema_name  varchar   not null default '',
    ref_table_name   varchar   not null default '',
    ref_columns      varchar[] not null default '{}',
    on_delete        varchar   not null default '',
    on_update        varchar   not null default '',
    is_deferrable    boolean   not null
);

-- ----------------------------------------------------------------------------
-- Аспекты объектов PostgreSQL и откуда они берутся. Аспект это текст, по
-- которому объект ищут; в поисковых таблицах pg_* ниже указано, какие аспекты
-- пишет каждая поверхность. Состав по поверхностям:
--
-- pg_table       name, path (schema.table), words, comment, columns, summary,
--                description
-- pg_column      name, path (schema.table.column), words, comment, summary,
--                description ('Column {path} {type}: {comment}')
-- pg_view        как pg_table; description строится из имени, комментария
--                и колонок, определение в текст не входит
-- pg_schema      name, words, comment, description
-- pg_database    name, words, comment, description
-- pg_index       name, path (schema.index), words, description
--                ('Index {name} on {table} ({columns}) {unique}')
-- pg_sequence    name, path (schema.sequence), words, description
-- pg_routine     name, path (schema.routine(arguments)), words, comment,
--                summary, description ('Function {name}({arguments}) returns
--                {result}: {comment}')
-- pg_constraint  name, words, description ('Foreign key {name} on {table}
--                ({columns}) references {ref_table} ({ref_columns})')
--
-- Атрибуты, по которым не ищут словами, а фильтруют или подправляют выдачу,
-- живут в поверхностях: владелец, табличное пространство, размер, оценка
-- числа строк, статистика обращений. DDL, определения представлений и тела
-- подпрограмм не хранятся и не индексируются: это оригиналы, LLM читает их
-- в источнике по адресу ноды. Значения строк не индексируются.
-- ----------------------------------------------------------------------------

-- Словарь аспектов источника PostgreSQL: какой текст объекта закодирован
-- в строке поисковой таблицы. Номера фиксированы здесь, потому что на них
-- ссылаются частичные индексы, а в условии индекса допустима только константа.
-- Ниже у каждого аспекта: откуда берётся текст для pg_table и для чего он.
-- Другие поверхности собирают те же аспекты из своих полей (attname вместо
-- relname, schema.table.column вместо schema.table); какие аспекты пишет
-- поверхность в какую поисковую таблицу, указано у самих поисковых таблиц.
--
-- description    описание, собранное индексатором из всего известного об
--                объекте, основной аспект для поиска. Для pg_fts это части
--                с весами внутри одного tsvector: A = words, B = words схемы
--                и comment, C = columns, D = summary; для pg_emb одна строка:
--                'Table {path}: {comment}. Columns: {col1} ({type}), ...'
-- comment        комментарий из источника как есть: obj_description для
--                таблицы, col_description для колонки; пишется, если не пуст
-- columns        имена колонок таблицы словами через пробел (в pg_emb через
--                запятую), чтобы таблица находилась по своим колонкам
-- summary        описание от LLM (плагин describer); пишется, если оно есть
-- name           имя объекта как есть: pg_class.relname, pg_attribute.attname;
--                для точного совпадения, подстроки и подсказки по префиксу
-- path           путь через точку, как пишет пользователь: nspname || '.' ||
--                relname, для колонки ещё || '.' || attname; для точного
--                совпадения
-- words          слова имени: name, разрезанный по CamelCase и подчёркиваниям,
--                в нижнем регистре, ё -> е; для опечаток: 'ordrs' к
--                'CustomerOrders' даёт 0.22, к 'customer orders' 0.5
do $$ begin
    create type ix.pg_aspect_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.pg_aspect_e add value if not exists 'description';
alter type ix.pg_aspect_e add value if not exists 'comment';
alter type ix.pg_aspect_e add value if not exists 'columns';
alter type ix.pg_aspect_e add value if not exists 'summary';
alter type ix.pg_aspect_e add value if not exists 'name';
alter type ix.pg_aspect_e add value if not exists 'path';
alter type ix.pg_aspect_e add value if not exists 'words';

comment on type ix.pg_aspect_e is
    'Какой текст объекта PostgreSQL закодирован в строке поисковой таблицы. Значения только добавляются или переименовываются; предикаты индексов следуют за переименованием.';

create table if not exists ix.pg_aspect (
    aspect       ix.pg_aspect_e primary key,
    description  varchar        not null
);

insert into ix.pg_aspect (aspect, description) values
    ('description', 'indexer-built description of the object from everything known about it; the main search aspect'),
    ('comment',     'comment from the source as is (obj_description, col_description); written only when not empty'),
    ('columns',     'column names of a table separated by spaces; lets a table be found by its columns'),
    ('summary',     'description generated by the LLM describer; written only when it exists'),
    ('name',        'object name as is (relname, attname); exact match and substring'),
    ('path',        'dotted path as the user types it: schema.table or schema.table.column; exact match'),
    ('words',       'name split into words by CamelCase and underscores, lower case, yo -> ye; typo-tolerant search')
on conflict (aspect) do nothing;

-- ----------------------------------------------------------------------------
-- Поисковые таблицы источника PostgreSQL: одна на вид индекса для всех
-- поверхностей pg_*, у всех трёх один ключ: node_id, kind, aspect.
-- kind это копия node.kind того же типа node_kind_e; она нужна индексам для
-- фильтра по виду без обращения к node. aspect это значение pg_aspect_e. content во всех трёх это текст аспекта, из которого построен
-- индекс: триграммам он нужен для точного расчёта похожести, полнотексту для
-- сниппета, вектору для проверки, изменился ли текст. Загрузчик пишет ноду,
-- поверхность и поисковые строки одной транзакцией.
-- ----------------------------------------------------------------------------

-- Полнотекстовый индекс: строка на аспект. Все поверхности пишут description
-- одним tsvector с весами: A = words имени, B = words схемы и comment,
-- C = columns (таблица, представление).
-- Текст каждой части нормализован в коде, tsvector собирает insert;
-- content = те же части одной строкой, для сниппета ts_headline в выдаче
-- и для сравнения при повторном прогоне.
-- insert into ix.pg_fts (node_id, kind, aspect, content, tsv) values ($1, 'pg_table', 'description', $content,
--     setweight(to_tsvector('russian', $words), 'A') ||
--     setweight(to_tsvector('russian', $schema_words || ' ' || $comment), 'B') ||
--     setweight(to_tsvector('russian', $columns), 'C'));
--
-- Summary от LLM это отдельная строка с аспектом summary, а не часть строки description:
-- у неё другой писатель (describer, а не индексатор), другой источник
-- (pg_summary), своё время появления и свой цикл пересчёта. Индексатор
-- пишет строку description при загрузке объекта, describer позже пишет строку summary:
-- insert into ix.pg_fts (node_id, kind, aspect, content, tsv)
-- values ($1, 'pg_table', 'summary', $summary, setweight(to_tsvector('russian', $summary), 'D'));
-- Поиск читает обе строки как один документ, ранг ноды это сумма рангов
-- её строк:
-- select node_id, sum(ts_rank_cd(tsv, q)) as rank
-- from   ix.pg_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
-- where  tsv @@ q
-- group by node_id order by rank desc limit 20;
create table if not exists ix.pg_fts (
    node_id    bigint   not null references ix.node on delete cascade,
    kind       ix.node_kind_e not null references ix.node_kind,
    aspect     ix.pg_aspect_e not null references ix.pg_aspect,
    content    varchar  not null,
    tsv        tsvector not null,
    primary key (node_id, kind, aspect)
);

-- конфигурация russian стеммит и русский, и английский: order/orders, заказ/заказы
-- select node_id, kind, ts_rank_cd(tsv, q) as rank
-- from   ix.pg_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
-- where  tsv @@ q
-- order by rank desc
-- limit  20;
-- Один GIN на kind и tsv (btree_gin): запрос без фильтра по виду идёт по нему
-- же, запрос с фильтром по редкому виду отбирает вид внутри индекса. Для частого
-- вида планировщик сам оставляет kind обычным фильтром после индекса, это
-- дешевле, чем читать его список из GIN.
create index if not exists pg_fts__kind_tsv__gin on ix.pg_fts using gin (kind, tsv);

-- Таблица триграмм хранит только идентификаторы, по одной строке на node_id, aspect.
-- Длинный текст сюда не кладём: триграммная похожесть на нём не работает, а btree по lower(content) падает на строках длиннее 2704 байт.
-- Все поверхности пишут name и words; path пишут таблица, колонка,
-- представление, индекс, последовательность, подпрограмма.
create table if not exists ix.pg_trgm (
    node_id    bigint   not null references ix.node on delete cascade,
    kind       ix.node_kind_e not null references ix.node_kind,
    aspect     ix.pg_aspect_e not null references ix.pg_aspect,
    content    varchar  not null,
    primary key (node_id, kind, aspect)
);

-- подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче;
-- word_similarity (<%, <<->), а не similarity (%, <->). Порог <% по умолчанию 0.6,
-- для коротких имён нужен 0.4
-- set pg_trgm.word_similarity_threshold = 0.4;
-- select node_id, kind, content
-- from   ix.pg_trgm
-- where  aspect = 'words' and 'ordrs' <% content
-- order by 'ordrs' <<-> content
-- limit  20;
create index if not exists pg_trgm__content__gist on ix.pg_trgm using gist (content gist_trgm_ops);

-- точное совпадение без учёта регистра
-- select node_id, kind from ix.pg_trgm
-- where  aspect = 'path' and lower(content) = lower('dm.fact_orders');
create index if not exists pg_trgm__aspect_lower_content on ix.pg_trgm using btree (aspect, lower(content));

-- подсказка при наборе: префикс. Обычный btree по lower(content) для префикса
-- не годится, нужен класс операторов varchar_pattern_ops. Оператор ^@ (starts
-- with) вместо like: в like подчёркивание значит «любой символ», и имя
-- fact_orders пришлось бы экранировать
-- select node_id, kind, content from ix.pg_trgm
-- where  aspect = 'name' and lower(content) ^@ lower('fact_ord')
-- limit  20;
create index if not exists pg_trgm__aspect_lower_content__prefix
    on ix.pg_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists pg_trgm__kind_aspect on ix.pg_trgm using btree (kind, aspect);

-- Векторный поиск, e5 1024: строка на аспект. Все поверхности пишут
-- description; comment пишут те, у кого он не пуст; columns таблица
-- и представление; summary таблица, колонка, представление, подпрограмма,
-- когда описание от LLM есть.
-- Текст кодируется с префиксом passage:, запрос с префиксом query:.
-- content = закодированный текст аспекта. Нужен для того, чтобы не гонять
-- embedding модель повторно, если текст не изменился
create table if not exists ix.pg_emb_e5_1024 (
    node_id       bigint        not null references ix.node on delete cascade,
    kind          ix.node_kind_e not null references ix.node_kind,
    aspect        ix.pg_aspect_e not null references ix.pg_aspect,
    content       varchar       not null,
    emb           halfvec(1024) not null,
    primary key (node_id, kind, aspect)
);

-- частичный HNSW на каждую существующую пару kind + aspect: фильтр по ним
-- попадает в свой индекс, а не усекает выдачу общего после обхода.
-- kind и aspect в предикате это значения enum, они следуют за
-- переименованием в словаре
-- select node_id, emb <=> $1::halfvec(1024) as dist
-- from   ix.pg_emb_e5_1024
-- where  kind = 'pg_table' and aspect = 'description'
-- order by dist
-- limit  20;
create index if not exists pg_emb_e5_1024__pg_database_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_database' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_database_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_database' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_schema_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_schema' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_schema_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_schema' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_table_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_table' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_table_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_table' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_table_columns__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_table' and aspect = 'columns';
create index if not exists pg_emb_e5_1024__pg_table_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_table' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_column_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_column' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_column_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_column' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_column_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_column' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_view_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_view' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_view_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_view' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_view_columns__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_view' and aspect = 'columns';
create index if not exists pg_emb_e5_1024__pg_view_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_view' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_index_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_index' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_sequence_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_sequence' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_sequence_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_sequence' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_routine_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_routine' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_routine_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_routine' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_routine_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_routine' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_constraint_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'pg_constraint' and aspect = 'description';

-- ============================================================================
-- Источник Confluence, проверено по REST API cwiki.apache.org. Ноды: спейс,
-- страница (страница и блог-запись это один вид, различаются атрибутом),
-- вложение, комментарий. Пользователи и метки нодами не являются: метки это
-- атрибут страницы. Адреса из таблицы адресов:
-- confluence_space       https://host/confluence/rest/api/space/FLINK
-- confluence_page        https://host/confluence/rest/api/content/307136992
-- confluence_attachment  https://host/confluence/download/attachments/307136992/design.pdf
-- confluence_comment     https://host/confluence/rest/api/content/127405740
--
-- tree: спейс -> страницы без ancestors (домашняя, корневые, блог-записи) ->
-- дочерние страницы (родитель = последний из ancestors) -> вложения
-- и комментарии страницы.
-- edge: refers_to от страницы к странице или вложению по гиперссылке в теле
-- (origin declared) и от страницы к таблице по идентификатору в тексте
-- (origin text_match).
-- Ссылки берутся из body.view, а не body.storage: макросы (cql, toc, children)
-- разворачиваются только там; на странице-оглавлении storage даёт 4 ссылки,
-- view 184. Внутренняя ссылка бывает по id (/spaces/KEY/pages/ID/...,
-- viewpage.action?pageId=ID) и по заголовку (/display/KEY/Title, ri:page);
-- заголовок разрешается в ноду по индексу confluence_page (space_key, title).
-- Внешние ссылки нодами не становятся и отбрасываются.
--
-- Два уровня отсечения при повторном прогоне. version из Confluence
-- отсекает скачивание: номер не изменился, объект не трогаем. content_hash
-- отсекает переиндексацию: объект скачан и разобран, хэш совпал с сохранённым,
-- поисковые строки не трогаем (версия растёт и при смене меток или
-- ограничений доступа, текст при этом прежний). Хэш не совпал: строки всех
-- аспектов ноды удаляются и пишутся заново одной транзакцией, эмбеддинги
-- считаются заново. Что хэшируется, сказано у каждой поверхности.
--
-- Оригиналы документов не хранятся: ни тело страницы, ни файлы вложений,
-- ни текст комментариев. Адрес объекта только в node.address (REST API);
-- ссылка для человека строится из него: /pages/viewpage.action?pageId=ID,
-- download вложения это и есть его адрес. Поверхность хранит идентификаторы
-- и метаданные, по адресу LLM читает оригинал сама. Текст,
-- который индексатор извлёк (тело страницы, разбор pdf и docx, OCR картинки,
-- описание картинки от LLM), живёт только в поисковых таблицах как content
-- своего аспекта: это индекс, а не копия данных.
-- ============================================================================

-- Поверхность confluence_summary: описание страницы или вложения от LLM
-- (describer), устроена как pg_summary: indexer_hash это снимок настроек
-- прогона, content_hash это хэш текста для поисковых строк аспекта summary.
create table if not exists ix.confluence_summary (
    node_id       bigint      primary key references ix.node on delete cascade,
    content       varchar     not null,
    content_hash  bytea       not null,
    indexer_hash  bytea       not null,
    created_at    timestamptz not null default now()
);

create index if not exists confluence_summary__indexer_hash on ix.confluence_summary using btree (indexer_hash);

-- Поверхность confluence_space: оригинал спейса
create table if not exists ix.confluence_space (
    node_id      bigint  primary key references ix.node on delete cascade,
    space_key    varchar not null,
    name         varchar not null,
    space_type   varchar not null,
    status       varchar not null,
    description  varchar not null default ''
);

-- Поверхность confluence_page: оригинал страницы или блог-записи.
-- content_type = page | blogpost, status = current | archived | trashed.
-- version = номер версии в Confluence (version.number): индексатор пропускает
-- страницу, если номер не изменился с прошлого прогона. created_at и author
-- из history, updated_at и last_editor из version. Тело страницы здесь не
-- хранится: индексатор берёт body.view (отрендеренный HTML с раскрытыми
-- макросами), снимает теги в коде и кладёт текст в аспект body поисковых
-- таблиц. content_hash = sha256 этого извлечённого текста вместе с заголовком
-- и метками. ancestor_titles = путь заголовков от корня спейса до родителя,
-- для хлебной крошки в выдаче.
create table if not exists ix.confluence_page (
    node_id          bigint      primary key references ix.node on delete cascade,
    space_key        varchar     not null,
    content_id       varchar     not null,
    content_type     varchar     not null,
    title            varchar     not null,
    status           varchar     not null,
    version          integer     not null,
    created_at       timestamptz not null,
    updated_at       timestamptz not null,
    author           varchar     not null,
    last_editor      varchar     not null,
    content_hash     bytea       not null,
    ancestor_titles  varchar[]   not null default '{}',
    labels           varchar[]   not null default '{}'
);

-- разрешение ссылок по заголовку в ноду: /display/KEY/Title
create index if not exists confluence_page__space_key_title on ix.confluence_page using btree (space_key, title);

-- Поверхность confluence_attachment: метаданные вложения. Сам файл не хранится: индексатор скачивает его,
-- извлекает текст (разбор pdf и docx в аспект body, OCR картинки в аспект
-- ocr, описание картинки от LLM в аспект vision) и файл отбрасывает.
create table if not exists ix.confluence_attachment (
    node_id        bigint      primary key references ix.node on delete cascade,
    space_key      varchar     not null,
    page_id        varchar     not null,
    attachment_id  varchar     not null,
    title          varchar     not null,
    media_type     varchar     not null,
    file_size      bigint      not null,
    version        integer     not null,
    created_at     timestamptz not null,
    updated_at     timestamptz not null,
    author         varchar     not null,
    content_hash   bytea       not null
);

-- Поверхность confluence_comment: метаданные комментария к странице.
-- location = inline | footer; у комментария своя версия и автор. Текст
-- берётся из body.storage, теги сняты в коде, и живёт в аспекте body;
-- content_hash = sha256 этого текста.
create table if not exists ix.confluence_comment (
    node_id     bigint      primary key references ix.node on delete cascade,
    space_key   varchar     not null,
    page_id     varchar     not null,
    comment_id  varchar     not null,
    location    varchar     not null,
    version     integer     not null,
    created_at  timestamptz not null,
    updated_at  timestamptz not null,
    author      varchar     not null,
    content_hash bytea      not null
);

-- Аспект источника Confluence: enum ix.confluence_aspect_e, описания в ix.confluence_aspect.
-- description    описание, собранное индексатором: для страницы title, метки,
--                путь заголовков и начало body; для вложения title, media_type
--                и начало text; для спейса name и description
-- body           полный текст: body страницы или text вложения. В pg-fts
--                целиком, в emb порезан на куски по окну модели, chunk_no
-- summary        описание от LLM; пишется, если оно есть
-- labels         метки страницы через пробел
-- name           заголовок страницы, имя файла вложения, имя спейса как есть
-- path           space_key || '/' || title, для точного совпадения
-- words          слова заголовка: name, разрезанный по CamelCase, дефисам
--                и подчёркиваниям, в нижнем регистре, ё -> е; для опечаток
-- ocr            текст, распознанный на картинке или скане (вложения
--                image/*, pdf без текстового слоя)
-- vision         смысл картинки, описанный LLM по изображению: что на схеме,
--                какие таблицы и системы на ней названы
do $$ begin
    create type ix.confluence_aspect_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.confluence_aspect_e add value if not exists 'description';
alter type ix.confluence_aspect_e add value if not exists 'body';
alter type ix.confluence_aspect_e add value if not exists 'summary';
alter type ix.confluence_aspect_e add value if not exists 'labels';
alter type ix.confluence_aspect_e add value if not exists 'name';
alter type ix.confluence_aspect_e add value if not exists 'path';
alter type ix.confluence_aspect_e add value if not exists 'words';
alter type ix.confluence_aspect_e add value if not exists 'ocr';
alter type ix.confluence_aspect_e add value if not exists 'vision';

comment on type ix.confluence_aspect_e is
    'Какой текст объекта Confluence закодирован в строке поисковой таблицы. Значения только добавляются или переименовываются; предикаты индексов следуют за переименованием.';

create table if not exists ix.confluence_aspect (
    aspect       ix.confluence_aspect_e primary key,
    description  varchar                not null
);

insert into ix.confluence_aspect (aspect, description) values
    ('description', 'indexer-built description: title, labels, ancestor path and the head of the text'),
    ('body',        'full text of a page or extracted text of an attachment; chunked for embeddings'),
    ('summary',     'description generated by the LLM describer; written only when it exists'),
    ('labels',      'page labels separated by spaces'),
    ('name',        'page title, attachment file name or space name as is; exact match and prefix'),
    ('path',        'space_key/title; exact match'),
    ('words',       'title split into words by CamelCase, hyphens and underscores, lower case, yo -> ye; typo-tolerant search'),
    ('ocr',         'text recognized on an image or a scanned document'),
    ('vision',      'meaning of an image described by the LLM from the picture itself')
on conflict (aspect) do nothing;

-- Полнотекстовый индекс. confluence_page пишет description одним tsvector
-- с весами: A = words заголовка, B = labels, C = body;
-- confluence_attachment: A = words имени файла, C = body, ocr и vision;
-- confluence_space: A = words имени, B = description; confluence_comment:
-- C = body. Summary от LLM это отдельная строка с аспектом summary из
-- confluence_summary, вес D, как у pg_fts.
create table if not exists ix.confluence_fts (
    node_id    bigint   not null references ix.node on delete cascade,
    kind       ix.node_kind_e         not null references ix.node_kind,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    content    varchar  not null,
    tsv        tsvector not null,
    primary key (node_id, kind, aspect)
);

create index if not exists confluence_fts__kind_tsv__gin on ix.confluence_fts using gin (kind, tsv);

-- Триграммы: спейс, страница и вложение пишут name, words; страница
-- и вложение ещё path. У комментария имени нет, он сюда не пишется
create table if not exists ix.confluence_trgm (
    node_id    bigint   not null references ix.node on delete cascade,
    kind       ix.node_kind_e         not null references ix.node_kind,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    content    varchar  not null,
    primary key (node_id, kind, aspect)
);

create index if not exists confluence_trgm__content__gist on ix.confluence_trgm using gist (content gist_trgm_ops);
create index if not exists confluence_trgm__aspect_lower_content on ix.confluence_trgm using btree (aspect, lower(content));
create index if not exists confluence_trgm__aspect_lower_content__prefix
    on ix.confluence_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists confluence_trgm__kind_aspect on ix.confluence_trgm using btree (kind, aspect);

-- Векторный поиск, e5 1024. Текст страницы длиннее окна модели (512 токенов),
-- поэтому аспект body режется на куски с перекрытием, и в ключе есть chunk_no;
-- у аспектов в один кусок chunk_no = 0. Страница пишет description и body,
-- комментарий body, вложение body или ocr и vision по типу файла,
-- спейс description, summary у любого, если есть.
create table if not exists ix.confluence_emb_e5_1024 (
    node_id    bigint        not null references ix.node on delete cascade,
    kind       ix.node_kind_e         not null references ix.node_kind,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    chunk_no   smallint      not null,
    content    varchar       not null,
    emb        halfvec(1024) not null,
    primary key (node_id, kind, aspect, chunk_no)
);

-- частичный HNSW на пару kind + aspect; аспекты description, body, ocr, vision,
-- summary
create index if not exists confluence_emb_e5_1024__page_description__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_page' and aspect = 'description';
create index if not exists confluence_emb_e5_1024__page_summary__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_page' and aspect = 'summary';
create index if not exists confluence_emb_e5_1024__page_body__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_page' and aspect = 'body';
create index if not exists confluence_emb_e5_1024__attachment_body__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_attachment' and aspect = 'body';
create index if not exists confluence_emb_e5_1024__space_description__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_space' and aspect = 'description';
create index if not exists confluence_emb_e5_1024__comment_body__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_comment' and aspect = 'body';
create index if not exists confluence_emb_e5_1024__attachment_ocr__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_attachment' and aspect = 'ocr';
create index if not exists confluence_emb_e5_1024__attachment_vision__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where kind = 'confluence_attachment' and aspect = 'vision';
