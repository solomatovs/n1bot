# ============================================================
# Boba — runtime Dockerfile
#
# Требует boba-base:latest (собирается из Dockerfile.base).
# Если base ещё не собран:
#   cd docker && docker build -f Dockerfile.base -t boba-base ..
#
# Сборка runtime (нужен сетевой доступ к PyPI):
#   cd docker && docker compose build
# ============================================================

FROM boba-base:latest AS deps
ENTRYPOINT []

# Poetry — разовая утилита: ставим в изолированный venv, чтобы он НЕ
# попал в /opt/python3.11/site-packages, которое копирует рантайм.
RUN python3 -m venv /tmp/poetry && \
    /tmp/poetry/bin/pip install --no-cache-dir poetry poetry-plugin-export

WORKDIR /tmp/install

# Метаданные монорепо: root pyproject + lock + pyproject'ы под-пакетов
# (poetry читает их при резолве path-deps boba-*). Исходники здесь не
# нужны — path-пакеты отфильтровываются из requirements.txt.
COPY pyproject.toml poetry.lock ./
COPY src/domain/pyproject.toml    src/domain/
COPY src/app/pyproject.toml       src/app/
COPY src/adapters/pyproject.toml  src/adapters/
COPY src/infra/pyproject.toml     src/infra/
COPY src/chainlit/pyproject.toml  src/chainlit/

# poetry export → requirements.txt без dev-групп; path-пакеты boba-*
# (develop=true) вырезаем — они идут через PYTHONPATH, а не через pip.
# Затем pip install в системный python (то, что скопирует runtime).
RUN /tmp/poetry/bin/poetry export --without=dev --without-hashes -f requirements.txt \
      | grep -v "file:///" > req.txt && \
    pip3 install --no-cache-dir -r req.txt

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

# .chainlit/ должен быть писемой директорией: Chainlit на старте создаёт
# внутри .chainlit/translations/. Поэтому монтировать весь каталог нельзя —
# в docker-compose bind'ом прокинут только config.toml (и chainlit.md),
# а сам каталог с исходным config.toml кладём здесь.
COPY src/chainlit/.chainlit/  ./chainlit/.chainlit/

ENV PYTHONPATH="/app/src/domain/src:/app/src/app/src:/app/src/adapters/src:/app/src/infra/src:/app/src/chainlit/src"

EXPOSE 8080
