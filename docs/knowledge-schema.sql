/*
============================================================================
Система решает две задачи:
- индексация
- поиск
по разнородным источникам информации:
- postgres
- clickhouse
- oracle
- mssql
- mysql
- confluence
- other webapp
============================================================================
Принципы проектирования

В основе системы заложен property graph для установления связей
Граф раскладывается на несколько реляционных таблиц:
- core          - ядро системы, здесь храниться вся абстрактная информация об объектах и их связях
- properties    - информация о конкретной поверхности: postgres, clickhouse, confluence и прочих

В основе Property Graphs лежат по сути две таблицы
- node:         это непосредственно объекты для графа
- edge:         это непосредственно связи между node

у node и edge есть свои списки properties, который можно хранить по разному.
Один из подходов это хранение properties в виде jsonb, тогда схема хранения очень сильно упрощается.
Но тогда поиск, изменение информации становиться сложным процессом

Другим подходом является разделение properties на разные поверхности и хранение поверхностей в разных таблицах.
Здесь как раз используется этот подход.
Информация о node properties и edge properties лежит в отдельных surface таблицах
properties по сути синоним surface далее. Далее по тексту буду использовать понятие surface

Итак, система состоит из ядра (core) и поверхностей (surface)

core — центральное место хранения адресов объектов и связей между ними.
core состоит из:
    node — хранит всё, что можно адресовать в источнике: таблица, колонка, страница в Confluence.
        Все что угодно, что можно выразить в виде адреса,
        а при поисковой выдаче можно скинуть прямую ссылку на объект
    tree - хранит иерархию между node.id, когда есть четкая иерархическая последовательность.
        Такой вид моделирования графа называется Список предков (Adjacency List для Деревьев)
        имеет четкую структуру и направление node
        из плюсов это быстрая вставка, дерево перестраивается сразу же
        из минусов это долгий recursive cte для поиска
    edge - хранит взаимосвязи в виде Взвешенного графа (Weighted Graph), когда несколько node
        которые могут указывать друг на друга, иметь явное направление от (src_node_id к tgt_node_id)
        и имеют вес этой связи в виде числа и имеют тип взаимосвязи

Почему используется два представления связей графа? Это продиктовано удобством поиска и обновления
В целом мы не ограничены моделями хранения графа (они бывают разные) и можем выбрать тот, который хорошо решает задачу

Пример tree:
    - confluence tree: space -> page -> attachment
    - postgres tree: database -> schema -> table -> column
Пример edge:
    - postgres edge: foreign key
    - confluence edge: links

По tree можно построить иерархию, по edge можно построить граф связанности

surface — поверхность индексации - то, что индексируется, то по чему строиться граф
surface - это одна или несколько плоских таблиц, в которых храниться метаинформация об объекте
surface таблицы перечислены в таблице surface
Если хочется добавить новый surface, необходимо сюда добавить новую строку
Пример surface:
    - pg_table          - хранит информацию о таблицах любых заиндексированных postgres источников
    - pg_column         - хранит информацию о колонках таблиц
    - pg_constraint     - хранит информацию об индексах
    - cfl_page          - хранит информацию о заиндексированных страницах Confluence

surface таблицы желательно должны проектироваться без связи друг с другом.
Они должны ссылаться через node_id или edge_id на core, но не на друг друга
Это позволит выполнять горизонтальное масштабирование и добавлять новые поверхности без особых проблем

surface хранит атрибуты объекта в структурированном виде (те самые properties в properties graph),
    а node_id позволяет получить address.
    атрибуты хранящиеся в surface являются горячей информацией об объекте и могут быть полезны
    для получения быстрого доступа, но естественным образом могут устаревать.
    Поэтому по ним нельзя строить иерархии или связи.
    Атрибуты surface таблиц позволят поисковику быстро получать названия колонок, баз, таблиц, размеры таблиц, confluence заголовков и прочего
    Но важно четко понимать, что эта информация актуальна на момент индексации и будет периодически устаревать и обновляться индексатором.
    Любую актуальную surface информацию можно получить если перейти по адресу объекта (node.address)
    Предполагается, что llm получив адрес будет переходить в источник и выполнять получение актуальной информации

surface таблицы имеют собственные индексы, которые храняться в отдельных таблицах:
- btree     - это lower(content) индекс, который нужен для быстрой выдачи при наборе в поисковой строке: "нефтян%" -> вот такое быстро ищется через btree
- fts       - это полнотекстовый индекс, который позволяет искать совпадения внутри большого текста
- trgm      - это триграмм индекс, который используются для нечеткого поиска по частям слов. пример как если бы хотелось найти: "%Васил%" - тут btree не подходит, а триграммы отлично справяться
- vector    - это векторный индекс, который используется для семантического поиска по "смыслу". Сохраняется векторное представление оригинального документа. поисковый запрос также преобразуется в вектор и производиться поиск косинуса расстояния между векторами

Важно понимать, что индексация объекта напрямую невозможна, мы лишь можем определить
    несколько аспектов объекта, которые будем индексировать.
    К примеру что значит объект pg_column? Что именно здесь будем индексировать? Имя?
    Вот нескольк примеров того, что можно заиндексировать только у колонки:
    - comment           - у postgres есть коментарий и его можно прогнать через btree, fts, trgm, vector индексы
    - human_description - описание колонки, которое делает человек
    - llm_description   - описание колонки, которое делает llm
    - create statement  - некий sql stmp в который входит название, тип данных, ограничения
    - dot_path          - путь к таблице/колонке через точку, к примеру: {schema}.{table}.{column}
    Таких аспектов индексации можно придумать сколько угодно, они бесконечны.
Обрати вниание, что при составлении поисккового индекса мы имеем дело с пересечением четырех осей:
- node.address: адрес индексируемого объекта
- node.surface: поверхность индексируемого объекта
- aspect:       аспект индексации (что именно индексируем у объекта)
- index:        тип используемого индекса
Каждый индексатор по своему будет проходить свои объекты,
контролировать содержательность поверхностей и их индексов.
Соответственно все индексы изначально деляться по surface:
- *_btree
- *_trgm
- *_fts
- *_vector

В префикс кладется принадлежность к surface (это не полный список):
- pg    - префикс postgres surface
- ch    - префикс clickhouse surface
- cfl   - префикс confluence surface
- oc    - префикс oracle surface
- ms    - префикс mssql surface
- my    - префикс mysql surface
- odata - префикс odata surface

Обрати внимание, что из-за технических ограничений postgres,
векторный индекс приходиться разделять по разным таблицам
в зависимости от размерности вектора и модели, к примеру pg_emb_e5_1024

Проход индексатора по объектам зиждиться на нескольких важных полях:
- src_version:  версия отдаваемая источником (если такой имеется) и позволяющая выявить
    изменен ли документ до скачивания и индексации (потому что это дорогие операции)
    К примеру confluence version
- src_checksum: sha256 хэш составленный самим индексатором из оригинального документа (не преобразованного)
    в процессе индексации. Позволяет определить, изменился ли индексируемый аспект объекта
    К примеру для cfl_page это исходный HTML страницы,
    comment column (в postgres),
    описание составляемое llm или человеком
- indexer_id:       уникальное имя индексатора в системе, которое позволяет выявить документы, им проиндексированные
- indexer_scope:    sha256 хэш от параметров индексатора, с которыми он был запущен и сформировал документ
    Нужен для того, что бы понять скоуп документов которые требуют переиндексации при изменении параметров индексации
    К примеру
    - summary через llm имеет параметры: model, system_prompt
    - sql запрос на выборку pg_table. Представь что изменился запрос на выборку поверхности
        добавил фильтрацию information_schema, что бы не получать ее данные. Это значит, что scope объектов
        которые возвращались ранее измениться и нам нужно удалить те объекты, которые больше никогда не придут с новым scope
        а именн объекты information_schema.
    Изменение одного из них должно приводить к изменению hash суммы и соответственно удалению объектов старого scope
    Сначала обхекты с новым scope индексируются (insert/update) а в конце прогона запускается удаление того
    Что осталось по indexer со scope
============================================================================
*/
create extension if not exists pg_trgm;
create extension if not exists vector;
create extension if not exists btree_gin;

