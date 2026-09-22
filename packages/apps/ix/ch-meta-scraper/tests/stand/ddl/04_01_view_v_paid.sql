create view edge_demo.v_paid as
select id, customer_id, amount, created_at
from edge_demo.orders
where status = 'paid'
