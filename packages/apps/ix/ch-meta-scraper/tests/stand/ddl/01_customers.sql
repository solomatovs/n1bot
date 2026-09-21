create table edge_demo.customers (
    id          UInt64,
    email       String comment 'Почта клиента',
    name        String,
    country     LowCardinality(String) codec(ZSTD(3)),
    created_at  DateTime default now()
)
engine = MergeTree
order by id
comment 'Клиенты';
