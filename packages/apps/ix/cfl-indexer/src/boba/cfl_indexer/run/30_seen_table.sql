/*
cfl-indexer: увиденные за прогон node живут в temp-таблице сессии, а не в памяти;
чистка спейса читает её anti-join'ом. Соединение из пула переживает несколько спейсов,
поэтому таблица создаётся один раз и очищается перед каждым обходом.
*/
-- @name seen_table
create temp table if not exists seen (
    node_id  bigint primary key
);
truncate seen;
