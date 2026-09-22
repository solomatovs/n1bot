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
/
comment on table edge_demo.customer_orders is 'Заказы по клиентам'
/
create or replace view edge_demo.open_orders as
select
    o.id, o.customer_id, o.amount, o.created_at
from
    edge_demo.orders o
where
    o.status = 'open'
with check option constraint open_orders_ck
/
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
/
comment on materialized view edge_demo.daily_sales is 'Продажи по дням'
/
create index edge_demo.daily_sales_day_ix on edge_demo.daily_sales (sale_day)
/
create or replace synonym edge_demo.cust for edge_demo.customers
/
create or replace synonym edge_demo.all_sales for edge_demo.daily_sales
/
