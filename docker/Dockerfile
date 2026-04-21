# ============================================================
# Boba — runtime Dockerfile
#
# Требует boba-base:latest (собирается из Dockerfile.base).
# Если base ещё не собран:
#   cd docker && docker build -f Dockerfile.base -t boba-base ..
#
# Сборка runtime:
#   cd docker && docker compose build
# ============================================================

FROM boba-base:latest AS deps
ENTRYPOINT []

COPY docker/wheels/ /tmp/wheels/

RUN pip3 install --no-cache-dir --no-deps --no-index --find-links=/tmp/wheels/ /tmp/wheels/*.whl && \
    rm -rf /tmp/wheels/

# ============================================================
# Runtime
# ============================================================
FROM boba-base:latest
ENTRYPOINT []

WORKDIR /app

# Python-зависимости из deps-stage
COPY --from=deps /opt/python3.11/lib/python3.11/site-packages /opt/python3.11/lib/python3.11/site-packages
COPY --from=deps /opt/python3.11/bin /opt/python3.11/bin

# Монорепозиторий: каждый слой — самостоятельный Poetry-пакет.
# В контейнере boba-* не устанавливаются через Poetry (нет локального
# сборщика); используется неявный namespace-пакет boba через PYTHONPATH.
# Файла boba/__init__.py намеренно нет — Python 3.3+ сам склеивает namespace.
COPY src/domain/src/boba/     ./src/domain/src/boba/
COPY src/app/src/boba/        ./src/app/src/boba/
COPY src/adapters/src/boba/   ./src/adapters/src/boba/
COPY src/infra/src/boba/      ./src/infra/src/boba/
COPY src/chainlit/src/boba/   ./src/chainlit/src/boba/

# Chainlit читает .chainlit/ и chainlit.md из working_dir (или из каталога
# с app.py). Кладём их рядом, чтобы сервис запускался с cwd=/app/chainlit.
COPY src/chainlit/.chainlit/  ./chainlit/.chainlit/
COPY src/chainlit/chainlit.md ./chainlit/chainlit.md

ENV PYTHONPATH="/app/src/domain/src:/app/src/app/src:/app/src/adapters/src:/app/src/infra/src:/app/src/chainlit/src"

EXPOSE 8080
