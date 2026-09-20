# pg-indexer-vector: векторный индекс объектов PostgreSQL

Пока только слой схемы: `schema/00_pg_emb_e5_1024.sql` создаёт `ix.pg_emb_e5_1024` и частичные
HNSW-индексы по парам surface и aspect. Воркер (очередь по `content_hash`, запись эмбеддингов,
prune) будет добавлен по тому же контракту, что у `pg-indexer-fts` и `pg-indexer-trgm`: сначала
добавить, удалять последним, определение аспектов продублировано в пакете, поисковые запросы
join'ят `ix.node`.

Предусловие: ядро `ix` из `docs/knowledge-schema.sql`, `docs/pg-scraper/schema/00_surface.sql`
(значения `surface_e` в предикатах индексов), словарь аспектов из схемы любого индексатора.
