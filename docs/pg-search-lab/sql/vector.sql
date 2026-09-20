/*
Вектор: ранг node это лучший чанк среди её аспектов; %(v)s это вектор запроса от embed_query
(префикс query: подставляет провайдер). Расстояние косинусное, меньше значит ближе.
*/
with hit as (
    select e.node_id, e.aspect, e.chunk_no, e.content, e.emb <=> %(v)s::halfvec(1024) as dist
    from ix.pg_emb_e5_1024 e
    order by e.emb <=> %(v)s::halfvec(1024)
    limit %(limit)s * 8
)
select n.surface, n.address, min(h.dist) as score,
       (array_agg(h.aspect order by h.dist))[1] as aspect,
       (array_agg(h.content order by h.dist))[1] as snippet
from hit h
join ix.node n on n.id = h.node_id
group by n.id, n.surface, n.address
order by score
limit %(limit)s;
