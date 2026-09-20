/*
Подсказки при наборе: сначала совпадение по префиксу имени или пути через btree
(varchar_pattern_ops, оператор ^@), затем похожие слова через триграммы с порогом 0.4.
Префиксные попадания получают счёт 1 и идут выше триграммных. Подсказка это текст, а не
объект: одинаковые имена схлопнуты, число объектов в скобках.
*/
with q as (select lower(%(q)s) as text),
prefix as (
    select t.node_id, t.aspect, t.content, 1.0::float8 as score
    from ix.pg_trgm t, q
    where t.aspect in ('name', 'path') and lower(t.content) ^@ q.text
),
fuzzy as (
    select t.node_id, t.aspect, t.content, word_similarity(q.text, t.content) as score
    from ix.pg_trgm t, q
    where t.aspect = 'words' and word_similarity(q.text, t.content) >= 0.4
),
hit as (select * from prefix union all select * from fuzzy)
select (array_agg(n.surface order by h.score desc))[1] as surface,
       (array_agg(n.address order by h.score desc))[1] as address,
       max(h.score) as score, h.aspect,
       case when count(distinct h.node_id) > 1 then h.content || ' (' || count(distinct h.node_id) || ' objects)' else h.content end as snippet
from hit h
join ix.node n on n.id = h.node_id
group by h.aspect, h.content
order by score desc, h.content
limit %(limit)s;