create schema if not exists ix;
do $$ begin
    create type ix.surface_e as enum ();
exception when duplicate_object then null; end $$;

/* значения pg_*: packages/apps/ix/pg-meta-scraper/src/boba/pg_meta_scraper/schema/00_surface_values.sql */
/* значения cfl_*: packages/apps/ix/cfl-indexer/src/boba/cfl_indexer/schema/00_values.sql */
alter type ix.surface_e add value if not exists 'cfl_space';
alter type ix.surface_e add value if not exists 'cfl_page';
alter type ix.surface_e add value if not exists 'cfl_blogpost';
alter type ix.surface_e add value if not exists 'cfl_attachment';
alter type ix.surface_e add value if not exists 'cfl_comment';
alter type ix.surface_e add value if not exists 'cfl_page_link';

comment on type ix.surface_e is
'surface name: имя surface-таблицы, в которой лежат атрибуты (properties graph).
Значения в этот enum могут только добавляться (alter type add value if not exists) или переименоваться
Удаление из enum в postgres невозможно. для этого требуется создание нового enum
';

create table if not exists ix.surface (
    name         ix.surface_e primary key,
    description  varchar      not null
);

/* строки pg_*: packages/apps/ix/pg-meta-scraper/src/boba/pg_meta_scraper/schema/10_surface_rows.sql */
/* строки cfl_*: packages/apps/ix/cfl-indexer/src/boba/cfl_indexer/schema/10_rows.sql */
insert into ix.surface (name, description) values
    ('cfl_space',      'Спейс Confluence; корень его tree.'),
    ('cfl_page',       'Страница Confluence.'),
    ('cfl_blogpost',   'Запись блога спейса Confluence.'),
    ('cfl_attachment', 'Файл, вложенный в страницу или блог-запись.'),
    ('cfl_comment',    'Встроенный или нижний комментарий к странице.'),
    ('cfl_page_link',  'Ребро: ссылка со страницы на другую страницу Confluence.')
on conflict (name) do nothing;

/*
node — любой объект, который можно адресовать в источнике.
- address:  главное поле содержащее адрес объекта в виде отдельных частей

Пример:
postgres:
address = {"scheme":"postgresql","host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}
address = {"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "view": "v_orders_daily", "column": "day"}
address = {"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "function": "calc_total", "args": "bigint,numeric"}

web:
addres  = {"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/space/FLINK"}

clickhouse:
address = {"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "column": "user_id"}
address = {"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "projection": "events_by_user"}

oracle:
address = {"scheme": "oracle", "host": "ora1", "port": 1521, "database": "ORCL", "schema": "SALES", "table": "ORDERS"}

mysql:
address = {"scheme": "mysql", "host": "db1", "port": 3306, "database": "shop", "table": "orders"}
*/
create table if not exists ix.node (
    id          bigserial       primary key,
    surface     ix.surface_e    not null references ix.surface,
    address     jsonb           not null,
    created_at  timestamptz     not null default now()
);

/*
Поиск node по адресу:
select id from ix.node
where
поиск всех node с указанными частями
    address @> '{"host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}';

поиск всех адресов postgresql
    address @> '{"scheme": "postgresql"}

поиск всех адресов с укзаанным host
    address @> '{"host": "dwh.local"}'
*/
create unique index if not exists node__address__uk on ix.node using btree (address);
create index if not exists node__address__gin on ix.node using gin (address jsonb_path_ops);
create index if not exists node__surface on ix.node using btree (surface);

