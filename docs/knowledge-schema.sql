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
    - confluence_page   - хранит информацию об заиндексированных confluence страницах

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
    К примеру для confluence_page это исходное состояние страницы,
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

/* значения pg_*: docs/pg-scraper/schema/00_surface.sql */
alter type ix.surface_e add value if not exists 'confluence_space';
alter type ix.surface_e add value if not exists 'confluence_page';
alter type ix.surface_e add value if not exists 'confluence_attachment';
alter type ix.surface_e add value if not exists 'confluence_comment';

comment on type ix.surface_e is
'surface name: имя surface-таблицы, в которой лежат атрибуты (properties graph).
Значения в этот enum могут только добавляться (alter type add value if not exists) или переименоваться
Удаление из enum в postgres невозможно. для этого требуется создание нового enum
';

create table if not exists ix.surface (
    name         ix.surface_e primary key,
    description  varchar      not null
);

/* строки pg_*: docs/pg-scraper/schema/00_surface.sql */
insert into ix.surface (name, description) values
    ('confluence_space',      'Спейс Confluence; корень его tree.'),
    ('confluence_page',       'Страница или запись блога Confluence.'),
    ('confluence_attachment', 'Файл, вложенный в страницу.'),
    ('confluence_comment',    'Встроенный или нижний комментарий к странице.')
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

Наполняет пакет docs/pg-scraper: scrape снимает сырые таблицы каталога источника
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

Позиционные рёбра (объект перечисляет колонки по порядку) несут строки в ix.pg_edge:
role называет список, side различает стороны FK, ordinal это позиция, is_key отделяет
ключевые колонки индекса от include. У одного ребра может быть несколько строк: таблица,
секционированная и распределённая по одной колонке, или FK на ту же колонку.

Не хранится по решению: тела функций и определения представлений (LLM читает их в
источнике по адресу), размеры отношений, статистика обращений и колонок, права.
На PostgreSQL 12–14 и Greenplum 7 рёбер generated-колонки нет: каталог их не записывает.
============================================================================

DDL: docs/pg-scraper/schema/10_pg_edge.sql (pg_edge_role_e, pg_edge) и 20_surfaces.sql (pg_database ... pg_statistics)


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
summary это описание от LLM (плагин describer) из surface pg_summary.
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
    'Какой текст объекта PostgreSQL закодирован в строке поисковой таблицы. Значения только добавляются или переименовываются: на них ссылаются предикаты частичных индексов, и они следуют за переименованием.';

create table if not exists ix.pg_aspect (
    aspect       ix.pg_aspect_e primary key,
    description  varchar        not null
);

insert into ix.pg_aspect (aspect, description) values
    ('description', 'описание объекта, собранное индексатором из всего, что о нём известно; основной аспект поиска'),
    ('comment',     'комментарий из источника как есть (obj_description, col_description); пишется, только если не пуст'),
    ('columns',     'имена колонок таблицы через пробел; таблица находится по своим колонкам'),
    ('summary',     'описание от LLM (плагин describer); пишется, только когда оно есть'),
    ('name',        'имя объекта как есть (relname, attname); точное совпадение и подстрока'),
    ('path',        'путь через точку, как пишет пользователь: schema.table или schema.table.column; точное совпадение'),
    ('words',       'слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е; поиск с опечатками')
on conflict (aspect) do nothing;

