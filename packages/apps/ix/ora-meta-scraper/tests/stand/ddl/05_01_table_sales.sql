create table edge_demo.sales (
    id        number(12) not null,
    sold_at   date not null,
    region    varchar2(10) not null,
    amount    number(18, 2) not null,
    constraint sales_pk primary key (id, sold_at)
)
partition by range (sold_at) (
    partition p2024 values less than (to_date('2025-01-01', 'YYYY-MM-DD')),
    partition p2025 values less than (to_date('2026-01-01', 'YYYY-MM-DD')),
    partition pmax values less than (maxvalue)
)
