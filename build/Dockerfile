ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist

FROM ${PYTHON_IMAGE} AS python-dist
FROM ${GCC_IMAGE} AS base


RUN apt-get update                                              \
    && apt-get install -y --no-install-recommends               \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0               \
        liblzma5 libreadline7 libncurses5 zlib1g curl           \
        libkrb5-dev krb5-user                                   \
        imagemagick                                             \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        ghostscript                                             \
        tesseract-ocr                                           \
        fonts-dejavu-core                                       \
        bubblewrap                                              \
        git                                                     \
        dnsutils                                                \
        vim                                                     \
        telnet                                                  \
    && rm -rf /var/lib/apt/lists/*

# перекрываем системный python своим
COPY --from=python-dist /opt/python /
RUN ldconfig                    \
    && gcc --version | head -n1 \
    && python3 --version        \
    && pip3 --version

# у каждой программы свой способ узнать CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt      \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt           \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt     \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# копируем конфигурации для pip, uv
COPY build/conf/ /tmp/conf/
RUN set -eu \
    && if [ -f /tmp/conf/pip.conf ]; then mkdir -p /etc && cp /tmp/conf/pip.conf /etc/pip.conf; fi \
    && if [ -f /tmp/conf/uv.toml ]; then mkdir -p /etc/uv && cp /tmp/conf/uv.toml /etc/uv/uv.toml; fi \
    && rm -rf /tmp/conf
COPY build/artifacts/uv/uv /usr/local/bin/uv

# bubblewrap: apt в Astra 2.12 даёт 0.3.3 (нет --clearenv и части unshare-
# флагов), поэтому собираем свежий из исходников. libcap влинковывается
# статически (meson подставляет полный путь к libcap.so — подменяем на .a),
# так что рантайму не нужен apt-пакет libcap2; glibc остаётся динамической —
# она и так база runtime-образа.
FROM base AS bwrap-build

ARG BWRAP_VERSION=0.11.2

RUN apt-get update                                  \
    && apt-get install -y --no-install-recommends   \
        libcap-dev meson ninja-build pkg-config     \
        xz-utils                                    \
    && rm -rf /var/lib/apt/lists/*

COPY build/artifacts/bwrap/bubblewrap-${BWRAP_VERSION}.tar.xz /tmp/bwrap.tar.xz

RUN set -eu                                                                   \
    && mkdir -p /tmp/bwrap-src && cd /tmp/bwrap-src                           \
    && tar -xJf /tmp/bwrap.tar.xz --strip-components=1                        \
    && meson setup _build                                                     \
        --buildtype=release --prefix=/usr                                     \
        -Dselinux=disabled -Dman=disabled                                     \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' \
        _build/build.ninja                                                    \
    && ninja -C _build bwrap                                                  \
    && strip _build/bwrap                                                     \
    && install -D -m 0755 _build/bwrap /out/bwrap                             \
    && ldd /out/bwrap | grep -q libcap && { echo "libcap не статическая" >&2; exit 1; } || true \
    && /out/bwrap --version

FROM base AS deps

COPY build/artifacts/wheels/ /tmp/wheels/
COPY pyproject.toml /tmp/boba/pyproject.toml
COPY packages/ /tmp/boba/packages/

RUN set -eu \
    && cd /tmp/boba \
    && uv build --all-packages --wheel --no-sources -o /tmp/wheels \
    && uv workspace list > /tmp/names.txt

FROM ${GLIBC_IMAGE} AS runtime

ARG PYTHON_VERSION=3.11 \
    BOBA_DIR=/app/boba

RUN apt-get update                                              \
    && apt-get install -y --no-install-recommends               \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0               \
        liblzma5 libreadline7 libncurses5 zlib1g curl           \
        libkrb5-dev krb5-user                                   \
        imagemagick                                             \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        ghostscript                                             \
        tesseract-ocr                                           \
        fonts-dejavu-core                                       \
        git                                                     \
        dnsutils                                                \
        gettext-base                                            \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dist /opt/python/usr/local ${BOBA_DIR}/third/python
COPY --from=base                            \
    /usr/local/lib64/libstdc++.so.6         \
    /usr/local/lib64/libstdc++.so.6.0.25    \
    ${BOBA_DIR}/third/lib/
COPY --from=base /usr/local/bin/uv  /usr/local/bin/uv
COPY --from=deps /tmp/wheels/       /tmp/wheels/
COPY --from=deps /tmp/names.txt     /tmp/names.txt
COPY build/artifacts/tessdata/      ${BOBA_DIR}/third/tessdata/
COPY build/artifacts/fastembed/     ${BOBA_DIR}/third/fastembed/
COPY build/artifacts/uv/uv          ${BOBA_DIR}/third/bin/uv
# песочница для tool `bash`: собранный из исходников bwrap (см. bwrap-build)
COPY --from=bwrap-build /out/bwrap  ${BOBA_DIR}/third/bin/bwrap
COPY build/conf/boba.env            /tmp/boba.env.tmpl

RUN set -eu \
    && PY=${BOBA_DIR}/third/python/bin/python3.11 \
    && export LD_LIBRARY_PATH="${BOBA_DIR}/third/lib:${BOBA_DIR}/third/python/lib"              \
    # ImageMagick конфиг (policy.xml)
    && mkdir -p ${BOBA_DIR}/etc \
    && cp -a /etc/ImageMagick-6 ${BOBA_DIR}/etc/                                                \
    && if ! grep -q 'pattern="PDF"' ${BOBA_DIR}/etc/ImageMagick-6/policy.xml; then              \
           sed -i 's#</policymap>#  <policy domain="coder" rights="read|write" pattern="PDF"/>\n</policymap>#' \
                   ${BOBA_DIR}/etc/ImageMagick-6/policy.xml;                                     \
       fi \
    # liteparse ждёт унифицированный CLI magick — это наш скрипт, он живёт в third/bin
    && mkdir -p ${BOBA_DIR}/third/bin \
    && printf '%s\n'                                                                            \
        '#!/bin/sh'                                                                             \
        'case "$1" in'                                                                          \
        '  convert|identify|mogrify|composite|montage|compare|stream|conjure|import) c="$1"; shift; exec "$c" "$@";;' \
        '  *) exec convert "$@";;'                                                              \
        'esac'                                                                                  \
        > ${BOBA_DIR}/third/bin/magick                                                          \
    && chmod +x ${BOBA_DIR}/third/bin/magick                                                    \
    && uv pip install --python "$PY" --no-index --find-links=/tmp/wheels pip setuptools wheel   \
    && uv pip install --python "$PY" --no-index --find-links=/tmp/wheels $(cat /tmp/names.txt)  \
    && ln -sf eng.traineddata ${BOBA_DIR}/third/tessdata/rus+eng.traineddata                    \
    && ln -sf rus.traineddata ${BOBA_DIR}/third/tessdata/eng+rus.traineddata                    \
    && mkdir -p ${BOBA_DIR}/conf/prompts ${BOBA_DIR}/etc                                        \
             ${BOBA_DIR}/data/workspaces/user ${BOBA_DIR}/data/workspaces/system                \
             ${BOBA_DIR}/data/.chainlit ${BOBA_DIR}/data/home                                   \
    && BOBA_DIR="${BOBA_DIR}" PYTHON_VERSION="${PYTHON_VERSION}"                                \
       envsubst '${BOBA_DIR} ${PYTHON_VERSION}'                                                 \
       < /tmp/boba.env.tmpl > ${BOBA_DIR}/conf/boba.env                                         \
    && rm -f /tmp/boba.env.tmpl

WORKDIR /app
EXPOSE 8501

ENV BOBA_DIR=${BOBA_DIR}
ENTRYPOINT ["/bin/sh", "-c", "set -o allexport && . \"$BOBA_DIR/conf/boba.env\" && set +o allexport && exec boba-chainlit \"$@\"", "--"]
CMD []