/*
Поисковые таблицы источника PostgreSQL: по одной на вид индекса, общие для
всех surface pg_*. У всех трёх один ключ (node_id, surface, aspect). surface это
копия node.surface того же типа surface_e: по ней индексы фильтруют по виду,
не обращаясь к node. aspect это значение pg_aspect_e. content это текст
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

insert into ix.pg_fts (node_id, surface, aspect, content, tsv)
values ($1, 'pg_table', 'description', $content,
    setweight(to_tsvector('russian', $words), 'A') ||
    setweight(to_tsvector('russian', $schema_words || ' ' || $comment),
              'B') ||
    setweight(to_tsvector('russian', $columns), 'C'));

Summary от LLM это отдельная строка с аспектом summary, а не часть строки
description: у неё другой писатель (describer, а не индексатор), другой
источник (surface pg_summary), своё время появления и свой цикл пересчёта.
Индексатор пишет строку description при загрузке объекта, describer позже
добавляет строку summary с весом D:

insert into ix.pg_fts (node_id, surface, aspect, content, tsv)
values ($1, 'pg_table', 'summary', $summary,
    setweight(to_tsvector('russian', $summary), 'D'));

Поиск читает обе строки как один документ: ранг node это сумма рангов
её строк.

select node_id, sum(ts_rank_cd(tsv, q)) as rank
from   ix.pg_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
where  tsv @@ q
group by node_id order by rank desc limit 20;

DDL: docs/pg-indexer-fts/schema/00_pg_fts.sql


Конфигурация russian стеммит и русский, и английский: order/orders,
заказ/заказы. Простой запрос без суммирования по node:

select node_id, surface, ts_rank_cd(tsv, q) as rank
from   ix.pg_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
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

DDL: docs/pg-indexer-trgm/schema/00_pg_trgm.sql


Подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче.
Используется word_similarity (операторы <% и <<->), а не similarity
(% и <->). Порог <% по умолчанию 0.6, для коротких имён нужен 0.4.

set pg_trgm.word_similarity_threshold = 0.4;
select node_id, surface, content
from   ix.pg_trgm
where  aspect = 'words' and 'ordrs' <% content
order by 'ordrs' <<-> content
limit  20;



Точное совпадение без учёта регистра.

select node_id, surface from ix.pg_trgm
where  aspect = 'path' and lower(content) = lower('dm.fact_orders');



Подсказка при наборе по префиксу. Обычный btree по lower(content) для
префикса не годится, нужен класс операторов varchar_pattern_ops. Вместо
like используется оператор ^@ (starts with): в like подчёркивание значит
«любой символ», и имя fact_orders пришлось бы экранировать.

select node_id, surface, content from ix.pg_trgm
where  aspect = 'name' and lower(content) ^@ lower('fact_ord')
limit  20;



Векторный поиск по embedding-модели e5 размерностью 1024, строка на аспект.
Все surface пишут description; comment пишут те, у кого он не пуст;
columns пишут таблица и представление; summary пишут таблица, колонка,
представление и подпрограмма, когда описание от LLM есть. Текст кодируется
с префиксом passage:, запрос с префиксом query:. content это закодированный
текст аспекта: если он не изменился, модель повторно не запускают.

DDL: docs/pg-indexer-vector/schema/00_pg_emb_e5_1024.sql
*/

/*
============================================================================
Источник Confluence, проверено по REST API cwiki.apache.org. Node бывает
четырёх surface: спейс, страница (страница и блог-запись это один surface,
различаются колонкой content_type), вложение и комментарий. Пользователи
и метки node не становятся: метки это атрибут страницы. Адрес node по surface:
confluence_space       https://host/confluence/rest/api/space/FLINK
confluence_page        https://host/confluence/rest/api/content/307136992
confluence_comment     https://host/confluence/rest/api/content/127405740
confluence_attachment
    https://host/confluence/download/attachments/307136992/design.pdf

tree: спейс -> страницы без ancestors (домашняя, корневые, блог-записи) ->
дочерние страницы (родитель это последний элемент ancestors) -> вложения
и комментарии страницы.
edge: refers_to от страницы к странице или вложению по гиперссылке в теле
(origin declared) и от страницы к таблице по идентификатору в тексте
(origin text_match).
Ссылки берутся из body.view, а не из body.storage: макросы (cql, toc,
children) разворачиваются только там; на странице-оглавлении storage даёт
4 ссылки, view 184. Внутренняя ссылка бывает по id
(/spaces/KEY/pages/ID/..., viewpage.action?pageId=ID) и по заголовку
(/display/KEY/Title, ri:page); заголовок разрешается в node по индексу
confluence_page (space_key, title). Внешние ссылки отбрасываются, node
для них не создаётся.

Повторный прогон отсекает работу на двух уровнях. version из Confluence
отсекает скачивание: если номер не изменился, объект не трогается.
content_hash отсекает переиндексацию: объект скачан и разобран, но хэш
совпал с сохранённым, и поисковые строки остаются прежними (version растёт
и при смене меток или ограничений доступа, текст при этом тот же). Если
хэш не совпал, строки всех aspect этой node удаляются и пишутся заново
одной транзакцией, эмбеддинги считаются заново. Что именно хэшируется,
сказано у каждой surface.

Оригиналы не хранятся: ни тело страницы, ни файл вложения, ни текст
комментария. Адрес объекта хранится только в ix.node.address (REST API),
surface его не дублирует. Ссылка для человека строится из адреса
(/pages/viewpage.action?pageId=ID), а адрес вложения и есть ссылка на
скачивание. Surface хранит идентификаторы и метаданные, оригинал LLM
читает по адресу сама. Текст, извлечённый индексатором (тело страницы,
разбор pdf и docx, OCR картинки, описание картинки от LLM), живёт только
в поисковых таблицах как content своего aspect: это индекс, а не копия.
============================================================================
*/

