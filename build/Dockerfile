# boba
ARG PYTHON_VERSION=3.11
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist

FROM ${PYTHON_IMAGE} AS python-dist


# gcc (glibc+libstdc++) + python + uv, без пакетов: и boba-base, и основа runtime
FROM ${GCC_IMAGE} AS base

# apt-sources.list replace (закрытый контур; по умолчанию .example пуст)
COPY build/artifacts/apt-sources.list* /tmp/aptsrc/
RUN if [ -f /tmp/aptsrc/apt-sources.list ] && grep -qvE '^[[:space:]]*(#|$)' /tmp/aptsrc/apt-sources.list; then \
        cp /tmp/aptsrc/apt-sources.list /etc/apt/sources.list;                                                  \
    fi                                                                                                          \
    && rm -rf /tmp/aptsrc

# ca-chain.crt replace (закрытый контур; по умолчанию .example — плейсхолдер)
COPY build/artifacts/ca-chain.crt* /tmp/cacrt/
RUN if [ -f /tmp/cacrt/ca-chain.crt ] && grep -qE '^-----BEGIN CERTIFICATE-----' /tmp/cacrt/ca-chain.crt; then \
        cp /tmp/cacrt/ca-chain.crt /usr/local/share/ca-certificates/ca-chain.crt                               \
        && update-ca-certificates;                                                                             \
    fi                                                                                                         \
    && rm -rf /tmp/cacrt

# рантайм-зависимости: python C-расширения, kerberos, обработка документов (OCR/office/pdf)
RUN apt-get update                                              \
    && apt-get install -y --no-install-recommends               \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0               \
        liblzma5 libreadline7 libncurses5 zlib1g curl           \
        libkrb5-dev krb5-user                                   \
        imagemagick                                             \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        ghostscript                                             \
        fonts-dejavu-core                                       \
        bubblewrap                                              \
        git                                                     \
        dnsutils                                                \
        vim                                                     \
        telnet                                                  \
    && rm -rf /var/lib/apt/lists/*

# python из dmp/python -dist (дерево /opt/python сливается с /usr/local тулчейна gcc)
COPY --from=python-dist /opt/python /
RUN ldconfig                    \
    && gcc --version | head -n1 \
    && python3 --version        \
    && pip3 --version

# ImageMagick: разрешить чтение/запись PDF (по умолчанию политика запрещает)
RUN for p in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml; do                \
        if [ -f "$p" ]; then                                                                \
            sed -i 's#rights="none" pattern="PDF"#rights="read|write" pattern="PDF"#' "$p"; \
        fi;                                                                                 \
    done

# liteparse ждёт унифицированный CLI magick
RUN printf '%s\n'                                                                                                     \
        '#!/bin/sh'                                                                                                   \
        'case "$1" in'                                                                                                \
        '  convert|identify|mogrify|composite|montage|compare|stream|conjure|import) c="$1"; shift; exec "$c" "$@";;' \
        '  *) exec convert "$@";;'                                                                                    \
        'esac'                                                                                                        \
        > /usr/local/bin/magick                                                                                       \
    && chmod +x /usr/local/bin/magick

# у каждой программы свой способ узнать CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt      \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt           \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt     \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# pip.conf (закрытый контур; по умолчанию .example пуст)
COPY build/artifacts/pip.conf* /tmp/pipconf/
RUN if [ -f /tmp/pipconf/pip.conf ]; then cp /tmp/pipconf/pip.conf /etc/pip.conf; fi && rm -rf /tmp/pipconf

# uv.toml (закрытый контур; по умолчанию не нужен — настройки берутся из pyproject.toml)
COPY build/artifacts/uv.toml* /tmp/uvconf/
RUN if [ -f /tmp/uvconf/uv.toml ]; then mkdir -p /etc/uv && cp /tmp/uvconf/uv.toml /etc/uv/uv.toml; fi && rm -rf /tmp/uvconf

# статический бинарь, ставится offline из wheelhouse
COPY build/artifacts/bin/uv /usr/local/bin/uv


# site-packages с одними внешними зависимостями: ставим пакеты boba ради их
# зависимостей, потом сносим сами пакеты
FROM base AS deps

COPY build/artifacts/wheels/ /tmp/wheels/
COPY packages/ /tmp/boba/packages/

# пакеты ищутся по pyproject.toml; -mindepth 1 отсекает сам packages/
RUN set -eu;                                                                                \
    pkgs=$(find /tmp/boba/packages -mindepth 1 -name pyproject.toml -printf '%h\n' | sort); \
    names=$(for p in $pkgs; do basename "$p"; done);                                        \
    uv pip install --system --no-index --find-links=/tmp/wheels $pkgs &&                    \
    uv pip uninstall --system $names


# runtime: base + зависимости из стадии deps + пакеты boba из исходников
FROM base
ARG PYTHON_VERSION=3.11

# только для метки образа
ARG BOBA_VERSION=0.0.0
LABEL org.opencontainers.image.version=${BOBA_VERSION}

COPY --from=deps /usr/local/lib/python${PYTHON_VERSION}/site-packages /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# --no-deps: зависимости уже пришли из стадии deps.
# --no-build-isolation: setuptools/wheel берём из образа — иначе пришлось бы
# копировать сюда весь wheelhouse и он остался бы в слоях (+171 МБ)
COPY packages/ /app/packages/
RUN set -eu;                                                                                  \
    for d in $(find /app/packages -mindepth 1 -name pyproject.toml -printf '%h\n' | sort); do \
      uv pip install --system --no-deps --no-build-isolation "$d";                             \
    done

COPY build/artifacts/tessdata/ /opt/tessdata/
ENV TESSDATA_PREFIX=/opt/tessdata \
    TESSDATA_PATH=/opt/tessdata

# liteparse просит модель по имени rus+eng, tesseract такой не знает
RUN ln -sf eng.traineddata /opt/tessdata/rus+eng.traineddata && \
    ln -sf rus.traineddata /opt/tessdata/eng+rus.traineddata

# веса fastembed (скачивает make fastembed)
COPY build/artifacts/fastembed/ /opt/fastembed/

WORKDIR /app
EXPOSE 8501

CMD ["boba-chainlit"]
