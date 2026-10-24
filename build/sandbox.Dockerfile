# основа образа boba-mcp: стадия runtime-sandbox build/Dockerfile и поверх неё модели
# приложения, образы корней плагинов и шаблон workspace. Контекст — каталог runtime/, из
# которого .dockerignore (его пишет make build) пускает только их: гигабайты уходят в демон
# только этой сборкой, а не контекстом каждой сборки build/Dockerfile. Модели меняются
# реже плагинов и идут слоем первыми.
# Собирает make build APP=mcp, результат — SANDBOX_IMAGE стадии runtime-mcp
ARG RUNTIME_SANDBOX
FROM ${RUNTIME_SANDBOX}

ARG BOBA_DIR=/app/boba

COPY --chown=root:root models/                 ${BOBA_DIR}/models/
COPY --chown=root:root sandbox/workspace.ext4  ${BOBA_DIR}/sandbox/workspace.ext4
COPY --chown=root:root sandbox/plugins/        ${BOBA_DIR}/sandbox/plugins/