/*
Surface confluence_summary: описание страницы или вложения, которое
сгенерировал LLM (describer). Устроена как pg_summary: indexer_hash это
снимок настроек прогона, content_hash это хэш текста, из которого пишутся
поисковые строки aspect summary.
*/
create table if not exists ix.confluence_summary (
    node_id       bigint      primary key references ix.node on delete cascade,
    content       varchar     not null,
    content_hash  bytea       not null,
    indexer_hash  bytea       not null,
    created_at    timestamptz not null default now()
);

create index if not exists confluence_summary__indexer_hash on ix.confluence_summary using btree (indexer_hash);

/*
Surface confluence_space: ключ, имя, тип, статус и описание спейса.
*/
create table if not exists ix.confluence_space (
    node_id      bigint  primary key references ix.node on delete cascade,
    space_key    varchar not null,
    name         varchar not null,
    space_type   varchar not null,
    status       varchar not null,
    description  varchar not null default ''
);

/*
Surface confluence_page: метаданные страницы или блог-записи.
content_type = page | blogpost, status = current | archived | trashed.
version это номер версии в Confluence (version.number): если он не
изменился с прошлого прогона, индексатор страницу пропускает. created_at
и author берутся из history, updated_at и last_editor из version. Тело
страницы здесь не хранится: индексатор берёт body.view (отрендеренный HTML
с раскрытыми макросами), снимает теги в коде и кладёт текст в aspect body
поисковых таблиц. content_hash = sha256 этого текста вместе с заголовком
и метками. ancestor_titles это путь заголовков от корня спейса до
родителя, для хлебной крошки в выдаче.
*/
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

/*
Разрешение ссылки по заголовку (/display/KEY/Title) в node.
*/
create index if not exists confluence_page__space_key_title on ix.confluence_page using btree (space_key, title);

/*
Surface confluence_attachment: метаданные вложения. Сам файл не хранится:
индексатор скачивает его, извлекает текст и файл отбрасывает. Какие aspect
получаются, зависит от типа файла: разбор pdf и docx идёт в body, OCR
картинки в ocr, описание картинки от LLM в vision. content_hash это хэш
байтов файла, а не извлечённого текста: OCR и описание от LLM
недетерминированы.
*/
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

/*
Surface confluence_comment: метаданные комментария к странице.
location = inline | footer; у комментария свои version и author. Текст
берётся из body.storage, теги снимаются в коде, и живёт в aspect body;
content_hash = sha256 этого текста.
*/
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

/*
Aspect источника Confluence: enum ix.confluence_aspect_e, описания значений
в словаре ix.confluence_aspect. Что попадает в каждый aspect:
description  описание, которое собрал индексатор. У страницы это title,
             метки, путь заголовков и начало body; у вложения title,
             media_type и начало извлечённого текста; у спейса name
             и description.
body         полный текст: тело страницы или извлечённый текст вложения.
             В confluence_fts лежит целиком, в confluence_emb_e5_1024
             порезан на куски по окну модели, кусок нумерует chunk_no.
summary      описание от LLM из confluence_summary; пишется, если оно есть.
labels       метки страницы через пробел.
name         заголовок страницы, имя файла вложения или имя спейса как есть.
path         space_key || '/' || title, для точного совпадения.
words        слова из name: разрезан по CamelCase, дефисам и
             подчёркиваниям, в нижнем регистре, ё -> е; для поиска
             с опечатками.
ocr          текст, распознанный на картинке или скане (вложения с типом image
             и pdf без текстового слоя).
vision       смысл картинки, описанный LLM по изображению: что на схеме,
             какие таблицы и системы на ней названы.
*/
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
    'Какой текст объекта Confluence лежит в строке поисковой таблицы. Значения только добавляются или переименовываются, и предикаты частичных индексов следуют за переименованием.';

