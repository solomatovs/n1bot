/*
Полнотекст: ранг node это сумма рангов её строк по всем аспектам, сниппет из лучшей строки.
Правится без перезапуска сервера: файл читается на каждый запрос.
*/
with q as (select websearch_to_tsquery('russian', %(q)s) as tsq),
hit as (
    select f.node_id, f.aspect, ts_rank_cd(f.tsv, q.tsq) as rank, ts_headline('russian', f.content, q.tsq, 'MaxWords=24, MinWords=8') as snippet
    from {schema}.pg_idx_fts f, q
    where f.tsv @@ q.tsq
)
select n.surface, n.address, sum(h.rank) as score,
       (array_agg(h.aspect order by h.rank desc))[1] as aspect,
       (array_agg(h.snippet order by h.rank desc))[1] as snippet
from hit h
join {schema}.node n on n.id = h.node_id
group by n.id, n.surface, n.address
order by score desc
limit %(limit)s;
