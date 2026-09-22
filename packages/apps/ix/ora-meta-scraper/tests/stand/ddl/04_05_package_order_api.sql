create or replace package edge_demo.order_api as
    procedure close_all;
    function count_open return number;
end order_api;
