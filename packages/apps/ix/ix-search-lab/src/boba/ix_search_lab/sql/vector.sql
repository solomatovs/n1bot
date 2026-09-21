/*
Вектор по одной таблице эмбеддингов: имя подставляется вместо {index} из реестра, а
берутся только таблицы той же модели и размерности, что у эмбеддера стенда. Ранг node
это лучший чанк среди её аспектов; %(v)s это вектор запроса от embed_query (префикс
query: подставляет провайдер). Расстояние косинусное, меньше значит ближе.
Поверхности задаёт вызывающий списком %(surfaces)s; список никогда не пуст: когда
на странице не выбрано ничего, стенд подставляет все поверхности словаря.
*/
with hit as (
    select
        e.node_id,
        e.aspect,
        e.content,
        e.emb <=> %(v)s::halfvec as dist
    from
        {schema}.{index} e
    where
        e.surface::varchar = any(%(surfaces)s::varchar[])
    order by
        e.emb <=> %(v)s::halfvec
    limit
        %(limit)s * 8
)
select
    n.surface,
    n.address,
    min(h.dist) as score,
    (array_agg(h.aspect order by h.dist))[1] as aspect,
    (array_agg(h.content order by h.dist))[1] as snippet,
    1 as objects
from
    hit h
    join {schema}.node n on n.id = h.node_id
group by
    n.id, n.surface, n.address
order by
    score
limit
    %(limit)s;
