# boba-ext-chromadb

Read-only ChromaDB knowledge-base tools для агента Boba. Минимальный
набор v0.1:

- `kb_list_collections` — список доступных коллекций с описанием.
- `kb_search` — semantic search по коллекции, возвращает top-k hits
  c id, distance, metadata и snippet (превью документа).

Записывать в БД агент не может — индексирование документов выполняет
оператор отдельным процессом, агент только читает.

## Установка

### Локально (dev)

```bash
pip install 'chromadb>=0.5'                      # runtime-зависимость
pip install --no-deps -e ./extensions/chromadb   # сам extension
```

`--no-deps` — потому что `boba` core доставляется через PYTHONPATH, а
не pip-инсталляцией. `chromadb` ставится явно отдельной командой,
чтобы не тянуть его в процессы, которым он не нужен.

После установки `ToolPluginLoader` подхватит entry-point
`boba.tools/chromadb` автоматически.

### Docker (prod)

Базовый образ собирается **без** chromadb — `~200 МБ` transitive deps
(`numpy`, `onnxruntime`, `posthog`, …) добавляются только при явном
включении:

```bash
INSTALL_EXT_CHROMADB=true docker compose build chainlit
# или:
docker build --build-arg INSTALL_EXT_CHROMADB=true ...
```

Без флага исходники extension'а попадают в образ
(`/app/extensions/chromadb`), но `pip install` пропускается —
entry-point не зарегистрирован, агент tools не видит. Это позволяет
включить extension позже без пересборки base layer'ов.

## Конфиг

Через namespaced extension bag (см. `AppConfig.extensions`):

| Поле | Источник | По умолчанию | Назначение |
|---|---|---|---|
| `persist_path` | `BOBA_EXT_CHROMADB__PERSIST_PATH` или `[extensions.chromadb] persist_path` | — (обязательно) | Путь к persistent-БД ChromaDB |
| `embedding_model` | `BOBA_EXT_CHROMADB__EMBEDDING_MODEL` | `default` | Имя модели embeddings. В v0.1 поддерживается только `default` (bundled ONNX). |
| `max_top_k` | `BOBA_EXT_CHROMADB__MAX_TOP_K` | `20` | Потолок параметра `top_k` для `kb_search` |
| `snippet_chars` | `BOBA_EXT_CHROMADB__SNIPPET_CHARS` | `300` | Длина превью документа в результатах `kb_search` |

## Подготовка коллекций (оператор)

Все коллекции, которые увидит агент, должен создать оператор заранее
обычным ChromaDB-API. Описание для агента кладётся в `metadata`
коллекции в поле `description`:

```python
import chromadb
client = chromadb.PersistentClient(path="/var/lib/chroma")
client.create_collection(
    name="docs_2026",
    metadata={"description": "Внутренняя документация за 2026 год"},
)
# ... наполнение через collection.upsert(...) ...
```

`kb_list_collections` покажет агенту имя + описание; коллекции без
`description` всё равно видны (но без описания назначения).
