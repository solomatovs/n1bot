create table edge_demo.orders (
    id           UInt64,
    customer_id  UInt64 comment 'Клиент, см. customers',
    amount       Decimal(18, 2),
    status       Enum8('open' = 1, 'paid' = 2, 'cancelled' = 3) default 'open',
    created_at   DateTime,
    day          Date materialized toDate(created_at),
    amount_rub   Decimal(18, 2) alias amount * 90,
    index idx_status status type set(3) granularity 4,
    index idx_amount amount type minmax granularity 1,
    projection p_by_status (
        select status, count() group by status
    )
)
engine = MergeTree
partition by toYYYYMM(created_at)
primary key (customer_id, intHash32(customer_id))
order by (customer_id, intHash32(customer_id), created_at)
sample by intHash32(customer_id)
ttl created_at + interval 3 year
settings index_granularity = 8192
comment 'Заказы'
