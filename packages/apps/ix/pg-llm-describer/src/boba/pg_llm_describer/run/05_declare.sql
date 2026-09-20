/*
pg-llm-describer, шаг 0: объявить аспект llm_description для каждой поверхности, у которой
есть аспект класса describer_input. Тело хранит плейсхолдер схемы, поэтому в файле он
удвоен; потребители подставят его при чтении.
*/
-- @name declare
insert into {schema}.surface_aspect
    (surface, aspect, body)
select
    sa.surface,
    'llm_description',
    'select node_id, content from {{schema}}.pg_llm_description where surface = '
        || quote_literal(sa.surface::varchar)
from
    {schema}.surface_aspect sa
    join {schema}.aspect a on a.aspect = sa.aspect
where
    a.class = 'describer_input'
on conflict (surface, aspect) do update
    set body = excluded.body;
