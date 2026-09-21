/*
cfl-indexer: ссылки страниц на страницы, снятые из тел за обход; в рёбра они
превращаются после обхода, когда все node спейса уже есть. Таблица сессии,
очищается перед каждым обходом.
*/
-- @name links_table
create temp table if not exists links (
    src_node      bigint not null,
    target_id     varchar not null,
    target_title  varchar not null,
    kind          varchar not null
);
truncate links;
