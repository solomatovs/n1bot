create materialized view edge_demo.mv_customer_orders
engine = AggregatingMergeTree
order by customer_id
as
select customer_id, countState() as cnt
from edge_demo.orders
group by customer_id
