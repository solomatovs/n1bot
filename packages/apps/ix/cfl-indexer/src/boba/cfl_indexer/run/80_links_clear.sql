/*
cfl-indexer, рёбра, шаг 1: снять прежние ссылки страниц, тела которых перечитаны за
обход; страницы без новых записей в links свои рёбра сохраняют. Отдельным оператором
от вставки: внутри одного оператора вставка не видит удаления и упёрлась бы в
уникальность пары.
*/
-- @name links_clear
delete from {schema}.edge e
where
    e.surface = 'cfl_page_link'
    and e.node_src_id in (select distinct src_node from links);
