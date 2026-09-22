create or replace package body edge_demo.order_api as
    procedure close_all is
    begin
        update edge_demo.orders set status = 'closed' where status = 'open';
    end;
    function count_open return number is
        v_count number;
    begin
        select count(*) into v_count from edge_demo.orders where status = 'open';
        return v_count;
    end;
end order_api;
