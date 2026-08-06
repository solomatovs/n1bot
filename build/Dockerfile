ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist

FROM ${PYTHON_IMAGE} AS python-dist
FROM ${GCC_IMAGE} AS base


RUN apt-get update                                              \
    && apt-get install -y --no-install-recommends               \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0               \
        liblzma5 libreadline7 libncurses5 zlib1g                \
        libkrb5-dev                                             \
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

# proxy
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


# bubblewrap building
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


# fuse2fs and workspace.ext4 building
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.2
ARG SANDBOX_WS_SIZE=1G

RUN apt-get update                                  \
    && apt-get install -y --no-install-recommends   \
        meson ninja-build pkg-config xz-utils       \
    && rm -rf /var/lib/apt/lists/*

COPY build/artifacts/src/fuse-${LIBFUSE_VERSION}.tar.xz         /tmp/fuse.tar.xz
COPY build/artifacts/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz  /tmp/e2fsprogs.tar.xz

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


# sandbox-rootfs prepare
FROM ${GLIBC_IMAGE} AS sandbox-rootfs

ARG LITEPARSE_PACKAGES="imagemagick libreoffice-writer libreoffice-calc libreoffice-impress ghostscript fonts-dejavu-core"

COPY build/conf/ /tmp/conf/
COPY --from=base /usr/local/lib64/libstdc++.so.6      \
                 /usr/local/lib64/libstdc++.so.6.0.25 \
                 /usr/local/lib64/

RUN set -eu                                                                     \
    && if [ -f /tmp/conf/ca-chain.crt ]; then                                   \
           mkdir -p /usr/local/share/ca-certificates                            \
           && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/       \
           && update-ca-certificates;                                           \
       fi                                                                       \
    && apt-get update                                                           \
    && apt-get install -y --no-install-recommends                               \
        unzip p7zip-full file curl jq                                           \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7         \
        libncurses5 zlib1g                                                      \
        libkrb5-3 libgssapi-krb5-2 krb5-user                                    \
        ${LITEPARSE_PACKAGES}                                                   \
    && rm -rf /var/lib/apt/lists/* /tmp/conf                                    \
    && echo /usr/local/lib64 > /etc/ld.so.conf.d/sandbox.conf                   \
    && ldconfig                                                                 \
    # liteparse ждёт унифицированный CLI magick, apt даёт только convert
    && printf '%s\n'                                                            \
        '#!/bin/sh'                                                             \
        'case "$1" in'                                                          \
        '  convert|identify|mogrify|composite|montage|compare|stream|conjure|import) c="$1"; shift; exec "$c" "$@";;' \
        '  *) exec convert "$@";;'                                              \
        'esac'                                                                  \
        > /usr/local/bin/magick                                                 \
    && chmod +x /usr/local/bin/magick                                           \
    # ImageMagick по умолчанию запрещает читать PDF
    && if ! grep -q 'pattern="PDF"' /etc/ImageMagick-6/policy.xml; then         \
           sed -i 's#</policymap>#  <policy domain="coder" rights="read|write" pattern="PDF"/>\n</policymap>#' \
                   /etc/ImageMagick-6/policy.xml;                               \
       fi                                                                       \
    # точки монтирования: bwrap не создаёт их в read-only корне
    && mkdir -p /opt/python /opt/site /opt/src /opt/fastembed                   \
                /usr/share/tessdata /workspace /etc/boba /mnt/pgsock            \
    && touch /etc/boba/krb5.keytab /etc/krb5.conf                               \
    && for tool in unzip 7z file head curl jq convert magick soffice gs; do     \
           command -v "$tool" >/dev/null;                                       \
       done


# boba wheels
FROM base AS boba-wheels

COPY pyproject.toml /tmp/boba/pyproject.toml
COPY packages/ /tmp/boba/packages/

RUN set -eu \
    && cd /tmp/boba \
    && uv build --all-packages --wheel --no-sources -o /tmp/wheels \
    && uv workspace list > /tmp/names.txt


# boba site-packages
FROM base AS app-site

COPY --from=boba-wheels /tmp/wheels/    /tmp/wheels/
COPY --from=boba-wheels /tmp/names.txt  /tmp/names.txt

# приложение ставится по своему графу зависимостей: парсеры и эмбеддер
# сюда не попадают — они объявлены как extra payload у tool-пакетов
RUN set -eu                                                                     \
    && uv pip install --python "$(command -v python3)" --target /opt/app-site   \
           --find-links=/tmp/wheels boba-chainlit                               \
    && rm -rf /tmp/wheels                                                       \
    # фронт chainlit тянет шрифт и стили формул с CDN: в закрытом контуре их
    # отдаёт сам сервер из public/, куда make webassets кладёт katex и Inter
    && sed -i                                                                   \
        -e '/rel="preconnect"/d'                                                \
        -e 's#https://fonts.googleapis.com/css2?family=Inter[^"]*#/public/vendor/inter/inter.css#' \
        -e 's#https://cdn.jsdelivr.net/npm/katex@[0-9.]*/dist/katex.min.css#/public/vendor/katex/katex.min.css#' \
        /opt/app-site/chainlit/frontend/dist/index.html                         \
    && ! grep -qE 'fonts\.(googleapis|gstatic)\.com|cdn\.jsdelivr\.net'         \
        /opt/app-site/chainlit/frontend/dist/index.html                         \
    && PYTHONPATH=/opt/app-site python3 -c "import boba.chainlit; print('app-site ok')" \
    && PYTHONPATH=/opt/app-site python3 -c "import importlib.metadata as md; \
print('\\n'.join(sorted(d.metadata['Name'] for d in md.distributions() \
if d.metadata['Name'].startswith('boba-'))))" > /opt/app-packages.txt