/*
tree описывает иерархию объектов.
Колонка лежит в таблице, таблица в схеме, схема в базе,
Вложение лежит в странице, страница в спейсе.
tree содержит связи в виде дерева:
    node_id:    это node_id адреса объекта
    parent_id:  это node_id родителя

tree хранится отдельно от edge, чтобы ранжированию не приходилось каждый раз исключать
подобные связи, так как они влияют на поисковую выдачу (самые очевидные)
*/
create table if not exists ix.tree (
    id          bigserial   not null primary key,
    node_id     bigint      not null references ix.node on delete cascade,
    parent_id   bigint          null references ix.node on delete cascade,
    created_at  timestamptz not null default now()
);
create unique index if not exists tree__uk on ix.tree using btree (node_id, parent_id);

/*
Выбрать всех детей:
    select node_id from ix.tree where parent_id = $1

Выбрать всех корневых родителей:
    select parent_id from ix.tree where parent_id is null

Выбрать все поддерево:
    with recursive sub as (
        select $1::bigint as id
        union all
        select t.node_id from ix.tree t join sub on t.parent_id = sub.id)
    select id from sub

Отдельно стоит отметить что при удалении в node объектов
сработает cascade delete который удалит его строки и в tree однако дети остануться.
Поэтому для удаления всего поддерева индексатор должен это сделать
отдельным рекурсивным запросом
*/
create index if not exists tree__parent on ix.tree using btree (parent_id, node_id);

/*
edge описывает взвешанный граф связей между node

surface указывает на поверхность в которой найдена связь, к примеру:
- postgres:     pg_stat_statements пишет запросы, которые используют пользователи, в этих запросах можно найти взаимосвязи между postgres node
- clickhouse:   system.query_log  также пишет запросы влог, в этих запросах мы найдем взаимосвязи между clickhouse node
- 

*/
create table if not exists ix.edge (
    id          bigserial       not null primary key,
    node_src_id bigint          not null references ix.node on delete cascade,
    node_tgt_id bigint          not null references ix.node on delete cascade,
    surface     ix.surface_e    not null references ix.surface,
    weight      real            not null check (weight between 0 and 1)
);

create unique   index if not exists edge__uk                    on ix.edge using btree (node_src_id, node_tgt_id);
create          index if not exists edge__tgt_src_surface       on ix.edge using btree (node_tgt_id, node_src_id, surface) include (weight);
create          index if not exists edge__surface_src           on ix.edge using btree (surface, node_src_id);


