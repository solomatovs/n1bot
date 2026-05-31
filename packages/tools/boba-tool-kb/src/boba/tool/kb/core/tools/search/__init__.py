"""KB search tools: read-only поиск по `kb_chunks`.

- `kb_search` — tools `kb_confluence_search` / `kb_confluence_doc_search`; единый
  интерфейс с параметром `method` (`vector` cosine | `fts` ts_rank_cd).
- `schema` — дискриминирующие типы по коллекции (`ConfluenceCollection` /
  `KbDocCollection`): строгий scope + детерминированная сборка строки.
"""
