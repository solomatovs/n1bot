/*
Триграммы по всем происхождениям: похожесть слова запроса на имя, слова и путь из
pg_idx_trgm и cfl_idx_trgm; префикс отдельно не ищется, его покрывает word_similarity.
Порог 0.3 задаётся здесь же.
*/
with idx as (
    select node_id, aspect, content from {schema}.pg_idx_trgm
    union all
    select node_id, aspect, content from {schema}.cfl_idx_trgm
),
hit as (
    select
        t.node_id,
        t.aspect,
        t.content,
        word_similarity(%(q)s, t.content) as sim
    from
        idx t
    where
        word_similarity(%(q)s, t.content) >= 0.3
)
select
    n.surface,
    n.address,
    max(h.sim) as score,
    (array_agg(h.aspect order by h.sim desc))[1] as aspect,
    (array_agg(h.content order by h.sim desc))[1] as snippet
from
    hit h
    join {schema}.node n on n.id = h.node_id
group by
    n.id, n.surface, n.address
order by
    score desc
limit
    %(limit)s;
