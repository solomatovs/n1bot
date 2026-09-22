create materialized view edge_demo.daily_sales
build immediate
refresh complete on demand
as
select
    trunc(o.created_at) as sale_day,
    count(*) as orders_count,
    sum(o.amount) as total_amount
from
    edge_demo.orders o
group by
    trunc(o.created_at)
