/*
cfl-indexer, индекс node, шаг 3: аспекты из объявлений в полнотекст. Источник sources
это union объявлений поверхностей cfl_*, собранный при старте; вес из конфига по имени
аспекта, без веса D. Строки, положенные шагом push, не перезаписываются.
*/
-- @name index_derive
-- @params node_id
insert into {schema}.cfl_idx_fts
    (node_id, surface, aspect, content, tsv)
select
    a.node_id,
    a.surface,
    a.aspect,
    a.content,
    setweight(to_tsvector('russian', a.content), coalesce(w.weight, 'D')::"char")
from
    ({sources}) a
    left join ({weights}) as w(aspect, weight) on w.aspect = a.aspect
where
    a.node_id = %(node_id)s
on conflict (node_id, surface, aspect) do nothing;
