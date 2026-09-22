/*
Полнотекст по одной таблице индекса: её имя подставляется вместо {index} из реестра
{schema}.index_table, поэтому запрос не знает ни происхождений, ни поверхностей.
Ранг node это сумма рангов её строк по всем аспектам, сниппет из лучшей строки.
Поверхности и аспекты задаёт вызывающий списками %(surfaces)s и %(aspects)s; списки
никогда не пусты: когда выбор не сделан, вызывающий подставляет все имена словаря,
чтобы фильтр в sql был один и тот же.
*/
with q as (
    select websearch_to_tsquery('russian', %(q)s) as tsq
),
hit as (
    select
        f.node_id,
        f.aspect,
        ts_rank_cd(f.tsv, q.tsq) as rank,
        ts_headline(
            'russian', f.content, q.tsq, 'MaxWords=24, MinWords=8'
        ) as snippet
    from
        {schema}.{index} f, q
    where 1=1
        and f.surface = any(%(surfaces)s::{schema}.surface_e[])
        and f.aspect = any(%(aspects)s::{schema}.aspect_e[])
        and f.tsv @@ q.tsq
)
select
    n.id as node_id,
    n.surface,
    n.address,
    sum(h.rank) as score,
    (array_agg(h.aspect order by h.rank desc))[1] as aspect,
    (array_agg(h.snippet order by h.rank desc))[1] as snippet,
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
