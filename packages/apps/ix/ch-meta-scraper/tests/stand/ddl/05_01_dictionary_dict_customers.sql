create dictionary edge_demo.dict_customers (
    id       UInt64,
    name     String,
    country  String
)
primary key id
source(clickhouse(table 'customers' db 'edge_demo'))
layout(hashed())
lifetime(min 60 max 300)
comment 'Словарь клиентов'
