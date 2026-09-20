/*
pg-indexer-trgm, схема: аспекты PostgreSQL (общий словарь, создаётся идемпотентно каждым
индексатором) и таблица ix.pg_trgm с индексами. Предусловие: ядро ix из
docs/knowledge-schema.sql. Внешнего ключа на ix.node нет намеренно.
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

create table if not exists ix.pg_trgm (
    node_id    bigint   not null,
    surface       ix.surface_e not null references ix.surface,
    aspect     ix.pg_aspect_e not null references ix.pg_aspect,
    content    varchar  not null,
    primary key (node_id, surface, aspect)
);

/*
Подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче.
Используется word_similarity (операторы <% и <<->), а не similarity
(% и <->). Порог <% по умолчанию 0.6, для коротких имён нужен 0.4.

set pg_trgm.word_similarity_threshold = 0.4;
select node_id, surface, content
from   ix.pg_trgm
where  aspect = 'words' and 'ordrs' <% content
order by 'ordrs' <<-> content
limit  20;
*/
create index if not exists pg_trgm__content__gist on ix.pg_trgm using gist (content gist_trgm_ops);

/*
Точное совпадение без учёта регистра.

select node_id, surface from ix.pg_trgm
where  aspect = 'path' and lower(content) = lower('dm.fact_orders');
*/
create index if not exists pg_trgm__aspect_lower_content on ix.pg_trgm using btree (aspect, lower(content));

/*
Подсказка при наборе по префиксу. Обычный btree по lower(content) для
префикса не годится, нужен класс операторов varchar_pattern_ops. Вместо
like используется оператор ^@ (starts with): в like подчёркивание значит
«любой символ», и имя fact_orders пришлось бы экранировать.

select node_id, surface, content from ix.pg_trgm
where  aspect = 'name' and lower(content) ^@ lower('fact_ord')
limit  20;
*/
create index if not exists pg_trgm__aspect_lower_content__prefix
    on ix.pg_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists pg_trgm__surface_aspect on ix.pg_trgm using btree (surface, aspect);
