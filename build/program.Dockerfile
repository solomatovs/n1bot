# слой программы образа приложения на Nuitka: каталог <программа>.dist, который make build
# компилирует в runtime/bin, поверх основы с песочницей. Контекст — runtime/bin, из которого
# .dockerignore (его пишет make build) пускает только эту программу: образ основы с моделями
# и плагинами не пересобирается, когда меняется код. Самопроверка бинарника — уже в образе,
# на его glibc. Собирает make build APP=mcp, результат — SANDBOX_IMAGE стадии runtime-mcp
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG BOBA_DIR=/app/boba
ARG PROGRAM

COPY --chown=root:root ${PROGRAM}.dist/  ${BOBA_DIR}/bin/${PROGRAM}.dist/

RUN chmod -R go-w ${BOBA_DIR}/bin && ${BOBA_DIR}/bin/${PROGRAM}.dist/${PROGRAM}.bin self-check
