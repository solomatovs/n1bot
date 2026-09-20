/*
Триграммы: похожесть слова запроса на name, words, path; префикс по name отдельно не ищется,
он покрывается word_similarity. Порог 0.3 задаётся здесь же.
*/
with hit as (
    select t.node_id, t.aspect, t.content, word_similarity(%(q)s, t.content) as sim
    from ix.pg_idx_trgm t
    where word_similarity(%(q)s, t.content) >= 0.3
)
select n.surface, n.address, max(h.sim) as score,
       (array_agg(h.aspect order by h.sim desc))[1] as aspect,
       (array_agg(h.content order by h.sim desc))[1] as snippet
from hit h
join ix.node n on n.id = h.node_id
group by n.id, n.surface, n.address
order by score desc
limit %(limit)s;
