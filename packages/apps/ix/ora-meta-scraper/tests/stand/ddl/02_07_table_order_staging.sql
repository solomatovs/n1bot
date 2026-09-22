create global temporary table edge_demo.order_staging (
    order_id  number(12),
    note      varchar2(400)
) on commit delete rows
