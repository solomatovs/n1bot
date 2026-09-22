create table edge_demo.daily_sales (
    day    Date,
    total  Decimal(18, 2)
)
engine = SummingMergeTree
order by day
