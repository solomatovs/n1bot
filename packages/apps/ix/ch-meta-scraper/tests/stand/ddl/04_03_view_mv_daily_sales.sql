create materialized view edge_demo.mv_daily_sales to edge_demo.daily_sales as
select toDate(created_at) as day, sum(amount) as total
from edge_demo.orders
group by day
