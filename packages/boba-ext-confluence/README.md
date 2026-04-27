# boba-ext-confluence

Навигация по экспортам Confluence-страниц (HTML-формат) внутри workspace.

В отличие от [boba-ext-html](../boba-ext-html), этот пакет понимает специфику
Confluence-разметки:

- **Anchor'ы** извлекаются из `<ac:structured-macro ac:name="anchor">/<ac:parameter>`
  (например `scroll-bookmark-22`), а не только из html-атрибута `id`. Служебный
  `_GoBack` пропускается.
- **Текст заголовков** очищается от содержимого `ac:*`/`ri:*`-макросов — без
  префиксов вида `scroll-bookmark-2Правила именования`.
- **`confluence_section`** по умолчанию вырезает `ac:*`/`ri:*` теги из ответа —
  модели достаётся чистый HTML без шума.

Источник — только локальные файлы workspace (через `ToolContext.project_workspace`),
сетевого доступа к Confluence API нет.

## Установка

```bash
pip install -e ./packages/boba-ext-confluence
```

Регистрируется через entry-point `boba.tools` под id `builtin.confluence`.

## Доступ

В TOML агента:

```toml
[agent]
tools_enabled = true
tools_allow = [
    "ls", "cat", "grep",
    "confluence_outline", "confluence_section",
]
```

## confluence_outline

| поле | обяз. | тип | описание |
|---|---|---|---|
| `path` | ✓ | str | HTML-файл Confluence-export'а |
| `max_depth` |  | int (1..6) | До какого уровня заголовков; пусто — все 6 |
| `limit` |  | int | Потолок количества заголовков (default 200) |

Ответ:
```
Документ: 950276.html  charset=utf-8
Заголовков: 36

  1. h1 Правила именования  #scroll-bookmark-1
  2. h1 Правила именования AD групп  #scroll-bookmark-2
  3.   h2 Группы доступа домены данных УпД  #scroll-bookmark-5
  ...
```

Если у заголовка нет confluence-anchor'а и нет `id` — fallback `#idx:N`.

## confluence_section

| поле | обяз. | тип | описание |
|---|---|---|---|
| `path` | ✓ | str | HTML-файл |
| `anchor` | ✓ | str | `scroll-bookmark-N` или `idx:N` (с/без `#`) |
| `include_subsections` |  | bool | true (default) — включать подзаголовки до следующего ≤-уровня |
| `strip_macros` |  | bool | true (default) — вырезать `ac:*`/`ri:*` теги из вывода |
| `max_chars` |  | int | Лимит длины (default 8000) |

При `strip_macros=true` (по умолчанию):
- удаляются макросы вида `<ac:structured-macro>`, `<ac:link>`, `<ac:image>`,
  `<ri:attachment>`, `<ac:emoticon>` целиком вместе с их содержимым;
- остальная разметка (`<p>`, `<ul>`, `<table>`, и т.п.) остаётся как есть.

При `strip_macros=false` — HTML возвращается дословно.

## Известное ограничение

Раздел собирается обходом sibling'ов от заголовка. Если у документа разметка
ставит заголовки на разной глубине дерева (редко в Confluence-export'ах),
section может остановиться раньше границы. Для обычных постранично-плоских
Confluence-страниц это не проявляется.