/*
============================================================================
Surface-таблицы

Surface это плоская таблица свойств node одного вида (properties в терминах property
graph). Она связана с core только через node_id и не ссылается на другие surface, чтобы
поверхности добавлялись, заменялись и жили в нескольких версиях независимо друг от друга.
Строка surface хранит свойства целиком; при изменении она удаляется и вставляется заново,
поэтому полей updated_at и content_hash нет: сравнение идёт по всем колонкам.
============================================================================
PostgreSQL и Greenplum

Наполняет пакет packages/apps/ix/pg-meta-scraper: scrape снимает сырые таблицы каталога источника
(без представлений и без замков на пользовательских таблицах), layout раскладывает их в
node, tree, edge, pg_edge и surface. Адрес node строится из частей: scheme, host, port,
database, schema и один из table, view, sequence, index, function+args, type, statistics;
column, constraint, trigger добавляются к адресу владельца.

tree:
    pg_database
    -> pg_schema
       -> pg_table       -> pg_column, pg_constraint, pg_index, pg_trigger
       -> pg_view        -> pg_column
       -> pg_sequence
       -> pg_routine
       -> pg_type        -> pg_constraint (ограничение домена)
       -> pg_statistics

edge, направление src -> tgt значит «src зависит от tgt»; вид связи следует из пары
surface концов:
    pg_constraint -> pg_column     колонки ограничения, у FK ещё целевые колонки (side = 1)
    pg_constraint -> pg_index      индекс, на который опирается PK, UNIQUE, EXCLUDE или FK
    pg_index      -> pg_column     колонки индекса; колонки выражения и предиката без позиции
    pg_index      -> pg_index      индекс секции к индексу секционированной таблицы
    pg_table      -> pg_column     ключ секционирования; в Greenplum ещё ключ распределения
    pg_table      -> pg_table      секция или потомок к родителю
    pg_view       -> pg_column     колонки, которые читает запрос представления
    pg_view       -> pg_table      только когда запрос не трогает ни одной колонки
    pg_column     -> pg_sequence   default через nextval
    pg_column     -> pg_routine    default через функцию
    pg_column     -> pg_column     generated-колонка к исходным (PostgreSQL 15+)
    pg_column     -> pg_type       колонка пользовательского типа
    pg_sequence   -> pg_column     owned by и identity
    pg_trigger    -> pg_column     колонки update of
    pg_trigger    -> pg_routine    функция триггера
    pg_routine    -> pg_column     тело begin atomic (PostgreSQL 14+)
    pg_statistics -> pg_column     колонки статистики

Позиционные рёбра (объект перечисляет колонки по порядку) несут строки в ix.pg_meta_edge:
role называет список, side различает стороны FK, ordinal это позиция, is_key отделяет
ключевые колонки индекса от include. У одного ребра может быть несколько строк: таблица,
секционированная и распределённая по одной колонке, или FK на ту же колонку.

Не хранится по решению: тела функций и определения представлений (LLM читает их в
источнике по адресу), размеры отношений, статистика обращений и колонок, права.
На PostgreSQL 12–14 и Greenplum 7 рёбер generated-колонки нет: каталог их не записывает.
============================================================================

DDL: packages/apps/ix/pg-meta-scraper/src/boba/pg_meta_scraper/schema/10_pg_meta_edge.sql (pg_meta_edge_role_e, pg_edge) и 20_pg_meta_surfaces.sql (pg_database ... pg_statistics)


Аспекты объектов PostgreSQL. Аспект это текст объекта, по которому объект
ищут; у объекта их несколько, и каждый лежит отдельной строкой поисковой
таблицы. Какие аспекты пишет каждая surface и из чего собирает description:

pg_table пишет name, path (schema.table), words, comment, columns, summary
  и description.
pg_column пишет name, path (schema.table.column), words, comment, summary
  и description вида 'Column {path} {type}: {comment}'.
pg_view пишет то же, что pg_table; description строится из имени,
  комментария и колонок, определение представления в текст не входит.
pg_schema и pg_database пишут name, words, comment и description.
pg_index пишет name, path (schema.index), words и description вида
  'Index {name} on {table} ({columns}) {unique}'.
pg_sequence пишет name, path (schema.sequence), words и description.
pg_routine пишет name, path (schema.routine(arguments)), words, comment,
  summary и description вида 'Function {name}({arguments}) returns
  {result}: {comment}'.
pg_constraint пишет name, words и description вида 'Foreign key {name}
  on {table} ({columns}) references {ref_table} ({ref_columns})'.

Атрибуты, по которым не ищут словами, а фильтруют или подправляют выдачу
(владелец, табличное пространство, размер, оценка числа строк, статистика
обращений), живут в surface. DDL, определения представлений и тела
подпрограмм не хранятся и не индексируются: LLM читает их в источнике по
адресу node. Значения строк таблиц не индексируются.


Словарь аспектов источника PostgreSQL: какой текст объекта закодирован
в строке поисковой таблицы. Значения enum только добавляются или
переименовываются: на них ссылаются предикаты частичных индексов
pg_emb_e5_1024, и при переименовании значения в словаре предикаты следуют
за ним. Ниже у каждого аспекта сказано, откуда берётся текст для pg_table
и для чего он нужен; другие surface собирают те же аспекты из своих полей
(attname вместо relname, schema.table.column вместо schema.table).

description это описание, собранное индексатором из всего известного об
  объекте, основной аспект поиска. В pg_fts это части с весами внутри
  одного tsvector: A = words, B = words схемы и comment, C = columns;
  в pg_emb_e5_1024 одна строка вида
  'Table {path}: {comment}. Columns: {col1} ({type}), ...'.
comment это комментарий из источника как есть: obj_description для
  таблицы, col_description для колонки. Пишется, только если не пуст.
columns это имена колонок таблицы через пробел (в pg_emb_e5_1024 через
  запятую), чтобы таблица находилась по своим колонкам.
summary это описание от LLM (плагин describer) из surface pg_llm_description.
  Пишется отдельной строкой, когда описание появилось.
name это имя объекта как есть: pg_class.relname, pg_attribute.attname.
  Нужно для точного совпадения, подстроки и подсказки по префиксу.
path это путь через точку, как его пишет пользователь: nspname || '.' ||
  relname, для колонки ещё || '.' || attname. Нужен для точного совпадения.
words это слова имени: name, разрезанный по CamelCase и подчёркиваниям,
  в нижнем регистре, ё заменена на е. Нужен для поиска с опечатками:
  'ordrs' к 'CustomerOrders' даёт похожесть 0.22, к 'customer orders' 0.5.
*/
do $$ begin
    create type ix.pg_idx_aspect_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.pg_idx_aspect_e add value if not exists 'meta_description';
alter type ix.pg_idx_aspect_e add value if not exists 'meta_comment';
alter type ix.pg_idx_aspect_e add value if not exists 'meta_columns';
alter type ix.pg_idx_aspect_e add value if not exists 'llm_description';
alter type ix.pg_idx_aspect_e add value if not exists 'meta_name';
alter type ix.pg_idx_aspect_e add value if not exists 'meta_path';
alter type ix.pg_idx_aspect_e add value if not exists 'meta_words';

comment on type ix.pg_idx_aspect_e is
    'Какой текст объекта PostgreSQL закодирован в строке поисковой таблицы. Значения только добавляются или переименовываются: на них ссылаются предикаты частичных индексов, и они следуют за переименованием.';

create table if not exists ix.pg_idx_aspect (
    aspect       ix.pg_idx_aspect_e primary key,
    description  varchar        not null
);

insert into ix.pg_idx_aspect (aspect, description) values
    ('meta_description', 'описание объекта, собранное индексатором из всего, что о нём известно; основной аспект поиска'),
    ('meta_comment',     'комментарий из источника как есть (obj_description, col_description); пишется, только если не пуст'),
    ('meta_columns',     'имена колонок таблицы через пробел; таблица находится по своим колонкам'),
    ('llm_description', 'описание от LLM (пакет pg-llm-describer); пишется, только когда оно есть'),
    ('meta_name',        'имя объекта как есть (relname, attname); точное совпадение и подстрока'),
    ('meta_path',        'путь через точку, как пишет пользователь: schema.table или schema.table.column; точное совпадение'),
    ('meta_words',       'слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е; поиск с опечатками')
on conflict (aspect) do nothing;

