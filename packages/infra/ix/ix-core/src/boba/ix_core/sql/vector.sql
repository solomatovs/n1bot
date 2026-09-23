/*
Вектор по таблицам эмбеддингов: вместо {index} подставляется их объединение из реестра,
таблицы той же модели и размерности, что у эмбеддера вызывающего. Ранг
node это лучший чанк среди её аспектов; %(v)s это вектор запроса от embed_query
(префикс query: подставляет провайдер). Расстояние косинусное, меньше значит ближе.
Поверхности и аспекты задаёт вызывающий списками %(surfaces)s и %(aspects)s; списки
никогда не пусты: когда выбор не сделан, вызывающий подставляет все имена словаря.
*/
with hit as (
    select
        e.node_id,
        e.aspect,
        e.content,
        e.emb <=> %(v)s::halfvec as dist
    from
        {index} e
    where 1=1
        and e.surface = any(%(surfaces)s::{schema}.surface_e[])
        and e.aspect = any(%(aspects)s::{schema}.aspect_e[])
    order by
        e.emb <=> %(v)s::halfvec
    limit
        (%(limit)s + %(offset)s) * 8
)
select
    n.id as node_id,
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
    %(limit)s
offset
    %(offset)s;
