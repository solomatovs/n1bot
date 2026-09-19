create extension if not exists pg_trgm;
create extension if not exists vector;
create extension if not exists btree_gin;

create schema if not exists ix;

/*
============================================================================
Принципы проектирования

Система состоит из ядра (core) и поверхностей индексации (surface)

core — место хранения адресов объектов и связей между ними.
core состоит из:
    node — хранит всё, что можно адресовать в источнике: таблица, колонка, страница в Confluence.
Все что угодно, что можно выразить в виде адреса, а при поисковой выдаче можно скинуть прямую ссылку на объект
    tree - хранит иерархию между node.id, когда есть четкая иерархическая последовательность.
    edge - хранит смысловые связи в виде графа, когда одни

Пример tree:
    - confluence tree: space -> page -> attachment
    - postgres tree: database -> schema -> table -> column
Пример edge:
    - postgres edge: foreign key
    - confluence edge: links

По tree можно построить иерархию, по edge можно построить граф связанности

surface — поверхность индексации - то, что индексируется.
surface - это одна или несколько плоских таблиц, в которых храниться метаинформация об объекте
surface таблицы перечислены в таблице node_kind - буквально содержит название surface таблицы,
что бы проще было найти связи между core и surface
Если хочется добавить новый surface, то в node_kind расположен тот самый реестр surface таблиц
Пример surface:
    - pg_table          - хранит информацию о таблицах любых заиндексированных postgres источников
    - pg_column         - хранит информацию о колонках таблиц
    - pg_constraint     - хранит информацию об индексах
    - confluence_page   - хранит информацию об заиндексированных confluence страницах

surface таблицы желательно должны проектироваться без связи друг с другом.
Они должны ссылаться через node_id на core, но не на друг друга
Это позволит выполнять горизонтальное масштабирование и добавлять новые индексы без особых проблем

surface хранит атрибуты объекта в структурированном виде, а node_id позволяет получить address.
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
    - comment - у postgres есть коментарий и его можно прогнать через btree, fts, trgm, vector индексы
    - human_description - описание колонки, которое делает человек
    - llm_description - описание колонки, которое делает llm
    - create statement - некий sql stmp в который входит название, тип данных, ограничения
    - path - путь к колонке через точку, к примеру: {schema}.{table}.{column}
    Таких аспектов индексации можно придумать сколько угодно, они бесконечны.
Обрати вниание, что при составлении индекса мы имеем дело с пересечением четырех осей:
- node.address: адрес индексируемого объекта
- node.kind:    поверхность индексируемого объекта
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
    'Kind node: имя surface-таблицы, в которой лежат атрибуты node. Значения только добавляются (alter type add value if not exists) или переименовываются; предикаты частичных индексов следуют за переименованием.';

create table if not exists ix.node_kind (
    kind         ix.node_kind_e primary key,
    description  varchar        not null
);

insert into ix.node_kind (kind, description) values
    ('pg_database',           'База данных источника PostgreSQL; корень его tree.'),
    ('pg_schema',             'Схема базы данных PostgreSQL.'),
    ('pg_table',              'Таблица, включая секционированные таблицы и секции.'),
    ('pg_column',             'Колонка таблицы, представления или материализованного представления.'),
    ('pg_view',               'Представление или материализованное представление.'),
    ('pg_index',              'Индекс таблицы.'),
    ('pg_sequence',           'Последовательность.'),
    ('pg_routine',            'Функция, процедура, агрегат или оконная функция; каждая перегрузка — отдельный node.'),
    ('pg_constraint',         'Ограничение таблицы; внешний ключ является source для edge вида references.'),
    ('confluence_space',      'Спейс Confluence; корень его tree.'),
    ('confluence_page',       'Страница или запись блога Confluence.'),
    ('confluence_attachment', 'Файл, вложенный в страницу.'),
    ('confluence_comment',    'Встроенный или нижний комментарий к странице.')
on conflict (kind) do nothing;

/*
node — любой объект, который можно адресовать в источнике.
- address:  главное поле содержащее адрес объекта в виде отдельных частей
- url:      строится из address (всегда по одному и тому же алгоритму) и имеет уникальный индекс
    по сути это другая форма записи address, более понятная человеку и llm
    однако по address полю удобней искать

Пример:
postgres:
address = {"scheme":"postgresql","host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}
url     = postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders
address = {"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "view": "v_orders_daily", "column": "day"}
url     = postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily&column=day
address = {"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "function": "calc_total", "args": "bigint,numeric"}
url     = postgresql://dwh.local:5432/dwh?schema=dm&function=calc_total&args=bigint%2Cnumeric

web:
addres  = {"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/space/FLINK"}
url     = https://cwiki.apache.org/confluence/rest/api/space/FLINK

clickhouse:
address = {"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "column": "user_id"}
url     = clickhouse://ch1:9000/logs?table=events&column=user_id
address = {"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "projection": "events_by_user"}
url     = clickhouse://ch1:9000/logs?table=events&projection=events_by_user

oracle:
address = {"scheme": "oracle", "host": "ora1", "port": 1521, "database": "ORCL", "schema": "SALES", "table": "ORDERS"}
url     = oracle://ora1:1521/ORCL?schema=SALES&table=ORDERS

mysql:
address = {"scheme": "mysql", "host": "db1", "port": 3306, "database": "shop", "table": "orders"}
url     = mysql://db1:3306/shop?table=orders
*/
create table if not exists ix.node (
    id          bigserial   primary key,
    kind        ix.node_kind_e not null references ix.node_kind,
    address     jsonb       not null,
    url         varchar     not null unique,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

/*
Поиск node по адресу:
select id from ix.node
where
    -- поиск всех node с указанными частями
    address @> '{"host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}';
    -- поиск всех адресов postgresql
    address @> '{"scheme": "postgresql"}
    -- поиск всех адресов с укзаанным host
    address @> '{"host": "dwh.local"}'
*/
create index if not exists node__address__gin on ix.node using gin (address jsonb_path_ops);
create index if not exists node__kind on ix.node using btree (kind);

/*
tree описывает иерархию объектов.
Колонка лежит в таблице, таблица в схеме, схема в базе, вложение в странице, страница в спейсе.
tree содержит связи в виде дерева:
    node_id:    это node_id адреса объекта
    parent_id:  это node_id родителя

tree хранится отдельно от edge, чтобы ранжированию не приходилось каждый раз исключать
подобные связи, так как они влияют на поисковую выдачу (самые очевидные)
*/
create table if not exists ix.tree (
    node_id    bigint primary key references ix.node on delete cascade,
    parent_id  bigint not null references ix.node on delete cascade
);

/*
Выбрать всех детей:
    select node_id from ix.tree where parent_id = $1

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
edge_kind — это виды связей, которые хранит edge таблица
    Виды связей строго фиксированы и расписаны как postgres enum
    Это позволяет индексировать поле как число (быстро)
    и при этом обращаться к полю по имени (удобно для человека)

Каждый новый вид связей это добавление в enum нового значения через alter:
    alter type ix.edge_kind_e add value if not exists 'references';

Удаление из enum невозможно, а значит если это необходимо сделать
    значит необходимо заново пересоздавать этотenum

Обрати внимание, что для того, что бы enum небыл повисшим в воздухе магическим словом
    сделана таблица edge_kind у которой primary key это edge_kind_e (enum postgres)
    это позволяет сделать прямые foreign key между таблицами и контролировать использование kind связей
    а также, что бы начать использовать новое enum значение, нужно добавить в эту таблицу этот enum
    и сделать его описание в виде description, что позволит сохранить порядок в схеме хранения
*/
do $$ begin
    create type ix.edge_kind_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.edge_kind_e add value if not exists 'foreign_key';
alter type ix.edge_kind_e add value if not exists 'reads_from';
alter type ix.edge_kind_e add value if not exists 'writes_to';
alter type ix.edge_kind_e add value if not exists 'queried_with';
alter type ix.edge_kind_e add value if not exists 'refers_to';

comment on type ix.edge_kind_e is
    'Вид связей в таблице edge';

create table if not exists ix.edge_kind (
    name         ix.edge_kind_e primary key,
    description  varchar        not null
);

insert into ix.edge_kind (name, description) values
    ('reads_from',      'View (представление) или ETL, который читает из таблицы.'),
    ('writes_to',       'ETL пишет в таблицу'),
    ('queried_with',    'Таблицы встречаются в одном запросе; пишется в обе стороны.'),
    ('refers_to',       'Документ ссылается на объект: страница на страницу, вложение или таблицу; способ ссылки (гиперссылка или имя в тексте) задаёт origin.')
on conflict (name) do nothing;

/*
edge_origin — это ответ на вопрос а "как получена связь?" в таблице edge
Почему вообще нужно сохранять источник взаимосвязи?
несколько задач:
    - владение весовым коэфициентом. у каждого индексатора свой собственный вес в ранжировании результатов
    - объяснение пользователю, кто нашел связь, какой именно индексатор

Давай пример:
В postgres пользователи пишут запросы, которые логируются и которые могут явиться источником информации о связях.
    Прочитав источник pg_stat_statements мы сможем определить связи между таблицами, колонками и прочим
    Если отдадим конкретный запрос в llm и попросим сформировать строго определенные взаимосвязи
    Однако 
    Значит тип связи reads_from устанавливается для разных объектов,
    так как postgres и clickhouse адреса будут разными.
    Но представь что информация об этой связи получена из разных источников:
    - pg_catalog.pg_stat_statements - предоставляет запросы, которые пишут пользователи
    - system.query_log              - предоставляет запросы, которые пишут пользователи



Origin нужен для трёх вещей: писатель находит свои строки по origin и своим
node; у каждого origin своя шкала weight; пользователю можно объяснить,
откуда взялась связь, а откуда именно, видно по kind node на её концах.
declared      объявлена самим источником: foreign key, зависимость view
              от таблицы, гиперссылка на странице, зависимость задач ETL.
              weight всегда 1.
observed      наблюдена в поведении: таблицы в одном запросе из
              pg_stat_statements или system.query_log. weight равен
              логарифму числа наблюдений, нормированному по прогону.
text_match    идентификатор объекта найден буквально в тексте другого:
              страница упоминает dm.fact_orders, тело функции упоминает
              таблицу. weight 1, потому что объект назван явно.
name_rule     предположена правилом по именам и типам: колонка совпала
              с primary key другой таблицы, копия таблицы в другом
              источнике с теми же колонками. weight равен уверенности
              правила.
llm           предположена моделью: describer вывел связь из комментариев
              и соседей, vision назвал таблицы на схеме. weight равен
              уверенности модели.
*/
do $$ begin
    create type ix.edge_origin_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.edge_origin_e add value if not exists 'declared';
alter type ix.edge_origin_e add value if not exists 'observed';
alter type ix.edge_origin_e add value if not exists 'text_match';
alter type ix.edge_origin_e add value if not exists 'name_rule';
alter type ix.edge_origin_e add value if not exists 'llm';

comment on type ix.edge_origin_e is
    'Как стало известно об edge: объявлен источником, наблюдён в использовании, найден буквально в тексте, предположен правилом по именам, предположен LLM. У каждого origin своя шкала weight.';

create table if not exists ix.edge_origin (
    name        ix.edge_origin_e   primary key,
    description varchar            not null
);

insert into ix.edge_origin (name, description) values
    ('declared',   'Объявлен самим источником: foreign key, зависимость view, гиперссылка, зависимость ETL; weight 1.'),
    ('observed',   'Наблюдён в использовании: таблицы в одном запросе из pg_stat_statements или system.query_log; weight равен логарифму числа наблюдений, нормированному по прогону.'),
    ('text_match', 'Идентификатор объекта найден буквально в тексте другого; weight 1.'),
    ('name_rule',  'Предположен правилом по именам и типам; weight равен уверенности правила.'),
    ('llm',        'Предположен моделью (describer, vision); weight равен уверенности модели.')
on conflict (name) do nothing;

/*
Edge — смысловые связи между node, explicit и implicit вместе. По ним
считается ранг и строятся диаграммы. Принадлежности здесь нет, она в tree.

Направление: source — объект, которому нужен target. Ограничение
fact_orders_customer_fkey пишется как ограничение -> customers,
представление, читающее orders, — как представление -> orders. PageRank
считает входящие edge голосами, поэтому высокий ранг означает, что от node
зависят многие.

Внешний ключ — отдельный node вида pg_constraint под таблицей-владельцем
в tree, и edge references идёт от него. Так каждая связь адресуема, и пять
ключей между одной парой таблиц остаются пятью edge. Колонки ключа — его
атрибуты, они лежат в surface ограничения.

weight — сколько edge весит для ранга, от 0 до 1. У explicit связей 1,
у implicit меньше, и нормируется он внутри своего origin. Сплошную или
пунктирную линию на диаграмме выбирает kind, а не weight.

origin — откуда взят edge. Одна пара node с одним kind может лежать по
строке на каждый origin: (orders, customers, shares_key_with, name_rule,
0.5) и (orders, customers, shares_key_with, llm, 0.8). Для ранга и
диаграммы пара сворачивается в одно число: 1 - (1 - 0.5) * (1 - 0.8) = 0.9,
то есть два независимых мнения усиливают друг друга. Подтверждение правила —
это запрос: пары, у которых есть и shares_key_with от name_rule, и
references от declared.

Повторный прогон писателя — это diff, а не перезапись. Найденное
сравнивается с его собственными строками (по origin и своим node: загрузчик
Postgres владеет строками declared и observed с source_id в его базе,
правило по именам — всеми name_rule, describer — всеми llm), новые
вставляются, у изменившихся обновляется weight, удаляются только исчезнувшие.
Массовых delete и insert нет.
*/
create table if not exists ix.edge (
    source_id  bigint   not null references ix.node on delete cascade,
    target_id  bigint   not null references ix.node on delete cascade,
    kind       ix.edge_kind_e not null references ix.edge_kind,
    origin     ix.origin_e    not null references ix.origin,
    weight     real     not null check (weight between 0 and 1),
    primary key (source_id, target_id, kind, origin)
);

/*
Кто зависит от node, пара свёрнута по origin:
select source_id, kind, 1 - exp(sum(ln(1 - weight))) as weight
from   ix.edge where target_id = $1 group by source_id, kind;
ER-диаграмма схемы: таблицы берутся через tree, ключи — как дети таблиц
вида pg_constraint, целевая таблица — через edge:
*/
with t  as (select node_id as id from ix.tree where parent_id = $schema_id),
     fk as (select tr.node_id as fk_id, tr.parent_id as table_id
            from   ix.tree tr
            join   t on t.id = tr.parent_id
            join   ix.node n on n.id = tr.node_id and n.kind = 'pg_constraint')
select fk.table_id, fk.fk_id, e.target_id
from   fk join ix.edge e on e.source_id = fk.fk_id and e.kind = 'references';
create index if not exists edge__target_source_kind
    on ix.edge using btree (target_id, source_id, kind) include (origin, weight);

-- Строки одного origin для diff при повторном прогоне писателя.
create index if not exists edge__origin_source on ix.edge using btree (origin, source_id);

/*
PageRank node. Считается в коде: берутся edge выбранных kind, свёрнутые по
origin в один weight на пару, и node вида таблица, представление или
материализованное представление; edge от node ограничения стягивается через
tree в таблицу-владельца. Результат записывается одной транзакцией.
value — сырое значение, сумма по всему графу равна 1. percentile — место
node среди остальных от 0 до 10000; поиск использует его как буст к
текстовой релевантности. Если строки нет, node в прогоне не участвовал и
буст у него нулевой.
*/
create table if not exists ix.node_pagerank (
    node_id      bigint           primary key references ix.node on delete cascade,
    value        double precision not null,
    percentile   smallint         not null check (percentile between 0 and 10000),
    computed_at  timestamptz      not null default now()
);

-- Верхушка ранга:
-- select node_id, value from ix.node_pagerank order by value desc limit 20;
create index if not exists node_pagerank__value on ix.node_pagerank using btree (value desc);

/*
============================================================================
Surface-таблицы: атрибуты node из источника. Каждая surface — плоская
таблица с единственной связью с ядром через node_id и полными именами
объекта для быстрого доступа внутри источника. Связей в ней нет и по ней
они не строятся.
============================================================================

============================================================================
Источник PostgreSQL.

Виды node: база, схема, таблица, колонка, представление (view и matview
один kind, различаются атрибутом view_kind), индекс, последовательность,
подпрограмма (function, procedure, aggregate и window один kind,
различаются атрибутом routine_kind), ограничение.

Tree строится так: база -> схема -> таблица | представление |
последовательность | подпрограмма; таблица -> колонка | ограничение |
индекс; представление -> колонка. Индекс в адресе уникален в пределах
схемы, но в tree лежит под таблицей, которой принадлежит
(pg_index.indrelid).

Edge двух kind: references идёт от node ограничения к таблице, на которую
ссылается внешний ключ; reads_from идёт от представления к таблицам и
представлениям, которые оно читает (по pg_rewrite и pg_depend).

Тела представлений и подпрограмм не хранятся: LLM при необходимости
читает их в источнике по адресу node.

content_hash таблицы покрывает её колонки и ограничения: если хэш таблицы
изменился, переписываются поисковые строки самой таблицы, её колонок и
ограничений. У базы, схемы и последовательности content_hash нет, их
атрибуты сравниваются с прошлым прогоном напрямую.
============================================================================

Surface pg_table: атрибуты таблицы PostgreSQL. Оригинал DDL и адрес здесь
не хранятся, только идентификаторы (база, схема, имя), табличное
пространство, владелец и комментарий.
content_hash — sha256 текста, из которого строятся аспекты поиска: имя,
путь, комментарий, колонки с типами и комментариями. Версии у объектов
PostgreSQL нет, поэтому хэш здесь единственный признак изменения: совпал с
прошлым прогоном — поисковые строки node не трогаются; не совпал —
переписываются все, включая строки колонок и ограничений таблицы.
*/
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

/*
Surface pg_summary: текст описания объекта, сгенерированный LLM
(describer). Describer — источник, чьи оригиналы хранятся только у нас:
адреса, по которому summary можно перечитать, не существует, поэтому сам
текст лежит в поле content.
indexer_hash играет роль версии: md5 снимка настроек прогона (модель,
системный промпт, параметры генерации). При повторном прогоне строки с
текущим indexer_hash не трогаются, строки с чужим генерируются заново.
content_hash — md5 текста content; по нему поисковые строки aspect summary
решают, надо ли переиндексировать node.
Summary пишется для таблиц, колонок, представлений и подпрограмм.
*/
create table if not exists ix.pg_summary (
    node_id       bigint      primary key references ix.node on delete cascade,
    content       varchar     not null,
    content_hash  bytea       not null,
    indexer_hash  bytea       not null,
    created_at    timestamptz not null default now()
);

-- Очередь describer'а: node нужных kind без summary или с чужим indexer_hash.
-- select n.id
-- from   ix.node n
-- left join ix.pg_summary s on s.node_id = n.id
-- where  n.kind in ('pg_table', 'pg_column', 'pg_view', 'pg_routine')
--   and (s.node_id is null or s.indexer_hash <> $current);
create index if not exists pg_summary__indexer_hash on ix.pg_summary using btree (indexer_hash);

-- Surface pg_database: атрибуты базы из pg_database. owner —
-- pg_get_userbyid(datdba), encoding — pg_encoding_to_char(encoding),
-- collate_name — datcollate, comment — shobj_description. content_hash нет:
-- атрибутов мало, они сравниваются с прошлым прогоном напрямую.
create table if not exists ix.pg_database (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    owner          varchar not null,
    encoding       varchar not null,
    collate_name   varchar not null,
    comment        varchar not null default ''
);

-- Surface pg_schema: атрибуты схемы из pg_namespace, comment —
-- obj_description. content_hash нет, атрибуты сравниваются напрямую.
create table if not exists ix.pg_schema (
    node_id        bigint  primary key references ix.node on delete cascade,
    database_name  varchar not null,
    schema_name    varchar not null,
    owner          varchar not null,
    comment        varchar not null default ''
);

-- Surface pg_column: атрибуты колонки из pg_attribute таблицы или
-- представления. relation_kind = table | view | matview показывает, чья это
-- колонка, без обращения к tree. default_expr берётся из pg_attrdef через
-- pg_get_expr, generated = attgenerated ('' — обычная колонка, 's' —
-- вычисляемая). Своего content_hash нет: колонку покрывает content_hash
-- таблицы или представления, при его смене поисковые строки колонки
-- переписываются вместе с родителем.
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

-- Surface pg_view: атрибуты представления из pg_class с relkind v | m;
-- view_kind = view | matview. Определение (pg_get_viewdef) не хранится, но
-- входит в content_hash вместе с колонками и комментарием: изменилось
-- определение — переписываются поисковые строки представления и его колонок.
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

-- Surface pg_index: атрибуты индекса из pg_index, pg_class индекса и
-- таблицы, метод доступа из pg_am. columns — выражения ключа по порядку,
-- взятые из pg_get_indexdef по колонкам; predicate — условие частичного
-- индекса из indpred. content_hash = md5(pg_get_indexdef(indexrelid)) у
-- индекса свой, потому что в content_hash таблицы индексы не входят.
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

-- Surface pg_sequence: атрибуты последовательности из pg_sequence и
-- pg_class. owned_by — колонка-владелец в виде schema.table.column из
-- pg_depend (deptype a или i), пусто у свободной последовательности; это
-- атрибут для чтения, edge из него не строится. content_hash нет, атрибуты
-- сравниваются напрямую.
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

-- Surface pg_routine: атрибуты подпрограммы из pg_proc. routine_kind =
-- function | procedure | aggregate | window (prokind f, p, a, w).
-- arguments — pg_get_function_identity_arguments; те же аргументы входят в
-- адрес, поэтому перегрузки — разные node. result —
-- pg_get_function_result, language — из pg_language. Тело (prosrc) не
-- хранится, но входит в content_hash вместе с сигнатурой и комментарием.
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

-- Surface pg_constraint: атрибуты ограничения таблицы из pg_constraint.
-- constraint_type = primary | unique | foreign | check | exclusion (contype
-- p, u, f, c, x). columns — колонки ограничения по порядку conkey. Для
-- внешнего ключа заполняются ref_schema_name, ref_table_name и ref_columns
-- по confkey, on_delete и on_update — из confdeltype и confupdtype; это
-- атрибуты для чтения, сама связь — edge references от этой node к таблице,
-- на которую ссылается ключ. Выражение check не хранится. Своего
-- content_hash нет: ограничение покрывает content_hash таблицы.
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
-- Аспекты объектов PostgreSQL. Аспект это текст объекта, по которому объект
-- ищут; у объекта их несколько, и каждый лежит отдельной строкой поисковой
-- таблицы. Какие аспекты пишет каждая surface и из чего собирает description:
--
-- pg_table пишет name, path (schema.table), words, comment, columns, summary
--   и description.
-- pg_column пишет name, path (schema.table.column), words, comment, summary
--   и description вида 'Column {path} {type}: {comment}'.
-- pg_view пишет то же, что pg_table; description строится из имени,
--   комментария и колонок, определение представления в текст не входит.
-- pg_schema и pg_database пишут name, words, comment и description.
-- pg_index пишет name, path (schema.index), words и description вида
--   'Index {name} on {table} ({columns}) {unique}'.
-- pg_sequence пишет name, path (schema.sequence), words и description.
-- pg_routine пишет name, path (schema.routine(arguments)), words, comment,
--   summary и description вида 'Function {name}({arguments}) returns
--   {result}: {comment}'.
-- pg_constraint пишет name, words и description вида 'Foreign key {name}
--   on {table} ({columns}) references {ref_table} ({ref_columns})'.
--
-- Атрибуты, по которым не ищут словами, а фильтруют или подправляют выдачу
-- (владелец, табличное пространство, размер, оценка числа строк, статистика
-- обращений), живут в surface. DDL, определения представлений и тела
-- подпрограмм не хранятся и не индексируются: LLM читает их в источнике по
-- адресу node. Значения строк таблиц не индексируются.
-- ----------------------------------------------------------------------------

-- Словарь аспектов источника PostgreSQL: какой текст объекта закодирован
-- в строке поисковой таблицы. Значения enum только добавляются или
-- переименовываются: на них ссылаются предикаты частичных индексов
-- pg_emb_e5_1024, и при переименовании значения в словаре предикаты следуют
-- за ним. Ниже у каждого аспекта сказано, откуда берётся текст для pg_table
-- и для чего он нужен; другие surface собирают те же аспекты из своих полей
-- (attname вместо relname, schema.table.column вместо schema.table).
--
-- description это описание, собранное индексатором из всего известного об
--   объекте, основной аспект поиска. В pg_fts это части с весами внутри
--   одного tsvector: A = words, B = words схемы и comment, C = columns;
--   в pg_emb_e5_1024 одна строка вида
--   'Table {path}: {comment}. Columns: {col1} ({type}), ...'.
-- comment это комментарий из источника как есть: obj_description для
--   таблицы, col_description для колонки. Пишется, только если не пуст.
-- columns это имена колонок таблицы через пробел (в pg_emb_e5_1024 через
--   запятую), чтобы таблица находилась по своим колонкам.
-- summary это описание от LLM (плагин describer) из surface pg_summary.
--   Пишется отдельной строкой, когда описание появилось.
-- name это имя объекта как есть: pg_class.relname, pg_attribute.attname.
--   Нужно для точного совпадения, подстроки и подсказки по префиксу.
-- path это путь через точку, как его пишет пользователь: nspname || '.' ||
--   relname, для колонки ещё || '.' || attname. Нужен для точного совпадения.
-- words это слова имени: name, разрезанный по CamelCase и подчёркиваниям,
--   в нижнем регистре, ё заменена на е. Нужен для поиска с опечатками:
--   'ordrs' к 'CustomerOrders' даёт похожесть 0.22, к 'customer orders' 0.5.
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

-- ----------------------------------------------------------------------------
-- Поисковые таблицы источника PostgreSQL: по одной на вид индекса, общие для
-- всех surface pg_*. У всех трёх один ключ (node_id, kind, aspect). kind это
-- копия node.kind того же типа node_kind_e: по ней индексы фильтруют по виду,
-- не обращаясь к node. aspect это значение pg_aspect_e. content это текст
-- аспекта, из которого построен индекс: триграммам он нужен для точного
-- расчёта похожести, полнотексту для сниппета, вектору для проверки,
-- изменился ли текст. Загрузчик пишет node, surface и поисковые строки одной
-- транзакцией.
-- ----------------------------------------------------------------------------

-- Полнотекстовый индекс, строка на аспект. Все surface пишут description
-- одним tsvector с весами: A = words имени, B = words схемы и comment,
-- C = columns (только таблица и представление). Текст каждой части
-- нормализован в коде, tsvector собирает сам insert; content это те же части
-- одной строкой, для сниппета ts_headline в выдаче и для сравнения при
-- повторном прогоне:
--
-- insert into ix.pg_fts (node_id, kind, aspect, content, tsv)
-- values ($1, 'pg_table', 'description', $content,
--     setweight(to_tsvector('russian', $words), 'A') ||
--     setweight(to_tsvector('russian', $schema_words || ' ' || $comment),
--               'B') ||
--     setweight(to_tsvector('russian', $columns), 'C'));
--
-- Summary от LLM это отдельная строка с аспектом summary, а не часть строки
-- description: у неё другой писатель (describer, а не индексатор), другой
-- источник (surface pg_summary), своё время появления и свой цикл пересчёта.
-- Индексатор пишет строку description при загрузке объекта, describer позже
-- добавляет строку summary с весом D:
--
-- insert into ix.pg_fts (node_id, kind, aspect, content, tsv)
-- values ($1, 'pg_table', 'summary', $summary,
--     setweight(to_tsvector('russian', $summary), 'D'));
--
-- Поиск читает обе строки как один документ: ранг node это сумма рангов
-- её строк.
--
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

-- Конфигурация russian стеммит и русский, и английский: order/orders,
-- заказ/заказы. Простой запрос без суммирования по node:
--
-- select node_id, kind, ts_rank_cd(tsv, q) as rank
-- from   ix.pg_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
-- where  tsv @@ q
-- order by rank desc
-- limit  20;
--
-- Один GIN по kind и tsv (btree_gin) обслуживает оба случая: запрос без
-- фильтра по виду идёт по нему же, запрос с фильтром по редкому виду
-- отбирает вид внутри индекса. Для частого вида планировщик сам оставляет
-- kind обычным фильтром после индекса: это дешевле, чем читать его список
-- из GIN.
create index if not exists pg_fts__kind_tsv__gin on ix.pg_fts using gin (kind, tsv);

-- Таблица триграмм хранит только идентификаторы, по строке на node_id и
-- aspect. Длинный текст сюда не кладут: триграммная похожесть на нём не
-- работает, а btree по lower(content) падает на строках длиннее 2704 байт.
-- Все surface пишут name и words; path пишут таблица, колонка, представление,
-- индекс, последовательность и подпрограмма.
create table if not exists ix.pg_trgm (
    node_id    bigint   not null references ix.node on delete cascade,
    kind       ix.node_kind_e not null references ix.node_kind,
    aspect     ix.pg_aspect_e not null references ix.pg_aspect,
    content    varchar  not null,
    primary key (node_id, kind, aspect)
);

-- Подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче.
-- Используется word_similarity (операторы <% и <<->), а не similarity
-- (% и <->). Порог <% по умолчанию 0.6, для коротких имён нужен 0.4.
--
-- set pg_trgm.word_similarity_threshold = 0.4;
-- select node_id, kind, content
-- from   ix.pg_trgm
-- where  aspect = 'words' and 'ordrs' <% content
-- order by 'ordrs' <<-> content
-- limit  20;
create index if not exists pg_trgm__content__gist on ix.pg_trgm using gist (content gist_trgm_ops);

-- Точное совпадение без учёта регистра.
--
-- select node_id, kind from ix.pg_trgm
-- where  aspect = 'path' and lower(content) = lower('dm.fact_orders');
create index if not exists pg_trgm__aspect_lower_content on ix.pg_trgm using btree (aspect, lower(content));

-- Подсказка при наборе по префиксу. Обычный btree по lower(content) для
-- префикса не годится, нужен класс операторов varchar_pattern_ops. Вместо
-- like используется оператор ^@ (starts with): в like подчёркивание значит
-- «любой символ», и имя fact_orders пришлось бы экранировать.
--
-- select node_id, kind, content from ix.pg_trgm
-- where  aspect = 'name' and lower(content) ^@ lower('fact_ord')
-- limit  20;
create index if not exists pg_trgm__aspect_lower_content__prefix
    on ix.pg_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists pg_trgm__kind_aspect on ix.pg_trgm using btree (kind, aspect);

-- Векторный поиск по embedding-модели e5 размерностью 1024, строка на аспект.
-- Все surface пишут description; comment пишут те, у кого он не пуст;
-- columns пишут таблица и представление; summary пишут таблица, колонка,
-- представление и подпрограмма, когда описание от LLM есть. Текст кодируется
-- с префиксом passage:, запрос с префиксом query:. content это закодированный
-- текст аспекта: если он не изменился, модель повторно не запускают.
create table if not exists ix.pg_emb_e5_1024 (
    node_id       bigint        not null references ix.node on delete cascade,
    kind          ix.node_kind_e not null references ix.node_kind,
    aspect        ix.pg_aspect_e not null references ix.pg_aspect,
    content       varchar       not null,
    emb           halfvec(1024) not null,
    primary key (node_id, kind, aspect)
);

-- Частичный HNSW на каждую существующую пару kind + aspect. HNSW отдаёт
-- k ближайших из своего индекса, и фильтр по общему индексу после обхода
-- усекал бы выдачу; с частичными индексами фильтр по kind и aspect попадает
-- в свой индекс. kind и aspect в предикате это значения enum, они следуют
-- за переименованием в словаре.
--
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
-- Источник Confluence, проверено по REST API cwiki.apache.org. Node бывает
-- четырёх kind: спейс, страница (страница и блог-запись это один kind,
-- различаются колонкой content_type), вложение и комментарий. Пользователи
-- и метки node не становятся: метки это атрибут страницы. Адрес node по kind:
-- confluence_space       https://host/confluence/rest/api/space/FLINK
-- confluence_page        https://host/confluence/rest/api/content/307136992
-- confluence_comment     https://host/confluence/rest/api/content/127405740
-- confluence_attachment
--     https://host/confluence/download/attachments/307136992/design.pdf
--
-- tree: спейс -> страницы без ancestors (домашняя, корневые, блог-записи) ->
-- дочерние страницы (родитель это последний элемент ancestors) -> вложения
-- и комментарии страницы.
-- edge: refers_to от страницы к странице или вложению по гиперссылке в теле
-- (origin declared) и от страницы к таблице по идентификатору в тексте
-- (origin text_match).
-- Ссылки берутся из body.view, а не из body.storage: макросы (cql, toc,
-- children) разворачиваются только там; на странице-оглавлении storage даёт
-- 4 ссылки, view 184. Внутренняя ссылка бывает по id
-- (/spaces/KEY/pages/ID/..., viewpage.action?pageId=ID) и по заголовку
-- (/display/KEY/Title, ri:page); заголовок разрешается в node по индексу
-- confluence_page (space_key, title). Внешние ссылки отбрасываются, node
-- для них не создаётся.
--
-- Повторный прогон отсекает работу на двух уровнях. version из Confluence
-- отсекает скачивание: если номер не изменился, объект не трогается.
-- content_hash отсекает переиндексацию: объект скачан и разобран, но хэш
-- совпал с сохранённым, и поисковые строки остаются прежними (version растёт
-- и при смене меток или ограничений доступа, текст при этом тот же). Если
-- хэш не совпал, строки всех aspect этой node удаляются и пишутся заново
-- одной транзакцией, эмбеддинги считаются заново. Что именно хэшируется,
-- сказано у каждой surface.
--
-- Оригиналы не хранятся: ни тело страницы, ни файл вложения, ни текст
-- комментария. Адрес объекта хранится только в ix.node.address (REST API),
-- surface его не дублирует. Ссылка для человека строится из адреса
-- (/pages/viewpage.action?pageId=ID), а адрес вложения и есть ссылка на
-- скачивание. Surface хранит идентификаторы и метаданные, оригинал LLM
-- читает по адресу сама. Текст, извлечённый индексатором (тело страницы,
-- разбор pdf и docx, OCR картинки, описание картинки от LLM), живёт только
-- в поисковых таблицах как content своего aspect: это индекс, а не копия.
-- ============================================================================

-- Surface confluence_summary: описание страницы или вложения, которое
-- сгенерировал LLM (describer). Устроена как pg_summary: indexer_hash это
-- снимок настроек прогона, content_hash это хэш текста, из которого пишутся
-- поисковые строки aspect summary.
create table if not exists ix.confluence_summary (
    node_id       bigint      primary key references ix.node on delete cascade,
    content       varchar     not null,
    content_hash  bytea       not null,
    indexer_hash  bytea       not null,
    created_at    timestamptz not null default now()
);

create index if not exists confluence_summary__indexer_hash on ix.confluence_summary using btree (indexer_hash);

-- Surface confluence_space: ключ, имя, тип, статус и описание спейса.
create table if not exists ix.confluence_space (
    node_id      bigint  primary key references ix.node on delete cascade,
    space_key    varchar not null,
    name         varchar not null,
    space_type   varchar not null,
    status       varchar not null,
    description  varchar not null default ''
);

-- Surface confluence_page: метаданные страницы или блог-записи.
-- content_type = page | blogpost, status = current | archived | trashed.
-- version это номер версии в Confluence (version.number): если он не
-- изменился с прошлого прогона, индексатор страницу пропускает. created_at
-- и author берутся из history, updated_at и last_editor из version. Тело
-- страницы здесь не хранится: индексатор берёт body.view (отрендеренный HTML
-- с раскрытыми макросами), снимает теги в коде и кладёт текст в aspect body
-- поисковых таблиц. content_hash = sha256 этого текста вместе с заголовком
-- и метками. ancestor_titles это путь заголовков от корня спейса до
-- родителя, для хлебной крошки в выдаче.
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

-- Разрешение ссылки по заголовку (/display/KEY/Title) в node.
create index if not exists confluence_page__space_key_title on ix.confluence_page using btree (space_key, title);

-- Surface confluence_attachment: метаданные вложения. Сам файл не хранится:
-- индексатор скачивает его, извлекает текст и файл отбрасывает. Какие aspect
-- получаются, зависит от типа файла: разбор pdf и docx идёт в body, OCR
-- картинки в ocr, описание картинки от LLM в vision. content_hash это хэш
-- байтов файла, а не извлечённого текста: OCR и описание от LLM
-- недетерминированы.
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

-- Surface confluence_comment: метаданные комментария к странице.
-- location = inline | footer; у комментария свои version и author. Текст
-- берётся из body.storage, теги снимаются в коде, и живёт в aspect body;
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

-- Aspect источника Confluence: enum ix.confluence_aspect_e, описания значений
-- в словаре ix.confluence_aspect. Что попадает в каждый aspect:
-- description  описание, которое собрал индексатор. У страницы это title,
--              метки, путь заголовков и начало body; у вложения title,
--              media_type и начало извлечённого текста; у спейса name
--              и description.
-- body         полный текст: тело страницы или извлечённый текст вложения.
--              В confluence_fts лежит целиком, в confluence_emb_e5_1024
--              порезан на куски по окну модели, кусок нумерует chunk_no.
-- summary      описание от LLM из confluence_summary; пишется, если оно есть.
-- labels       метки страницы через пробел.
-- name         заголовок страницы, имя файла вложения или имя спейса как есть.
-- path         space_key || '/' || title, для точного совпадения.
-- words        слова из name: разрезан по CamelCase, дефисам и
--              подчёркиваниям, в нижнем регистре, ё -> е; для поиска
--              с опечатками.
-- ocr          текст, распознанный на картинке или скане (вложения image/*
--              и pdf без текстового слоя).
-- vision       смысл картинки, описанный LLM по изображению: что на схеме,
--              какие таблицы и системы на ней названы.
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

-- Полнотекстовый индекс. Каждый kind пишет в aspect description один
-- tsvector с весами. У confluence_page вес A получают words заголовка,
-- B получают labels, C получает body. У confluence_attachment A получают
-- words имени файла, C получают body, ocr и vision. У confluence_space
-- A получают words имени, B получает description. У confluence_comment
-- C получает body. Summary от LLM это отдельная строка с aspect summary из
-- confluence_summary и весом D, как в pg_fts.
create table if not exists ix.confluence_fts (
    node_id    bigint   not null references ix.node on delete cascade,
    kind       ix.node_kind_e         not null references ix.node_kind,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    content    varchar  not null,
    tsv        tsvector not null,
    primary key (node_id, kind, aspect)
);

create index if not exists confluence_fts__kind_tsv__gin on ix.confluence_fts using gin (kind, tsv);

-- Триграммы для поиска по имени. Спейс, страница и вложение пишут aspect
-- name и words, страница и вложение ещё path. У комментария имени нет,
-- в эту таблицу он не пишется.
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

-- Векторный поиск на эмбеддингах e5 размерности 1024. Текст страницы длиннее
-- окна модели (512 токенов), поэтому aspect body режется на куски
-- с перекрытием, и в первичном ключе есть chunk_no; у aspect, который
-- помещается в один кусок, chunk_no = 0. Страница пишет description и body,
-- комментарий body, вложение body или ocr и vision в зависимости от типа
-- файла, спейс description; summary пишет любой kind, у которого оно есть.
create table if not exists ix.confluence_emb_e5_1024 (
    node_id    bigint        not null references ix.node on delete cascade,
    kind       ix.node_kind_e         not null references ix.node_kind,
    aspect     ix.confluence_aspect_e not null references ix.confluence_aspect,
    chunk_no   smallint      not null,
    content    varchar       not null,
    emb        halfvec(1024) not null,
    primary key (node_id, kind, aspect, chunk_no)
);

-- Частичный HNSW на каждую пару kind + aspect, по которой ищут: description,
-- body, ocr, vision и summary.
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
