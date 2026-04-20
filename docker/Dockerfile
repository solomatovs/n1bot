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

# Копируем установленные пакеты из deps stage
COPY --from=deps /opt/python3.11/lib/python3.11/site-packages /opt/python3.11/lib/python3.11/site-packages
COPY --from=deps /opt/python3.11/bin /opt/python3.11/bin

# Копируем пакеты проекта (монорепо)
COPY src/boba-domain/src/boba_domain/     ./src/boba-domain/src/boba_domain/
COPY src/boba-app/src/boba_app/           ./src/boba-app/src/boba_app/
COPY src/boba-adapters/src/boba_adapters/ ./src/boba-adapters/src/boba_adapters/
COPY src/boba-infra/src/boba_infra/       ./src/boba-infra/src/boba_infra/

ENV PYTHONPATH="/app/src/boba-domain/src:/app/src/boba-app/src:/app/src/boba-adapters/src:/app/src/boba-infra/src"
