create table edge_demo.products (
    id       UInt64,
    sku      String,
    price    Decimal(10, 2),
    version  UInt32
)
engine = ReplacingMergeTree(version)
order by id;

create table edge_demo.events_log (
    ts   DateTime,
    msg  String
)
engine = Log;
