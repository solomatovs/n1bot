/*
ix-trgm, схема: таблица {schema}.ix_trgm с индексами.
*/
create table if not exists {schema}.ix_trgm (
    node_id  bigint not null,
    surface  {schema}.surface_e not null references {schema}.surface,
    aspect   {schema}.aspect_e not null references {schema}.aspect,
    content  varchar not null,
    primary key (node_id, surface, aspect)
);

/*
Подстрока и опечатки по всему источнику, таблицы и колонки в одной выдаче.
Используется word_similarity (операторы <% и <<->), а не similarity
(% и <->). Порог <% по умолчанию 0.6, для коротких имён нужен 0.4.

set pg_trgm.word_similarity_threshold = 0.4;
select node_id, surface, content
from   {schema}.ix_trgm
where  aspect = 'meta_words' and 'ordrs' <% content
order by 'ordrs' <<-> content
limit  20;
*/
create index if not exists ix_trgm__content__gist on {schema}.ix_trgm using gist (content gist_trgm_ops);

/*
Точное совпадение без учёта регистра.

select node_id, surface from {schema}.ix_trgm
where  aspect = 'meta_path' and lower(content) = lower('dm.fact_orders');
*/
create index if not exists ix_trgm__aspect_lower_content on {schema}.ix_trgm using btree (aspect, lower(content));

/*
Подсказка при наборе по префиксу. Обычный btree по lower(content) для
префикса не годится, нужен класс операторов varchar_pattern_ops. Вместо
like используется оператор ^@ (starts with): в like подчёркивание значит
«любой символ», и имя fact_orders пришлось бы экранировать.

select node_id, surface, content from {schema}.ix_trgm
where  aspect = 'meta_name' and lower(content) ^@ lower('fact_ord')
limit  20;
*/
create index if not exists ix_trgm__aspect_lower_content__prefix
    on {schema}.ix_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists ix_trgm__surface_aspect on {schema}.ix_trgm using btree (surface, aspect);

/*
Таблица в реестре индексов ядра: поиск читает реестр и не знает имён таблиц. Модель и
размерность вектора в реестр не идут — они живут в конфиге владельца.
*/
insert into {schema}.index_table (kind, name, owner) values
    ('trgm', 'ix_trgm', 'ix-trgm')
on conflict (kind, name) do update
    set owner = excluded.owner;
