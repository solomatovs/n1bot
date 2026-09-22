create or replace view edge_demo.open_orders as
select
    o.id, o.customer_id, o.amount, o.created_at
from
    edge_demo.orders o
where
    o.status = 'open'
with check option constraint open_orders_ck
