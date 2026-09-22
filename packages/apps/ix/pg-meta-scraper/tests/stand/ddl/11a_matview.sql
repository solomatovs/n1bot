create materialized view dm.mv_region_totals as
    select region, sum(amount) as amount
    from dm.v_customer_totals
    group by region;
