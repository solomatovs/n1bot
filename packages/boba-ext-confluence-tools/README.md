# boba-ext-confluence-tools

Online-tools для работы с Confluence через REST API. Все tools идут к
живой инстанции Confluence — workspace и локальные файлы не используются.

| tool | назначение |
|---|---|
| `confluence_search` | поиск страниц по тексту (CQL `text ~ "..."`) |
| `confluence_page_outline` | структура заголовков конкретной страницы |
| `confluence_page_section` | текст одной секции страницы по anchor |

## Установка

```bash
pip install -e ./packages/boba-ext-confluence-tools
```

Регистрируется через entry-point `boba.tools` под id `builtin.confluence`.

## Подключение

```toml
[ext.confluence]
enable = true
# tools_allow = ["confluence_search"]   # пусто = все tools пакета

[ext.confluence.search]
base_url = "https://confluence.example.com"
auth_method = "pat"
auth_token = "..."

[ext.confluence.page]
base_url = "https://confluence.example.com"
auth_method = "pat"
auth_token = "..."
body_format = "view"   # или export_view / storage
```

Секреты лучше держать в `.env`:

```bash
BOBA_EXT__CONFLUENCE__SEARCH__BASE_URL=https://confluence.example.com
BOBA_EXT__CONFLUENCE__SEARCH__AUTH_TOKEN=...
BOBA_EXT__CONFLUENCE__PAGE__BASE_URL=https://confluence.example.com
BOBA_EXT__CONFLUENCE__PAGE__AUTH_TOKEN=...
```

## Workflow для агента

1. `confluence_search(query="...", limit=5)` → массив hits с `page_id`/`url`/`excerpt`.
2. `confluence_page_outline(page_id="...", max_headings=50)` → структура секций c `anchor`.
3. `confluence_page_section(page_id="...", anchor="...", max_chars=5000)` → текст одной секции.
