/*
pg-idx-trgm, схема, шаг 1: словарь аспектов и таблица {schema}.pg_idx_trgm с индексами.
*/
create table if not exists {schema}.pg_idx_aspect (    aspect       {schema}.pg_idx_aspect_e primary key,
    description  varchar        not null
);

insert into {schema}.pg_idx_aspect (aspect, description) values
    ('meta_description', 'описание объекта, собранное индексатором из всего, что о нём известно; основной аспект поиска'),
    ('meta_comment',     'комментарий из источника как есть (obj_description, col_description); пишется, только если не пуст'),
    ('meta_columns',     'имена колонок таблицы через пробел; таблица находится по своим колонкам'),
    ('llm_description',     'описание от LLM (плагин describer); пишется, только когда оно есть'),
    ('meta_name',        'имя объекта как есть (relname, attname); точное совпадение и подстрока'),
    ('meta_path',        'путь через точку, как пишет пользователь: schema.table или schema.table.column; точное совпадение'),
    ('meta_words',       'слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е; поиск с опечатками')
on conflict (aspect) do nothing;

create table if not exists {schema}.pg_idx_trgm (
    node_id    bigint   not null,
    surface       {schema}.surface_e not null references {schema}.surface,
    aspect     {schema}.pg_idx_aspect_e not null references {schema}.pg_idx_aspect,
    content    varchar  not null,
    primary key (node_id, surface, aspect)
);

/*
Подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче.
Используется word_similarity (операторы <% и <<->), а не similarity
(% и <->). Порог <% по умолчанию 0.6, для коротких имён нужен 0.4.

set pg_trgm.word_similarity_threshold = 0.4;
select node_id, surface, content
from   {schema}.pg_idx_trgm
where  aspect = 'meta_words' and 'ordrs' <% content
order by 'ordrs' <<-> content
limit  20;
*/
create index if not exists pg_idx_trgm__content__gist on {schema}.pg_idx_trgm using gist (content gist_trgm_ops);

/*
Точное совпадение без учёта регистра.

select node_id, surface from {schema}.pg_idx_trgm
where  aspect = 'meta_path' and lower(content) = lower('dm.fact_orders');
*/
create index if not exists pg_idx_trgm__aspect_lower_content on {schema}.pg_idx_trgm using btree (aspect, lower(content));

/*
Подсказка при наборе по префиксу. Обычный btree по lower(content) для
префикса не годится, нужен класс операторов varchar_pattern_ops. Вместо
like используется оператор ^@ (starts with): в like подчёркивание значит
«любой символ», и имя fact_orders пришлось бы экранировать.

select node_id, surface, content from {schema}.pg_idx_trgm
where  aspect = 'meta_name' and lower(content) ^@ lower('fact_ord')
limit  20;
*/
create index if not exists pg_idx_trgm__aspect_lower_content__prefix
    on {schema}.pg_idx_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists pg_idx_trgm__surface_aspect on {schema}.pg_idx_trgm using btree (surface, aspect);
