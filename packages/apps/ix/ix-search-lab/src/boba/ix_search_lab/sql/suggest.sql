/*
Подсказки при наборе по одной таблице триграмм: имя подставляется вместо {index} из
реестра. Сначала совпадение по префиксу идентификатора через btree
(varchar_pattern_ops, оператор ^@), затем похожие слова через триграммы с порогом 0.4.
Какие аспекты идентификаторы, а какие слова, говорит класс в словаре аспектов, а не
имя: подсказки не знают ни происхождений, ни поверхностей. Префиксные попадания
получают счёт 1 и идут выше триграммных. Подсказка это текст, а не объект: одинаковые
имена схлопнуты, число объектов отдаётся колонкой objects и склеивается по таблицам
на стороне стенда.
Поверхности задаёт вызывающий списком %(surfaces)s; список никогда не пуст: когда
на странице не выбрано ничего, стенд подставляет все поверхности словаря.
*/
with q as (
    select lower(%(q)s) as text
),
prefix as (
    select
        t.node_id,
        t.aspect,
        t.content,
        1.0::float8 as score
    from
        {schema}.{index} t
        join {schema}.aspect a on a.aspect = t.aspect,
        q
    where 1=1
        and t.surface = any(%(surfaces)s::{schema}.surface_e[])
        and a.class = 'ident'
        and lower(t.content) ^@ q.text
),
fuzzy as (
    select
        t.node_id,
        t.aspect,
        t.content,
        word_similarity(q.text, t.content) as score
    from
        {schema}.{index} t
        join {schema}.aspect a on a.aspect = t.aspect,
        q
    where 1=1
        and t.surface = any(%(surfaces)s::{schema}.surface_e[])
        and a.class = 'words'
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
    h.content as snippet,
    count(distinct h.node_id) as objects
from
    hit h
    join {schema}.node n on n.id = h.node_id
group by
    h.aspect, h.content
order by
    score desc, h.content
limit
    %(limit)s;