# site-packages песочницы
FROM base AS sandbox-site

COPY --from=boba-wheels /tmp/wheels/    /tmp/wheels/
COPY --from=boba-wheels /tmp/names.txt  /tmp/names.txt

# в песочнице живут только инструменты: приложение сюда не ставится,
# а tool-пакеты берутся с extra payload (парсеры, эмбеддер, plotly)
RUN set -eu                                                                     \
    && sed -n 's/^\(boba-tool-[a-z]*\|boba-web\)$/\1[payload]/p' /tmp/names.txt \
         > /tmp/names-sandbox.txt                                               \
    && uv pip install --python "$(command -v python3)" --target /opt/site       \
           --find-links=/tmp/wheels $(cat /tmp/names-sandbox.txt)               \
    && rm -rf /tmp/wheels                                                       \
    && PYTHONPATH=/opt/site python3 -c "import liteparse, markdownify, bs4, lxml, plotly, httpx, psycopg, clickhouse_connect, aiohttp, fastembed, gssapi, krb5; print('site ok')"


# runtime image for docker-compose
# and make extract copy to server host
FROM ${GLIBC_IMAGE} AS runtime

ARG PYTHON_VERSION=3.11 \
    BOBA_DIR=/app/boba \
    BOBA_UID=1000 \
    BOBA_GID=1000


RUN apt-get update                                              \
    && apt-get install -y --no-install-recommends               \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0               \
        liblzma5 libreadline7 libncurses5 zlib1g                \
        libkrb5-3 libgssapi-krb5-2 krb5-user                    \
        gettext-base                                            \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dist                         /opt/python/usr/local   ${BOBA_DIR}/third/python
