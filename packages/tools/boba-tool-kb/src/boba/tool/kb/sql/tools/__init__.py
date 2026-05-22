"""SQL-tools: ad-hoc read-only SQL-tools для exploratory-аналитики.

- `sql_list_tables(schema=None)`      — список таблиц/view.
- `sql_describe_table(table, schema)` — колонки/типы.
- `sql_query(query, row_limit)`       — произвольный SELECT → markdown-таблица.

Безопасность — DSN-роль + libpq session-options (`default_transaction_read_only=on`,
`statement_timeout=...`). См. docstring пакета `sql/` для деталей.
"""
