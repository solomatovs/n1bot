-- @min 80300
create view dm.v_orders_daily as
    select date_trunc('day', created_at) as day, currency, sum(amount) as amount
    from dm.orders
    where status <> 'cancelled'
    group by 1, 2;
create view dm.v_customer_totals as
    select c.id as customer_id, c.region, sum(o.amount) as amount
    from dm.customers c
    join dm.orders o on o.customer_id = c.id
    group by c.id, c.region;
create view dm.v_orders_count as select count(*) as n from dm.orders;
