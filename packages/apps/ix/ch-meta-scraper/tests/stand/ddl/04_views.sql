create view edge_demo.v_paid as
select id, customer_id, amount, created_at
from edge_demo.orders
where status = 'paid';

create table edge_demo.daily_sales (
    day    Date,
    total  Decimal(18, 2)
)
engine = SummingMergeTree
order by day;

create materialized view edge_demo.mv_daily_sales to edge_demo.daily_sales as
select toDate(created_at) as day, sum(amount) as total
from edge_demo.orders
group by day;

create materialized view edge_demo.mv_customer_orders
engine = AggregatingMergeTree
order by customer_id
as
select customer_id, countState() as cnt
from edge_demo.orders
group by customer_id;
