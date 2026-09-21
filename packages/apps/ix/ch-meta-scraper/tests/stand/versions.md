# Матрицы стенда: что отдали версии

Строки каталога, снятые файлами scrape на демонстрационном наборе edge_demo (ddl/), по целям стенда. SKIP значит, что ни один вариант файла к версии не применим.

| name | 22.12 | 23.12 | 24.12 | 25.12 | 26.6 |
|---|---|---|---|---|---|
| server | 1 | 1 | 1 | 1 | 1 |
| databases | 2 | 2 | 2 | 2 | 2 |
| tables | 10 (`__lt26_6`) | 10 (`__lt26_6`) | 10 (`__lt26_6`) | 10 (`__lt26_6`) | 10 |
| columns | 33 | 33 | 33 | 33 | 33 |
| indices | 2 | 2 | 2 | 2 | 2 |
| projections | SKIP | SKIP | 1 | 1 | 1 |
| dictionaries | 1 | 1 | 1 | 1 | 1 |
| functions | 1 | 1 | 1 | 1 | 1 |

Раскладка того же снятия в ix: число node и ролей рёбер. Проекция появляется с 24.4 (system.projections), роль target с 26.6 (system.tables.target_*).

| цель | node | edge | ch_meta_edge | роли | merge |
|---|---|---|---|---|---|
| ch-22.12 | 49 | 9 | 16 | partition_key 1, sorting_key 6, primary_key 5, sampling_key 1, dependency 2, loading 1 | planned=applied |
| ch-23.12 | 49 | 9 | 16 | те же | planned=applied |
| ch-24.12 | 50 | 9 | 16 | те же | planned=applied |
| ch-25.12 | 50 | 9 | 16 | те же | planned=applied |
| ch-26.7 (26.6.1.1) | 50 | 11 | 18 | те же плюс target 2 | planned=applied |

Отпечатки в cons/golden.txt совпадают у 22.12 и 23.12 и у 24.12 и 25.12: набор node, tree, рёбер и поверхностей от версии не зависит, различие только в проекции (с 24.4) и роли target (с 26.6).

Разница между 22.12 и 26.6 в system-таблицах, которая скраперу важна: `system.projections` появилась в 24.x, `target_database`/`target_table` в `system.tables` в 26.6; остальные снимаемые колонки есть во всех пяти версиях.
