# основа образа boba-mcp: стадия runtime-sandbox build/Dockerfile и поверх неё образы корней
# плагинов и шаблон workspace. Контекст — каталог runtime/sandbox: гигабайты образов уходят
# в демон только этой сборкой, а не контекстом каждой сборки build/Dockerfile.
# Собирает make build APP=mcp, результат — SANDBOX_IMAGE стадии runtime-mcp
ARG RUNTIME_SANDBOX
FROM ${RUNTIME_SANDBOX}

ARG BOBA_DIR=/app/boba

COPY --chown=root:root workspace.ext4  ${BOBA_DIR}/sandbox/workspace.ext4
COPY --chown=root:root plugins/        ${BOBA_DIR}/sandbox/plugins/
