-- @min 80300
create table dm.orders (
    id           bigserial       primary key,
    customer_id  bigint          not null references dm.customers (id),
    currency     char(3)         not null references ref.currencies (code) deferrable initially deferred,
    line_no      smallint        not null,
    amount       numeric         not null check (amount >= 0),
    status       dm.order_status not null default 'open',
    created_at   timestamptz     not null default now(),
    unique (id, line_no)
);
create index orders__open on dm.orders (created_at) where status = 'open';
