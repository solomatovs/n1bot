create or replace view edge_demo.customer_orders as
select
    c.id as customer_id,
    c.email,
    count(o.id) as orders_count,
    sum(o.amount) as total_amount
from
    edge_demo.customers c
    left join edge_demo.orders o on o.customer_id = c.id
group by
    c.id, c.email
