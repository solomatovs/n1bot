# boba-cli-vector-index

Operator CLI: индексация документов в векторную базу (ChromaDB), которую
читает Boba-агент через `boba-ext-chromadb`.

Полностью независим от агентского runtime: нужны только `chromadb` и
доступ к персистент-директории. Backend в v0.1 захардкожен под
ChromaDB; имя пакета нейтральное на случай добавления других векторных
бэкендов в будущем.

## Установка

Минимум (md/txt):
```bash
pip install -e ./cli/vector-index
```

С дополнительными reader'ами:
```bash
pip install -e './cli/vector-index[html]'        # HTML-страницы
pip install -e './cli/vector-index[confluence]'  # Confluence (планируется)
```

После установки доступна команда `boba-cli-vector-index` (плюс
эквивалент `python -m boba_cli_vector_index`).

## Конфиг

| Источник | Назначение |
|---|---|
| `--persist-path` (CLI-флаг) | Путь к ChromaDB persistent-директории |
| `BOBA_VECTOR_INDEX_PERSIST_PATH` (env) | Тот же, fallback если флаг не указан |

CLI намеренно использует **собственный** namespace env-переменных, не
переиспользует агентский `BOBA_EXT_CHROMADB__*` — оператору нужно
прописать оба указывающими на тот же путь, чтобы агент видел то, что
проиндексировал CLI.

## Команды

### `index`

```bash
boba-cli-vector-index index <path>... \
  --collection <name> \
  [--description "..."] \
  [--chunk-size 1000] \
  [--chunk-overlap 200]
```

`<path>` — файл или директория (рекурсивный обход, скрытые `.git`/
`.venv` пропускаются). Расширения, для которых нет reader'а — skip с
warning.

**Idempotent reindex:** перед upsert чанков файла CLI удаляет старые
чанки с `metadata.source_path == <abs path>` — повторный
`index <тот же файл>` = «обновить», а не «добавить ещё одну копию».

`--description` применяется **только при создании** коллекции; для
существующей описание не перезаписывается, чтобы операторские правки
не терялись.

### `list`

```bash
boba-cli-vector-index list
```

Имена коллекций + кол-во чанков + description (то, что видит агент
через `kb_list_collections`).

### `delete`

```bash
boba-cli-vector-index delete --collection <name> [--yes]
```

Без `--yes` спрашивает подтверждение в stdin.

## Поддерживаемые форматы

| Reader | Расширения | Установка |
|---|---|---|
| `MarkdownReader` | `.md`, `.markdown` | базовый |
| `TextReader` | `.txt` | базовый |
| `HtmlReader` | `.html`, `.htm` | `[html]` extra (planned) |

## Метаданные чанков

Каждый чанк получает в `metadata`:

- `source_path` — абсолютный путь файла-источника (используется для dedupe)
- `chunk_index` — порядковый номер чанка в файле
- `file_mtime` — Unix-timestamp mtime файла (для будущей `reindex`-команды)
- `format` — `markdown` / `text` (от reader'а)

## Запуск из VSCode

См. `boba/.vscode/launch.json`, конфигурации `Vector Index: …`.
