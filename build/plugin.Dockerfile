# Шаг image: образ группы плагинов — корень песочницы (образ rootfs.Dockerfile,
# ROOTFS) и программа группы в /plugin (контекст — build/out/boba-mcp/plugins/<группа>).
# Те же части, что у образа mcp, без второй компиляции; корень плагина — сам образ
# контейнера. Вход — программа: `run <id> <подкоманда>`
ARG ROOTFS
FROM ${ROOTFS}

COPY . /plugin/

RUN chmod -R go-w /plugin

ENTRYPOINT ["/plugin/run"]