/*
Поисковые таблицы источника PostgreSQL: по одной на вид индекса, общие для
всех surface pg_*. У всех трёх один ключ (node_id, surface, aspect). surface это
копия node.surface того же типа surface_e: по ней индексы фильтруют по виду,
не обращаясь к node. aspect это значение pg_idx_aspect_e. content это текст
аспекта, из которого построен индекс: триграммам он нужен для точного
расчёта похожести, полнотексту для сниппета, вектору для проверки,
изменился ли текст. Поисковые таблицы не связаны с node внешним ключом: их ведут
независимые индексаторы, каждый своим процессом, и сами убирают строки node,
которых больше нет, и строки с устаревшим content.


Полнотекстовый индекс, строка на аспект. Все surface пишут description
одним tsvector с весами: A = words имени, B = words схемы и comment,
C = columns (только таблица и представление). Текст каждой части
нормализован в коде, tsvector собирает сам insert; content это те же части
одной строкой, для сниппета ts_headline в выдаче и для сравнения при
повторном прогоне:

insert into ix.pg_idx_fts (node_id, surface, aspect, content, tsv)
values ($1, 'pg_meta_table', 'meta_description', $content,
    setweight(to_tsvector('russian', $words), 'A') ||
    setweight(to_tsvector('russian', $schema_words || ' ' || $comment),
              'B') ||
    setweight(to_tsvector('russian', $columns), 'C'));

Summary от LLM это отдельная строка с аспектом summary, а не часть строки
description: у неё другой писатель (describer, а не индексатор), другой
источник (surface pg_llm_description), своё время появления и свой цикл пересчёта.
Индексатор пишет строку description при загрузке объекта, describer позже
добавляет строку summary с весом D:

insert into ix.pg_idx_fts (node_id, surface, aspect, content, tsv)
values ($1, 'pg_meta_table', 'llm_description', $text,
    setweight(to_tsvector('russian', $text), 'D'));

Поиск читает обе строки как один документ: ранг node это сумма рангов
её строк.

select node_id, sum(ts_rank_cd(tsv, q)) as rank
from   ix.pg_idx_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
where  tsv @@ q
group by node_id order by rank desc limit 20;

DDL: packages/apps/ix/pg-idx-fts/src/boba/pg_idx_fts/schema/00_pg_idx_fts.sql


Конфигурация russian стеммит и русский, и английский: order/orders,
заказ/заказы. Простой запрос без суммирования по node:

select node_id, surface, ts_rank_cd(tsv, q) as rank
from   ix.pg_idx_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
where  tsv @@ q
order by rank desc
limit  20;

Один GIN по surface и tsv (btree_gin) обслуживает оба случая: запрос без
фильтра по виду идёт по нему же, запрос с фильтром по редкому виду
отбирает вид внутри индекса. Для частого вида планировщик сам оставляет
surface обычным фильтром после индекса: это дешевле, чем читать его список
из GIN.


Таблица триграмм хранит только идентификаторы, по строке на node_id и
aspect. Длинный текст сюда не кладут: триграммная похожесть на нём не
работает, а btree по lower(content) падает на строках длиннее 2704 байт.
Все surface пишут name и words; path пишут таблица, колонка, представление,
индекс, последовательность и подпрограмма.

DDL: packages/apps/ix/pg-idx-trgm/src/boba/pg_idx_trgm/schema/00_pg_idx_trgm.sql


Подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче.
Используется word_similarity (операторы <% и <<->), а не similarity
(% и <->). Порог <% по умолчанию 0.6, для коротких имён нужен 0.4.

set pg_trgm.word_similarity_threshold = 0.4;
select node_id, surface, content
from   ix.pg_idx_trgm
where  aspect = 'meta_words' and 'ordrs' <% content
order by 'ordrs' <<-> content
limit  20;



Точное совпадение без учёта регистра.

select node_id, surface from ix.pg_idx_trgm
where  aspect = 'meta_path' and lower(content) = lower('dm.fact_orders');



Подсказка при наборе по префиксу. Обычный btree по lower(content) для
префикса не годится, нужен класс операторов varchar_pattern_ops. Вместо
like используется оператор ^@ (starts with): в like подчёркивание значит
«любой символ», и имя fact_orders пришлось бы экранировать.

select node_id, surface, content from ix.pg_idx_trgm
where  aspect = 'meta_name' and lower(content) ^@ lower('fact_ord')
limit  20;



Векторный поиск по embedding-модели e5 размерностью 1024, строка на аспект.
Все surface пишут description; comment пишут те, у кого он не пуст;
columns пишут таблица и представление; summary пишут таблица, колонка,
представление и подпрограмма, когда описание от LLM есть. Текст кодируется
с префиксом passage:, запрос с префиксом query:. content это закодированный
текст аспекта: если он не изменился, модель повторно не запускают.

DDL: packages/apps/ix/pg-idx-vector/src/boba/pg_idx_vector/schema/00_pg_idx_emb_e5_1024.sql
*/

