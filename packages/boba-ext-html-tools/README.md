# boba-ext-html

HTML-навигация по файлам workspace. Два tool'а:

- `html_outline` — оглавление документа: иерархия `<h1>..<h6>` с anchor'ами.
- `html_section` — фрагмент раздела по anchor.

Источник — только локальные файлы workspace (тот же `ToolContext.project_workspace`,
что у `boba-ext-files`); сетевого доступа нет.

## Установка

```bash
pip install -e ./packages/boba-ext-html
```

Tool'ы регистрируются через entry-point `boba.tools` под id `builtin.html`.

## Доступ

Расширение подключается явно — секцией `[ext.html]`:

```toml
[ext.html]
enable = true
# tools_allow = ["html_outline"]   # пусто = все tools пакета
```

Без `enable = true` (или без секции) — tools не регистрируются.

## html_outline

| поле | обяз. | тип | описание |
|---|---|---|---|
| `path` | ✓ | str | Путь к HTML-файлу в workspace |
| `max_depth` |  | int (1..6) | До какого уровня заголовков; пусто — все 6 |
| `limit` |  | int | Лимит на количество заголовков (по умолчанию 200) |

Ответ — текст-таблица:
```
Документ: docs/api.html  title='API Reference'  charset=utf-8
Заголовков: 42

  1. h1 API Reference  #api-reference
  2.   h2 Authentication  #auth
  3.     h3 Bearer tokens  #idx:3
  ...
```

Anchor'ы: `#<id>` если у `<hN>` есть атрибут `id`, иначе fallback `#idx:N`
(порядковый номер). Этот anchor подаётся в `html_section`.

## html_section

| поле | обяз. | тип | описание |
|---|---|---|---|
| `path` | ✓ | str | Путь к HTML-файлу |
| `anchor` | ✓ | str | Anchor из `html_outline` (`idx:N` или html-id; ведущий `#` опционален) |
| `include_subsections` |  | bool | true (default) — включать вложенные подзаголовки; стоп на следующем заголовке ≤ уровня. false — стоп на любом |
| `max_chars` |  | int | Лимит длины ответа (default 8000) |

Возвращает HTML-фрагмент раздела как есть (без markdown-конвертации).

### Известное ограничение

Раздел собирается через обход sibling'ов от заголовка. Если в документе
заголовки лежат на разной глубине дерева (например, `<h2>` снаружи, `<h3>`
внутри `<article>`), section может остановиться раньше, не пересекая
границу контейнера. На типичных blog/docs/wiki это не проявляется.
