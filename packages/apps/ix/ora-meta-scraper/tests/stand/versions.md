# Матрицы стенда: что отдали версии

Строки словаря, снятые файлами scrape на демонстрационном наборе EDGE_DEMO (ddl/), по целям
стенда. На 12.2 (контейнер `oracle`) кроме EDGE_DEMO стоят демо-схемы Oracle (HR, OE, SH,
PM, IX, SCOTT), поэтому строк там больше; партиционированная таблица `sales` (05_partitioned.sql,
`@min 18`) на 12.2 EE без опции Partitioning не создаётся.

| name | 12.2 | 18 | 21 | 23 |
|---|---|---|---|---|
| database | 1 | 1 | 1 | 1 |
| users | 14 | 4 | 4 | 3 |
| tablespaces | 5 | 7 | 7 | 7 |
| objects | 239 | 32 | 32 | 32 |
| tables | 52 | 6 | 6 | 6 |
| columns | 706 | 39 | 39 | 39 |
| comments | 176 | 8 | 8 | 8 |
| con | 199 | 28 | 28 | 28 |
| cdef | 199 | 28 | 28 | 28 |
| ccol | 215 | 29 | 29 | 29 |
| indexes | 112 | 13 | 13 | 13 |
| icol | 118 | 15 | 15 | 15 |
| views | 17 | 2 | 2 | 2 |
| mviews | 3 | 1 | 1 | 1 |
| sequences | 7 | 2 | 2 | 2 |
| synonyms | 8 | 2 | 2 | 2 |
| triggers | 7 | 2 | 2 | 2 |
| dependencies | 129 | 12 | 12 | 12 |
| partobj | 2 | 1 | 1 | 1 |
| partcol | 2 | 1 | 1 | 1 |

Раскладка набора EDGE_DEMO в ix на 23: node по поверхностям и роли рёбер.

| surface | node |
|---|---|
| ora_meta_schema | 1 |
| ora_meta_table | 5 (customers, orders, order_items IOT, order_staging temporary, sales partitioned) |
| ora_meta_view | 2 |
| ora_meta_mview | 1 |
| ora_meta_column | 36 |
| ora_meta_constraint | 10 (P 4, U 1, R 2, C 2, V 1) |
| ora_meta_index | 11 |
| ora_meta_sequence | 2 (customer_seq и ISEQ$$ identity) |
| ora_meta_synonym | 2 |
| ora_meta_trigger | 2 |
| ora_meta_routine | 4 (procedure, function, package, type) |

Роли рёбер: index и constraint по позициям колонок, partition_key у sales, dependency у
представлений, mview, синонимов и подпрограмм, synonym у обоих синонимов. Системные таблицы
словаря и их колонки одинаковы на всех четырёх версиях: вариантов файлов scrape нет,
все двадцать применяются без ворот.

Отпечатки в cons/golden.txt различаются между 18, 21 и 23 только строкой ora_meta_schema
(на 18 и 21 есть схема OPS$ORACLE, на 23 её нет) и порядком системных объектов; системные
имена SYS_C<n> и ISEQ$$_<n> в отпечатке нормализованы.
