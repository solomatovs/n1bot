/*
Триграммы по одной таблице индекса: имя подставляется вместо {index} из реестра.
Похожесть слова запроса на содержимое строки; префикс отдельно не ищется, его
покрывает word_similarity. Порог 0.3 задаётся здесь же.
*/
with hit as (
    select
        t.node_id,
        t.aspect,
        t.content,
        word_similarity(%(q)s, t.content) as sim
    from
        {schema}.{index} t
    where
        word_similarity(%(q)s, t.content) >= 0.3
)
select
    n.surface,
    n.address,
    max(h.sim) as score,
    (array_agg(h.aspect order by h.sim desc))[1] as aspect,
    (array_agg(h.content order by h.sim desc))[1] as snippet,
    1 as objects
from
    hit h
    join {schema}.node n on n.id = h.node_id
group by
    n.id, n.surface, n.address
order by
    score desc
limit
    %(limit)s;
