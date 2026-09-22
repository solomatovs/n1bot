create or replace type edge_demo.money_t as object (
    amount    number(18, 2),
    currency  varchar2(3)
)