/*
============================================================================
Источник Confluence: пакет packages/apps/ix/cfl-indexer, скрапер и индексатор
одним компонентом. Проверено на Confluence Server стенда (confl.loshara.com)
и на заглушке boba.stand.confluence.

Node бывает пяти surface: спейс, страница, блог-запись, вложение и комментарий;
ребро одного surface — ссылка страницы на страницу. Имена без meta_: это не
метаданные о базе, как у pg, а сами страницы и файлы. Адрес частями, как у
postgres; страница адресуется id без ключа спейса, перенос между спейсами
не рождает новый node:

cfl_space       {"scheme":"https","host":"confl.loshara.com","port":443,"space":"DEV"}
cfl_page        {"scheme":"https","host":"confl.loshara.com","port":443,"content":"307136992"}
cfl_blogpost    {"scheme":"https","host":"confl.loshara.com","port":443,"content":"307140001"}
cfl_attachment  {..., "content":"307136992","attachment":"att4521"}
cfl_comment     {..., "content":"307136992","comment":"127405740"}

tree: спейс -> страницы без предков и блог-записи -> дочерние страницы по
последнему ancestor -> вложения и комментарии страницы.
edge: cfl_page_link от страницы к странице по ссылке в body.view (по id,
по заголовку, макросом ri:page); внешние ссылки и ссылки на себя рёбер не
дают. Ссылки берутся из body.view, а не из body.storage: макросы (cql, toc,
children) разворачиваются только там.

Обход спейса строго последовательный, один воркер на спейс: список страниц
и блог-записей без тел (space/{key}/content/page и /blogpost с expand
version, ancestors, metadata.labels, history, children.attachment), на
каждом объекте node, tree, surface-строка и все три индекса, потом вложения
и комментарии страницы. Оригиналы не хранятся: ни HTML страницы, ни файл
вложения, ни текст комментария. Хэш от оригинала, индекс от преобразования:
content_hash = sha256 исходного HTML body.view (у вложения — байтов файла,
считается по дороге на диск), а в индекс идёт markdown из markdownify без
экранирования подчёркиваний, у вложений — текст liteparse (pdf, office,
картинки с OCR) или декодирование текстовых типов; текст картинки идёт
аспектом ocr, остальное — body. indexer_hash = md5 параметров преобразования
и модели (формат тела, стиль markdown, модель, окно чанков, веса, маски
вложений, кодировки, OCR, версия раскладки): смена любого переиндексирует
спейс с повторным скачиванием — цена решения не хранить оригиналы.

Отсечение работы: version и indexer_hash совпали с surface-строкой — объект
не трогается; version сменился — тело скачивается, content_hash оригинала
решает, что переписать; у вложения при том же content_hash прежний текст
берётся из полнотекста без повторного разбора. Node пишется одной
транзакцией (surface-строка, триграммы, полнотекст, вектор): сорвался шаг —
node прежний, следующий прогон делает его заново. Вектор пересчитывает
только аспекты, чей md5 текста изменился. В конце обхода node спейса,
которых прогон не видел, снимаются вместе со строками индексов; область
спейса — space_key поверхностей внутри одного сервера (address @> base).
Ключ спейса и id контента уникальны только внутри сервера, поэтому индексы
по ним не уникальные.

Вложение получает node и surface-строку всегда — файл находится по имени и
пути; текст только у взятых: маски attachments и флаг OCR решают, качать ли
файл, неподдерживаемый тип остаётся метаданными. Файл, который не
разобрался, считается в failed отчёта спейса и его не останавливает.
============================================================================
*/

/*
cfl-indexer, схема, шаг 0: значения surface_e и aspect_e, которыми владеет индексатор
Confluence. Отдельным файлом: использовать значения enum можно только после коммита,
словарь и объявления идут следующими файлами.
*/
alter type ix.surface_e add value if not exists 'cfl_space';
alter type ix.surface_e add value if not exists 'cfl_page';
alter type ix.surface_e add value if not exists 'cfl_blogpost';
alter type ix.surface_e add value if not exists 'cfl_attachment';
alter type ix.surface_e add value if not exists 'cfl_comment';
alter type ix.surface_e add value if not exists 'cfl_page_link';

alter type ix.aspect_e add value if not exists 'title';
alter type ix.aspect_e add value if not exists 'path';
alter type ix.aspect_e add value if not exists 'words';
alter type ix.aspect_e add value if not exists 'labels';
alter type ix.aspect_e add value if not exists 'card';
alter type ix.aspect_e add value if not exists 'body';
alter type ix.aspect_e add value if not exists 'ocr';

/*
cfl-indexer, схема, шаг 1: строки словарей ix.surface и ix.aspect.
*/
insert into ix.surface (name, description) values
    ('cfl_space',      'Спейс Confluence; корень его tree.'),
    ('cfl_page',       'Страница Confluence.'),
    ('cfl_blogpost',   'Запись блога спейса Confluence.'),
    ('cfl_attachment', 'Файл, вложенный в страницу или блог-запись.'),
    ('cfl_comment',    'Встроенный или нижний комментарий к странице.'),
    ('cfl_page_link',  'Ребро: ссылка со страницы на другую страницу Confluence.')
on conflict (name) do nothing;

insert into ix.aspect (aspect, class, description, owner) values
    ('title',  'ident',       'Заголовок страницы, имя файла вложения или имя спейса как есть.',          'cfl-indexer'),
    ('path',   'ident',       'Путь через ключ спейса: DEV/Заголовок, DEV/Заголовок/design.pdf.',          'cfl-indexer'),
    ('words',  'words',       'Слова заголовка по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е.',  'cfl-indexer'),
    ('labels', 'description', 'Метки страницы через пробел.',                                             'cfl-indexer'),
    ('card',   'description', 'Карточка: вид объекта, путь, метки и начало текста.',                     'cfl-indexer'),
    ('body',   'description', 'Полный текст: markdown страницы, текст вложения или комментария.',        'cfl-indexer'),
    ('ocr',    'description', 'Текст, распознанный на картинке или скане вложения.',                     'cfl-indexer')
on conflict (aspect) do nothing;

/*
cfl-indexer, схема, шаг 2: поверхности Confluence. В них идентификаторы, метаданные и
хэши; ни тела страницы, ни файла здесь нет. content_hash это sha256 оригинала (HTML
страницы, байты файла), indexer_hash это md5 параметров преобразования и модели:
по ним индексатор решает, что переиндексировать. Ключ спейса и id контента уникальны
только внутри одного сервера Confluence, сервер задаёт адрес node, поэтому индексы по
ним не уникальные.
*/
create table if not exists ix.cfl_space (
    node_id       bigint primary key references ix.node on delete cascade,
    space_key     varchar not null,
    name          varchar not null,
    space_type    varchar not null,
    status        varchar not null,
    description   varchar not null,
    content_hash  varchar not null,
    indexer_hash  varchar not null
);
create index if not exists cfl_space__space_key on ix.cfl_space using btree (space_key);