create table if not exists ix.confluence_aspect (
    aspect       ix.confluence_aspect_e primary key,
    description  varchar                not null
);

insert into ix.confluence_aspect (aspect, description) values
    ('description', 'описание, собранное индексатором из title, меток, пути заголовков и начала текста'),
    ('body',        'полный текст страницы или извлечённый текст вложения; для эмбеддингов режется на куски'),
    ('summary',     'описание от LLM (describer); пишется, только если оно есть'),
    ('labels',      'метки страницы через пробел'),
    ('name',        'заголовок страницы, имя файла вложения или имя спейса как есть; точное совпадение и префикс'),
    ('path',        'space_key/title; точное совпадение'),
    ('words',       'заголовок, разрезанный на слова по CamelCase, дефисам и подчёркиваниям, в нижнем регистре, ё -> е; поиск с опечатками'),
    ('ocr',         'текст, распознанный на картинке или скане'),
    ('vision',      'смысл картинки, описанный LLM по самому изображению')
on conflict (aspect) do nothing;

/*
Полнотекстовый индекс. Каждый surface пишет в aspect description один
tsvector с весами. У confluence_page вес A получают words заголовка,
B получают labels, C получает body. У confluence_attachment A получают
words имени файла, C получают body, ocr и vision. У confluence_space
A получают words имени, B получает description. У confluence_comment
C получает body. Summary от LLM это отдельная строка с aspect summary из
confluence_summary и весом D, как в pg_fts.
*/
create table if not exists ix.confluence_fts (
    node_id    bigint   not null,
    surface       ix.surface_e         not null references ix.surface,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    content    varchar  not null,
    tsv        tsvector not null,
    primary key (node_id, surface, aspect)
);

create index if not exists confluence_fts__surface_tsv__gin on ix.confluence_fts using gin (surface, tsv);

/*
Триграммы для поиска по имени. Спейс, страница и вложение пишут aspect
name и words, страница и вложение ещё path. У комментария имени нет,
в эту таблицу он не пишется.
*/
create table if not exists ix.confluence_trgm (
    node_id    bigint   not null,
    surface       ix.surface_e         not null references ix.surface,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    content    varchar  not null,
    primary key (node_id, surface, aspect)
);

create index if not exists confluence_trgm__content__gist on ix.confluence_trgm using gist (content gist_trgm_ops);
create index if not exists confluence_trgm__aspect_lower_content on ix.confluence_trgm using btree (aspect, lower(content));
create index if not exists confluence_trgm__aspect_lower_content__prefix
    on ix.confluence_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists confluence_trgm__surface_aspect on ix.confluence_trgm using btree (surface, aspect);

/*
Векторный поиск на эмбеддингах e5 размерности 1024. Текст страницы длиннее
окна модели (512 токенов), поэтому aspect body режется на куски
с перекрытием, и в первичном ключе есть chunk_no; у aspect, который
помещается в один кусок, chunk_no = 0. Страница пишет description и body,
комментарий body, вложение body или ocr и vision в зависимости от типа
файла, спейс description; summary пишет любой surface, у которого оно есть.
*/
create table if not exists ix.confluence_emb_e5_1024 (
    node_id    bigint   not null,
    surface       ix.surface_e         not null references ix.surface,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    chunk_no   smallint      not null,
    content    varchar       not null,
    emb        halfvec(1024) not null,
    primary key (node_id, surface, aspect, chunk_no)
);

/*
Частичный HNSW на каждую пару surface + aspect, по которой ищут: description,
body, ocr, vision и summary.
*/
create index if not exists confluence_emb_e5_1024__page_description__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_page' and aspect = 'description';
create index if not exists confluence_emb_e5_1024__page_summary__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_page' and aspect = 'summary';
create index if not exists confluence_emb_e5_1024__page_body__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_page' and aspect = 'body';
create index if not exists confluence_emb_e5_1024__attachment_body__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_attachment' and aspect = 'body';
create index if not exists confluence_emb_e5_1024__space_description__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_space' and aspect = 'description';
create index if not exists confluence_emb_e5_1024__comment_body__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_comment' and aspect = 'body';
create index if not exists confluence_emb_e5_1024__attachment_ocr__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_attachment' and aspect = 'ocr';
create index if not exists confluence_emb_e5_1024__attachment_vision__hnsw
    on ix.confluence_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'confluence_attachment' and aspect = 'vision';
