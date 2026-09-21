/*
Полнотекст по одной таблице индекса: её имя подставляется вместо {index} из реестра
{schema}.index_table, поэтому запрос не знает ни происхождений, ни поверхностей.
Ранг node это сумма рангов её строк по всем аспектам, сниппет из лучшей строки.
Правится без перезапуска сервера: файл читается на каждый запрос.
Поверхности задаёт вызывающий списком %(surfaces)s; список никогда не пуст: когда
на странице не выбрано ничего, стенд подставляет все поверхности словаря.
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
        and f.tsv @@ q.tsq
)
select
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
