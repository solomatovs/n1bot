# Шаг image: образ любой программы boba из её каталога (контекст сборки —
# build/out/<программа>). Основа BASE — glibc с CA (стадия os base.Dockerfile).
# Каталог принадлежит root и только читается; программа пишет только в data/
# экземпляра. PROGRAM — путь бинарника от каталога,
# CHECK — самопроверка, которая роняет сборку образа, если внутри чего-то нет.
# В exec-форме ENTRYPOINT docker не подставляет переменные, поэтому вход идёт через
# оболочку с exec: процессом остаётся сама программа
ARG BASE
FROM ${BASE}

ARG PROGRAM
ARG PORT
ARG CHECK=self-check
ARG BOBA_UID=1000
ARG BOBA_GID=1000

ENV PROGRAM=/app/boba/${PROGRAM}
ENV BOBA_RUNTIME=/app/boba

COPY . /app/boba/

RUN chmod -R a+rX,go-w /app/boba

WORKDIR /app
EXPOSE ${PORT}
USER ${BOBA_UID}:${BOBA_GID}
RUN "$PROGRAM" ${CHECK} > /dev/null
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD "$PROGRAM" health --config /app/boba/conf/config.toml
ENTRYPOINT ["/bin/sh", "-c", "exec \"$PROGRAM\" \"$@\"", "program"]
CMD ["serve", "--config", "/app/boba/conf/config.toml"]
