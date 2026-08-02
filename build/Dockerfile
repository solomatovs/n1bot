ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist
ARG SANDBOX_IMAGE=dmp/python:${PYTHON_VERSION}

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

# закрытый контур: индексы пакетов (nexus) и CA его внутреннего TLS.
# Без CA в системном bundle apt/pip/uv/curl не установят соединение
COPY build/conf/ /tmp/conf/
RUN set -eu \
    && if [ -f /tmp/conf/pip.conf ]; then mkdir -p /etc && cp /tmp/conf/pip.conf /etc/pip.conf; fi \
    && if [ -f /tmp/conf/uv.toml ]; then mkdir -p /etc/uv && cp /tmp/conf/uv.toml /etc/uv/uv.toml; fi \
    && if [ -f /tmp/conf/ca-chain.crt ]; then                                \
           mkdir -p /usr/local/share/ca-certificates                         \
           && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/    \
           && update-ca-certificates;                                        \
       fi \
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

COPY build/artifacts/src/bubblewrap-${BWRAP_VERSION}.tar.xz /tmp/bwrap.tar.xz

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

# fuse2fs: монтирование ext4-образов workspace без root (fuse в user
# namespace). libfuse влинковывается статически, рантайму нужен только
# /dev/fuse; glibc остаётся динамической. Здесь же готовится шаблонный
# workspace-образ: sparse-файл SANDBOX_WS_SIZE с чистой ext4
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.2
ARG SANDBOX_WS_SIZE=1G

RUN apt-get update                                  \
    && apt-get install -y --no-install-recommends   \
        meson ninja-build pkg-config xz-utils       \
    && rm -rf /var/lib/apt/lists/*

COPY build/artifacts/src/fuse-${LIBFUSE_VERSION}.tar.xz /tmp/fuse.tar.xz
COPY build/artifacts/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz /tmp/e2fsprogs.tar.xz

RUN set -eu                                                                   \
    && mkdir -p /tmp/fuse-src && cd /tmp/fuse-src                             \
    && tar -xJf /tmp/fuse.tar.xz --strip-components=1                         \
    && meson setup _build                                                     \
        --buildtype=release --prefix=/usr/local                               \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false \
    && ninja -C _build install

RUN set -eu                                                                   \
    && mkdir -p /tmp/e2fs-src && cd /tmp/e2fs-src                             \
    && tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1                    \
    && export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig \
    && LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls       \
    && make -j"$(nproc)"                                                      \
    && strip misc/fuse2fs                                                     \
    && install -D -m 0755 misc/fuse2fs /out/fuse2fs                           \
    && ldd /out/fuse2fs | grep -q fuse && { echo "libfuse не статическая" >&2; exit 1; } || true \
    && /out/fuse2fs -V                                                        \
    && truncate -s ${SANDBOX_WS_SIZE} /out/sandbox-workspace.ext4             \
    && MKE2FS_CONFIG=/tmp/e2fs-src/misc/mke2fs.conf                           \
       ./misc/mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L workspace          \
       /out/sandbox-workspace.ext4

# rootfs песочницы: база + утилиты, которых ждёт LLM (head уже в coreutils).
# На хост его достаёт make sandbox-rootfs; в релизный образ он не кладётся
FROM ${SANDBOX_IMAGE} AS sandbox-rootfs

COPY build/conf/ /tmp/conf/
RUN set -eu                                                       \
    && if [ -f /tmp/conf/ca-chain.crt ]; then                     \
           mkdir -p /usr/local/share/ca-certificates              \
           && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ \
           && update-ca-certificates;                             \
       fi                                                         \
    && apt-get update                                             \
    && apt-get install -y --no-install-recommends                 \
        unzip p7zip-full file curl jq                             \
    && rm -rf /var/lib/apt/lists/* /tmp/conf                      \
    && mkdir -p /workspace                                        \
    && for tool in unzip 7z file head curl jq; do command -v "$tool" >/dev/null; done


# колёса пакетов boba и их зависимостей: сторонние тянутся из индекса
# (conf/pip.conf, conf/uv.toml), а sdist'ы вроде gssapi собираются здесь —
# gcc и заголовки есть только на этой стадии
FROM base AS deps

COPY pyproject.toml /tmp/boba/pyproject.toml
COPY packages/ /tmp/boba/packages/

RUN set -eu \
    && cd /tmp/boba \
    && uv build --all-packages --wheel --no-sources -o /tmp/wheels \
    && uv workspace list > /tmp/names.txt \
    && pip3 wheel --no-cache-dir -w /tmp/wheels --find-links /tmp/wheels \
        $(cat /tmp/names.txt) pip setuptools wheel

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
COPY --from=fuse2fs-build /out/fuse2fs ${BOBA_DIR}/third/bin/fuse2fs
COPY --from=fuse2fs-build /out/sandbox-workspace.ext4 ${BOBA_DIR}/third/sandbox-workspace.ext4
COPY build/conf/                    /tmp/conf/

RUN set -eu \
    # closed-loop: тем же CA приложение ходит в nexus и к LLM по внутреннему TLS
    && if [ -f /tmp/conf/ca-chain.crt ]; then                                                   \
           mkdir -p /usr/local/share/ca-certificates                                            \
           && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/                       \
           && update-ca-certificates;                                                           \
       fi \
    && cp /tmp/conf/boba.env /tmp/boba.env.tmpl \
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
    && mkdir -p ${BOBA_DIR}/third/sandbox-rootfs ${BOBA_DIR}/data/workspace                     \
    && ${BOBA_DIR}/third/bin/bwrap --version                                                    \
    && ${BOBA_DIR}/third/bin/fuse2fs -V                                                         \
    && BOBA_DIR="${BOBA_DIR}" PYTHON_VERSION="${PYTHON_VERSION}"                                \
       envsubst '${BOBA_DIR} ${PYTHON_VERSION}'                                                 \
       < /tmp/boba.env.tmpl > ${BOBA_DIR}/conf/boba.env                                         \
    && rm -rf /tmp/boba.env.tmpl /tmp/conf

WORKDIR /app
EXPOSE 8501

ENV BOBA_DIR=${BOBA_DIR}
ENTRYPOINT ["/bin/sh", "-c", "set -o allexport && . \"$BOBA_DIR/conf/boba.env\" && set +o allexport && exec boba-chainlit \"$@\"", "--"]
CMD []
