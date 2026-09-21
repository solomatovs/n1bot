/*
ix-llm-describer, шаг 1: очередь на описание. Текст структуры объекта, который уйдёт в
модель, отдаёт поверхность аспектом класса describer_input; источник подставляется вместо
плейсхолдера sources из объявлений {schema}.surface_aspect. input_hash это md5 этого
текста. В очередь попадают объекты без описания, с другим input_hash или с другим
indexer_hash (сменили модель или промпт). Каждый выданный объект захвачен сессионным
advisory-замком, чужие пропускаются.
*/
-- @name queue
-- @params batch indexer_hash
with input as (
    {sources}
),
todo as (
    select
        i.node_id,
        i.surface,
        i.content as text,
        md5(i.content) as input_hash
    from
        input i
        left join {schema}.llm_description s on s.node_id = i.node_id
    where
        s.node_id is null
        or s.input_hash <> md5(i.content)
        or s.indexer_hash <> %(indexer_hash)s
    order by
        i.node_id
    limit
        %(batch)s * 4
)
select
    node_id, surface, text, input_hash
from
    todo
where
    pg_try_advisory_lock(hashtextextended('llm_description', node_id))
limit
    %(batch)s;
