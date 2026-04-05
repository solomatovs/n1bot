# N1Bot Docker

Multi-stage сборка на базе Astra Linux CE 2.12 с glibc 2.28, GCC 8, Python 3.11.

## Стейджи Dockerfile

| Stage | Target | Описание |
|---|---|---|
| **builder** | `builder` | Компиляция glibc 2.28 + GCC 8 + Python 3.11 из исходников (~30 мин) |
| **base** | `base` | Чистый runtime-образ с новым стеком (без pip-пакетов) |
| **deps** | `deps` | Установка Python-зависимостей из `pyproject.toml` |
| **wheels-export** | `wheels-export` | Экспорт wheels на хост для офлайн-сборки |
| **runtime** | (default) | Финальный образ с приложением |

## Команды сборки

Все команды выполняются из директории `docker/`.

### Полная сборка приложения

```bash
cd docker && docker compose build
```

### Собрать базовый образ отдельно

```bash
docker build --target=base -t n1bot-base -f docker/Dockerfile .
```

Полезно для кеширования: после первой сборки glibc/gcc/python этот образ
можно тегнуть и больше не пересобирать.

### Экспорт wheels на хост

Собирает wheels и копирует их в `docker/wheels/` для офлайн-установки:

```bash
docker build \
    --target=wheels-export \
    --output=type=local,dest=./docker/wheels \
    -f docker/Dockerfile .
```

## Зависимости

Python-зависимости описаны в `pyproject.toml` в корне проекта.
Dockerfile автоматически генерирует `requirements.txt` из `pyproject.toml`
и устанавливает пакеты через `pip install --only-binary=:all:`.

## Исходники для builder

Stage `builder` требует тарболлы в следующих директориях:

```
docker/
  glibc-src/glibc-2.28.tar.xz
  gcc-src/gcc-8.5.0.tar.xz
  gcc-src/gmp-6.1.2.tar.xz
  gcc-src/mpfr-4.0.2.tar.xz
  gcc-src/mpc-1.1.0.tar.gz
  python-src/Python-3.11.12.tar.xz
```

Эти файлы нужны только для первой сборки. После `docker build --target=base`
результат кешируется в Docker layer cache.

### Скачивание исходников

```bash
cd docker && \
curl -L -o glibc-src/glibc-2.28.tar.xz     https://ftp.wayne.edu/gnu/glibc/glibc-2.28.tar.xz && \
curl -L -o gcc-src/gcc-8.5.0.tar.xz         https://ftp.wayne.edu/gnu/gcc/gcc-8.5.0/gcc-8.5.0.tar.xz && \
curl -L -o gcc-src/gmp-6.1.2.tar.xz         https://ftp.wayne.edu/gnu/gmp/gmp-6.1.2.tar.xz && \
curl -L -o gcc-src/mpfr-4.0.2.tar.xz        https://ftp.wayne.edu/gnu/mpfr/mpfr-4.0.2.tar.xz && \
curl -L -o gcc-src/mpc-1.1.0.tar.gz         https://ftp.wayne.edu/gnu/mpc/mpc-1.1.0.tar.gz && \
curl -L -o python-src/Python-3.11.12.tar.xz https://www.python.org/ftp/python/3.11.12/Python-3.11.12.tar.xz
```

Суммарный размер ~130 МБ. После первой успешной сборки base-образа
файлы можно удалить — Docker layer cache сохранит результат.

## Структура приложения

```
pyproject.toml       - Python-зависимости (Poetry)
src/
  app.py             - Streamlit UI (точка входа)
  config.py          - SSL, secrets, tiktoken
  embeddings.py      - LiteLLMEmbeddings, E5OllamaEmbeddings
  chunking.py        - AdvancedChunker, split_into_chunks_semantic
  vectorstore.py     - ChromaDB: get/store/remove
  retrieval.py       - retrieve_docs, rrf_merge, reranking
  rag.py             - get_openai, prepare_rag_context, generate_answer
  confluence.py      - Confluence ingest
```
