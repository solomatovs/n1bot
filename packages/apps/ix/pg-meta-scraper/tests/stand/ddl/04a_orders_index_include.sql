-- @min 110000
create index orders__customer_created on dm.orders (customer_id, created_at desc) include (amount);