COPY --from=base /usr/local/lib64/libstdc++.so.6      ${BOBA_DIR}/third/lib/
COPY --from=base /usr/local/lib64/libstdc++.so.6.0.25 ${BOBA_DIR}/third/lib/
COPY --from=base /usr/local/bin/uv              ${BOBA_DIR}/third/bin/uv
COPY --from=app-site /opt/app-site              ${BOBA_DIR}/third/python/lib/python${PYTHON_VERSION}/site-packages/
COPY --from=app-site    /opt/app-packages.txt   ${BOBA_DIR}/conf/packages.txt
COPY --from=bwrap-build /out/bwrap              ${BOBA_DIR}/third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs          ${BOBA_DIR}/third/bin/fuse2fs
COPY --from=sandbox-rootfs                      / ${BOBA_DIR}/third/sandbox/rootfs/
COPY --from=sandbox-site                        /opt/site ${BOBA_DIR}/third/sandbox/site/
COPY --from=python-dist                         /opt/python/usr/local ${BOBA_DIR}/third/sandbox/python/
COPY --from=fuse2fs-build                       /out/sandbox-workspace.ext4 ${BOBA_DIR}/third/sandbox/workspace.ext4
COPY build/artifacts/tessdata/                  ${BOBA_DIR}/third/sandbox/data/tessdata/
COPY build/artifacts/fastembed/                 ${BOBA_DIR}/third/sandbox/data/fastembed/
COPY build/conf/                                /tmp/conf/
# UI
COPY packages/agents/boba-chainlit/assets/chainlit/config.toml      ${BOBA_DIR}/data/.chainlit/
COPY packages/agents/boba-chainlit/assets/chainlit/translations/    ${BOBA_DIR}/data/.chainlit/translations/
COPY packages/agents/boba-chainlit/assets/chainlit/chainlit.md      ${BOBA_DIR}/data/
COPY packages/agents/boba-chainlit/assets/public/                   ${BOBA_DIR}/data/public/
COPY build/artifacts/webassets/                                     ${BOBA_DIR}/data/public/vendor/

RUN set -eu \
    && if [ -f /tmp/conf/ca-chain.crt ]; then                                                   \
           mkdir -p /usr/local/share/ca-certificates                                            \
           && cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/                       \
           && update-ca-certificates;                                                           \
       fi \
    && cp /tmp/conf/boba.env /tmp/boba.env.tmpl \
    && export LD_LIBRARY_PATH="${BOBA_DIR}/third/lib:${BOBA_DIR}/third/python/lib"                                    \
    && SITE=${BOBA_DIR}/third/python/lib/python${PYTHON_VERSION}/site-packages                  \
    && mv "$SITE"/bin/* ${BOBA_DIR}/third/python/bin/                                           \
    && rmdir "$SITE"/bin                                                                        \
    && sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/python/bin/python3.11|"                    \
            ${BOBA_DIR}/third/python/bin/boba-*                                                 \
    # conf — boba.env и config.toml; data — app_root chainlit;
    # data/home — HOME процесса; data/workspace — ext4-образы пользователей
    && mkdir -p ${BOBA_DIR}/conf ${BOBA_DIR}/data/.chainlit                                     \
             ${BOBA_DIR}/data/home ${BOBA_DIR}/data/workspace                                   \
    # приложение бежит непривилегированным: под root образы пользователей
    # ложились бы в том с чужим uid, а userns песочницы его не мапит
    && chown -R ${BOBA_UID}:${BOBA_GID} ${BOBA_DIR}/data                                        \
    && ln -sf eng.traineddata ${BOBA_DIR}/third/sandbox/data/tessdata/rus+eng.traineddata       \
    && ln -sf rus.traineddata ${BOBA_DIR}/third/sandbox/data/tessdata/eng+rus.traineddata       \
    && ${BOBA_DIR}/third/bin/bwrap --version                                                    \
    && ${BOBA_DIR}/third/bin/fuse2fs -V                                                         \
    && BOBA_DIR="${BOBA_DIR}" PYTHON_VERSION="${PYTHON_VERSION}"                                \
       envsubst '${BOBA_DIR} ${PYTHON_VERSION}'                                                 \
       < /tmp/boba.env.tmpl > ${BOBA_DIR}/conf/boba.env                                         \
    && rm -rf /tmp/boba.env.tmpl /tmp/conf

WORKDIR /app
EXPOSE 8501

ENV BOBA_DIR=${BOBA_DIR}
USER ${BOBA_UID}:${BOBA_GID}
ENTRYPOINT ["/bin/sh", "-c", "set -o allexport && . \"$BOBA_DIR/conf/boba.env\" && set +o allexport && exec boba-chainlit \"$@\"", "--"]
CMD []