create table if not exists ix.cfl_page (
    node_id          bigint primary key references ix.node on delete cascade,
    space_key        varchar not null,
    content_id       varchar not null,
    title            varchar not null,
    status           varchar not null,
    version          integer not null,
    created_at       timestamptz not null,
    updated_at       timestamptz not null,
    author           varchar not null,
    last_editor      varchar not null,
    labels           varchar[] not null,
    ancestor_titles  varchar[] not null,
    content_hash     varchar not null,
    indexer_hash     varchar not null
);
create index if not exists cfl_page__content_id on ix.cfl_page using btree (content_id);
create index if not exists cfl_page__space_key_title on ix.cfl_page using btree (space_key, title);

create table if not exists ix.cfl_blogpost (
    node_id       bigint primary key references ix.node on delete cascade,
    space_key     varchar not null,
    content_id    varchar not null,
    title         varchar not null,
    status        varchar not null,
    version       integer not null,
    created_at    timestamptz not null,
    updated_at    timestamptz not null,
    author        varchar not null,
    last_editor   varchar not null,
    labels        varchar[] not null,
    content_hash  varchar not null,
    indexer_hash  varchar not null
);
create index if not exists cfl_blogpost__content_id on ix.cfl_blogpost using btree (content_id);
create index if not exists cfl_blogpost__space_key on ix.cfl_blogpost using btree (space_key);

create table if not exists ix.cfl_attachment (
    node_id        bigint primary key references ix.node on delete cascade,
    space_key      varchar not null,
    page_id        varchar not null,
    attachment_id  varchar not null,
    title          varchar not null,
    media_type     varchar not null,
    file_size      bigint not null,
    version        integer not null,
    created_at     timestamptz not null,
    updated_at     timestamptz not null,
    author         varchar not null,
    content_hash   varchar not null,
    indexer_hash   varchar not null
);
create index if not exists cfl_attachment__space_key on ix.cfl_attachment using btree (space_key);
create index if not exists cfl_attachment__page_id on ix.cfl_attachment using btree (page_id);

create table if not exists ix.cfl_comment (
    node_id       bigint primary key references ix.node on delete cascade,
    space_key     varchar not null,
    page_id       varchar not null,
    comment_id    varchar not null,
    location      varchar not null,
    version       integer not null,
    created_at    timestamptz not null,
    updated_at    timestamptz not null,
    author        varchar not null,
    content_hash  varchar not null,
    indexer_hash  varchar not null
);
create index if not exists cfl_comment__space_key on ix.cfl_comment using btree (space_key);
create index if not exists cfl_comment__page_id on ix.cfl_comment using btree (page_id);

/*
Ребро страница -> страница по ссылке в теле; kind говорит, как ссылка была записана:
по id, по заголовку или макросом.
*/
create table if not exists ix.cfl_page_link (
    edge_id  bigint primary key references ix.edge on delete cascade,
    kind     varchar not null
);

/*
cfl-indexer, схема, шаг 3: три таблицы индексов поверхностей Confluence. Устроены как
pg_idx_*: колонка aspect ссылается на словарь ядра, внешнего ключа на ix.node нет
намеренно, строки удалённых node снимает сам индексатор при чистке спейса.
*/
create table if not exists ix.cfl_idx_trgm (
    node_id  bigint not null,
    surface  ix.surface_e not null references ix.surface,
    aspect   ix.aspect_e not null references ix.aspect,
    content  varchar not null,
    primary key (node_id, surface, aspect)
);
create index if not exists cfl_idx_trgm__content__gist on ix.cfl_idx_trgm using gist (content gist_trgm_ops);
create index if not exists cfl_idx_trgm__aspect_lower_content on ix.cfl_idx_trgm using btree (aspect, lower(content));
create index if not exists cfl_idx_trgm__aspect_lower_content__prefix
    on ix.cfl_idx_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists cfl_idx_trgm__surface_aspect on ix.cfl_idx_trgm using btree (surface, aspect);

create table if not exists ix.cfl_idx_fts (
    node_id  bigint not null,
    surface  ix.surface_e not null references ix.surface,
    aspect   ix.aspect_e not null references ix.aspect,
    content  varchar not null,
    tsv      tsvector not null,
    primary key (node_id, surface, aspect)
);
create index if not exists cfl_idx_fts__surface_tsv__gin on ix.cfl_idx_fts using gin (surface, tsv);

/*
Текст длиннее окна модели режется на чанки: chunk_no это номер куска, content_hash это
md5 полного текста аспекта, общий для всех его чанков. Частичный HNSW на каждую пару
surface + aspect, по которой ищут: пары известны пакету, поэтому индексы здесь.
*/
create table if not exists ix.cfl_idx_emb_e5_1024 (
    node_id       bigint not null,
    surface       ix.surface_e not null references ix.surface,
    aspect        ix.aspect_e not null references ix.aspect,
    chunk_no      smallint not null,
    content       varchar not null,
    content_hash  varchar not null,
    emb           halfvec(1024) not null,
    primary key (node_id, surface, aspect, chunk_no)
);
create index if not exists cfl_idx_emb_e5_1024__cfl_space_card__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_space' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_page_card__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_page' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_page_body__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_page' and aspect = 'body';
create index if not exists cfl_idx_emb_e5_1024__cfl_blogpost_card__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_blogpost' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_blogpost_body__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_blogpost' and aspect = 'body';
create index if not exists cfl_idx_emb_e5_1024__cfl_attachment_card__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_attachment' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_attachment_body__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_attachment' and aspect = 'body';
create index if not exists cfl_idx_emb_e5_1024__cfl_attachment_ocr__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_attachment' and aspect = 'ocr';
create index if not exists cfl_idx_emb_e5_1024__cfl_comment_card__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_comment' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_comment_body__hnsw
    on ix.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_comment' and aspect = 'body';


