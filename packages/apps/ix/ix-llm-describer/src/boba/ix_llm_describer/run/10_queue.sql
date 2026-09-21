/*
ix-llm-describer, шаг 1: очередь на описание.

Материал объекта отдаёт поверхность аспектом класса describer_input; источник
подставляется вместо плейсхолдера sources из объявлений {schema}.surface_aspect.
Описываются только те пары «поверхность, аспект», у которых есть промпт в
{schema}.surface_prompt: их список приходит параметрами вместе с отпечатком каждой,
и соединение с mix заодно отсекает пары без промпта.

input_hash это md5 материала. В очередь попадают объекты без описания, с другим
input_hash или с другим отпечатком своей пары (сменили модель, промпт, шаблоны свёртки
или бюджет входа). Каждый выданный объект захвачен сессионным advisory-замком, чужие
пропускаются.
*/
-- @name queue
-- @params batch surfaces aspects hashes
with mix as (
    select
        m.surface,
        m.aspect,
        m.indexer_hash
    from
        unnest(
            %(surfaces)s::{schema}.surface_e[],
            %(aspects)s::{schema}.aspect_e[],
            %(hashes)s::varchar[]
        ) as m(surface, aspect, indexer_hash)
),
input as (
    {sources}
),
todo as (
    select
        i.node_id,
        i.surface,
        i.aspect,
        i.content as text,
        md5(i.content) as input_hash
    from
        input i
        join mix m
            on  m.surface = i.surface
            and m.aspect = i.aspect
        left join {schema}.llm_description s on s.node_id = i.node_id
    where
        s.node_id is null
        or s.input_hash <> md5(i.content)
        or s.indexer_hash <> m.indexer_hash
    order by
        i.node_id
    limit
        %(batch)s * 4
)
select
    node_id, surface, aspect, text, input_hash
from
    todo
where
    pg_try_advisory_lock(hashtextextended('llm_description', node_id))
limit
    %(batch)s;
