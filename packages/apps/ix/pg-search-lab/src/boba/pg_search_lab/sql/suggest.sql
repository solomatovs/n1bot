/*
Подсказки при наборе по всем происхождениям: сначала совпадение по префиксу
идентификатора через btree (varchar_pattern_ops, оператор ^@), затем похожие слова
через триграммы с порогом 0.4. Какие аспекты идентификаторы, а какие слова, говорит
класс в словаре аспектов, а не имя: подсказки не знают, откуда пришла поверхность.
Префиксные попадания получают счёт 1 и идут выше триграммных. Подсказка это текст, а не
объект: одинаковые имена схлопнуты, число объектов в скобках.
*/
with q as (
    select lower(%(q)s) as text
),
idx as (
    select node_id, aspect, content from {schema}.pg_idx_trgm
    union all
    select node_id, aspect, content from {schema}.cfl_idx_trgm
),
prefix as (
    select
        t.node_id,
        t.aspect,
        t.content,
        1.0::float8 as score
    from
        idx t
        join {schema}.aspect a on a.aspect = t.aspect,
        q
    where
        a.class = 'ident'
        and lower(t.content) ^@ q.text
),
fuzzy as (
    select
        t.node_id,
        t.aspect,
        t.content,
        word_similarity(q.text, t.content) as score
    from
        idx t
        join {schema}.aspect a on a.aspect = t.aspect,
        q
    where
        a.class = 'words'
        and word_similarity(q.text, t.content) >= 0.4
),
hit as (
    select * from prefix
    union all
    select * from fuzzy
)
select
    (array_agg(n.surface order by h.score desc))[1] as surface,
    (array_agg(n.address order by h.score desc))[1] as address,
    max(h.score) as score,
    h.aspect,
    case
        when count(distinct h.node_id) > 1
        then h.content || ' (' || count(distinct h.node_id) || ' objects)'
        else h.content
    end as snippet
from
    hit h
    join {schema}.node n on n.id = h.node_id
group by
    h.aspect, h.content
order by
    score desc, h.content
limit
    %(limit)s;