/*
Аспекты Confluence — значения общего ix.aspect_e и строки словаря ix.aspect
с владельцем cfl-indexer; объявления surface_aspect делает сам индексатор и
сам ими пользуется: title, path, words, labels, card выводятся из
surface-строки и уже лежащего в cfl_idx_fts текста (union объявлений с
фильтром по node_id), а body и ocr он кладёт в cfl_idx_fts из Python, и
объявление body читает их оттуда. Описатель и другие потребители объявят
поверх них свои аспекты (describer_input, llm_description).

Таблицы индексов остаются своими у происхождения — cfl_idx_trgm,
cfl_idx_fts, cfl_idx_emb_e5_1024 — с той же формой, что pg_idx_*; стенд
pg-search-lab читает оба набора union all, а подсказки выбирают аспекты по
классу из словаря, а не по имени. Веса полнотекста задаёт конфиг индексатора
по имени аспекта (title A, words A, path B, labels B, card B, body C, ocr C),
аспект без веса получает D.

DDL ниже — копия schema/ пакета cfl-indexer с подставленной схемой ix.
*/

/*
cfl-indexer, схема, шаг 4: объявления surface_aspect поверхностей Confluence. Индексатор
сам читает их при записи каждого node: title, path, words, labels и card выводятся из
surface-строки и уже лежащего в cfl_idx_fts текста, а body и ocr он кладёт в cfl_idx_fts
из Python, и объявление читает их оттуда. Схема в теле удвоена, чтобы после наката в
строке остался плейсхолдер; накат проверяет каждое тело по контракту (node_id, content).
*/
insert into ix.surface_aspect (surface, aspect, body) values
    ('cfl_space', 'title', $body$
    select
        x.node_id,
        x.name as content
    from
        ix.cfl_space x
    $body$),
    ('cfl_space', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        ix.cfl_space x
    $body$),
    ('cfl_space', 'card', $body$
    select
        x.node_id,
        'Space ' || x.space_key || ': ' || x.name
            || coalesce(E'\n' || nullif(x.description, ''), '') as content
    from
        ix.cfl_space x
    $body$),
    ('cfl_page', 'title', $body$
    select
        x.node_id,
        x.title as content
    from
        ix.cfl_page x
    $body$),
    ('cfl_page', 'path', $body$
    select
        x.node_id,
        x.space_key || '/' || x.title as content
    from
        ix.cfl_page x
    $body$),
    ('cfl_page', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.title, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        ix.cfl_page x
    $body$),
    ('cfl_page', 'labels', $body$
    select
        x.node_id,
        nullif(array_to_string(x.labels, ' '), '') as content
    from
        ix.cfl_page x
    $body$),
    ('cfl_page', 'card', $body$
    select
        x.node_id,
        'Page ' || x.space_key || '/' || array_to_string(x.ancestor_titles || x.title, '/')
            || coalesce(E'\nLabels: ' || nullif(array_to_string(x.labels, ' '), ''), '')
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        ix.cfl_page x
        left join ix.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_page', 'body', $body$
    select
        f.node_id,
        f.content
    from
        ix.cfl_idx_fts f
        join ix.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_page'
    where
        f.aspect = 'body'
    $body$),
    ('cfl_blogpost', 'title', $body$
    select
        x.node_id,
        x.title as content
    from
        ix.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'path', $body$
    select
        x.node_id,
        x.space_key || '/' || x.title as content
    from
        ix.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.title, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        ix.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'labels', $body$
    select
        x.node_id,
        nullif(array_to_string(x.labels, ' '), '') as content
    from
        ix.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'card', $body$
    select
        x.node_id,
        'Blog post ' || x.space_key || '/' || x.title
            || coalesce(E'\nLabels: ' || nullif(array_to_string(x.labels, ' '), ''), '')
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        ix.cfl_blogpost x
        left join ix.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_blogpost', 'body', $body$
    select
        f.node_id,
        f.content
    from
        ix.cfl_idx_fts f
        join ix.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_blogpost'
    where
        f.aspect = 'body'
    $body$),
    ('cfl_attachment', 'title', $body$
    select
        x.node_id,
        x.title as content
    from
        ix.cfl_attachment x
    $body$),
    ('cfl_attachment', 'path', $body$
    select
        x.node_id,
        x.space_key || '/' || x.title as content
    from
        ix.cfl_attachment x
    $body$),
    ('cfl_attachment', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.title, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        ix.cfl_attachment x
    $body$),
    ('cfl_attachment', 'card', $body$
    select
        x.node_id,
        'Attachment ' || x.space_key || '/' || x.title
            || ' (' || x.media_type || ', ' || x.file_size || ' bytes)'
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        ix.cfl_attachment x
        left join ix.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_attachment', 'body', $body$
    select
        f.node_id,
        f.content
    from
        ix.cfl_idx_fts f
        join ix.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_attachment'
    where
        f.aspect = 'body'
    $body$),
    ('cfl_attachment', 'ocr', $body$
    select
        f.node_id,
        f.content
    from
        ix.cfl_idx_fts f
        join ix.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_attachment'
    where
        f.aspect = 'ocr'
    $body$),
    ('cfl_comment', 'card', $body$
    select
        x.node_id,
        'Comment by ' || x.author || ' in ' || x.space_key
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        ix.cfl_comment x
        left join ix.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_comment', 'body', $body$
    select
        f.node_id,
        f.content
    from
        ix.cfl_idx_fts f
        join ix.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_comment'
    where
        f.aspect = 'body'
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;
