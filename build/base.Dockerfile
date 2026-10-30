# Шаг prepare: среда сборки boba. Классический сборщик (DOCKER_BUILDKIT=0) проходит
# все стадии до --target по порядку файла, поэтому стадии идут от общих к частным, а
# стадия site с аргументами одной детали стоит последней. Сеть — только реестры
# контура: apt из sources.list образов dmp/*, pypi из build/conf. Синтаксис только
# классический: без RUN --mount, COPY --chmod/--link и heredoc
ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist

FROM ${PYTHON_IMAGE} AS python-dist


# CA внутреннего TLS и индексы пакетов контура из build/conf
FROM ${GLIBC_IMAGE} AS conf

COPY build/conf/  /tmp/conf/

RUN mkdir -p /certs/etc/ssl /certs/usr/local/share /index/etc/uv
RUN if [ -f /tmp/conf/ca-chain.crt ]; then \
        cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ && update-ca-certificates; \
    fi
RUN cp -a /etc/ssl/certs /certs/etc/ssl/ \
    && cp -a /usr/local/share/ca-certificates /certs/usr/local/share/
RUN if [ -f /tmp/conf/pip.conf ]; then cp /tmp/conf/pip.conf /index/etc/pip.conf; else : > /index/etc/pip.conf; fi
RUN if [ -f /tmp/conf/uv.toml ]; then cp /tmp/conf/uv.toml /index/etc/uv/uv.toml; fi


# python без сборочного и учебного балласта: дерево программы на python и корней
# песочницы с python
FROM ${GLIBC_IMAGE} AS python-slim

ARG PYTHON_VERSION

COPY --from=python-dist /opt/python/usr/local  /python/

RUN lib="/python/lib/python${PYTHON_VERSION}" \
    && rm -rf /python/include /python/share /python/lib/pkgconfig \
              /python/bin/2to3* /python/bin/idle3* /python/bin/pip* /python/bin/pydoc3* \
              /python/bin/python*-config \
              "$lib/test" "$lib/idlelib" "$lib/tkinter" "$lib/lib2to3" "$lib/ensurepip" \
              "$lib/config-${PYTHON_VERSION}-x86_64-linux-gnu" \
              "$lib/site-packages/pip" "$lib/site-packages/setuptools" \
              "$lib/site-packages/pkg_resources" "$lib/site-packages/"*.dist-info

RUN LD_LIBRARY_PATH=/python/lib PYTHONHOME=/python /python/bin/python3 -c "import ssl, sqlite3, lzma, bz2"


# основа образа программы на python (чат, пока он не на Nuitka): glibc, CA и
# системные библиотеки интерпретатора и kerberos
FROM ${GLIBC_IMAGE} AS pyos

COPY --from=conf /certs/  /

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-3 libgssapi-krb5-2 krb5-user                     \
    && rm -rf /var/lib/apt/lists/*


# основа образа программы на Nuitka: glibc и CA, больше ничего
FROM ${GLIBC_IMAGE} AS os

COPY --from=conf /certs/  /


# среда сборки: gcc, наш python поверх системного, uv, CA и индексы контура
FROM ${GCC_IMAGE} AS base

COPY --from=conf /certs/  /

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-dev patch xz-utils                               \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dist /opt/python   /
COPY --from=python-slim /python/      /opt/python-slim/
COPY --from=conf /index/              /
COPY build/src/uv/uv                  /usr/local/bin/uv

# закрытый контур: pip не спрашивает индекс о своей новой версии, uv не качает python
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 UV_PYTHON_DOWNLOADS=never \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt           \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt      \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt                \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

RUN ldconfig && gcc --version | head -n1 && python3 --version && uv --version


# bubblewrap статически с libcap: у meson bwrap нет такой опции, поэтому в
# сгенерированном build.ninja .so подменяется на .a
FROM base AS bwrap

ARG BWRAP_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcap-dev meson ninja-build pkg-config                  \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/bubblewrap-${BWRAP_VERSION}.tar.xz  /tmp/bwrap.tar.xz

WORKDIR /tmp/bwrap-src
RUN tar -xJf /tmp/bwrap.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr -Dselinux=disabled -Dman=disabled \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' _build/build.ninja \
    && ninja -C _build bwrap
RUN strip _build/bwrap && install -D -m 0755 _build/bwrap /out/bwrap
RUN ! ldd /out/bwrap | grep libcap && /out/bwrap --version


# fuse2fs статически с libfuse и mke2fs той же версии. Патч: монтирование ro
# открывает образ только на чтение — без него fuse2fs пишет суперблок образа,
# смонтированного ro, и не монтирует его под ro-биндом (EROFS)
FROM base AS fuse2fs

ARG LIBFUSE_VERSION
ARG E2FSPROGS_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        meson ninja-build pkg-config                             \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/fuse-${LIBFUSE_VERSION}.tar.xz                  /tmp/fuse.tar.xz
COPY build/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz           /tmp/e2fsprogs.tar.xz
COPY build/patches/e2fsprogs-${E2FSPROGS_VERSION}-ro-mount.patch  /tmp/e2fsprogs.patch

WORKDIR /tmp/fuse-src
RUN tar -xJf /tmp/fuse.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr/local \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false \
    && ninja -C _build install

WORKDIR /tmp/e2fs-src
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig
RUN tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1
RUN patch -p1 < /tmp/e2fsprogs.patch
RUN LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls && make -j"$(nproc)"
RUN strip misc/fuse2fs && install -D -m 0755 misc/fuse2fs /out/fuse2fs
RUN ! ldd /out/fuse2fs | grep fuse && /out/fuse2fs -V
RUN install -D -m 0755 misc/mke2fs /out/mke2fs && cp misc/mke2fs.conf /out/mke2fs.conf


# вспомогательные программы песочницы: make забирает /libexec в каталог программы mcp
FROM scratch AS libexec

COPY --from=bwrap   /out/bwrap        /libexec/bwrap
COPY --from=fuse2fs /out/fuse2fs      /libexec/fuse2fs
COPY --from=fuse2fs /out/mke2fs       /libexec/mke2fs
COPY --from=fuse2fs /out/mke2fs.conf  /libexec/mke2fs.conf


# python-oracledb с патчем: sdist ровно той версии из индекса pypi с проверкой
# sha256, локальный суффикс версии — чтобы установщик не подменил колесо готовым
FROM base AS oracledb

ARG ORACLEDB_VERSION
ARG ORACLEDB_SHA256
ARG ORACLEDB_LOCAL
ARG CYTHON_VERSION
ARG SETUPTOOLS_VERSION=84.0.0
ARG WHEEL_VERSION=0.48.0

COPY build/patches/oracledb-${ORACLEDB_VERSION}-arrow-duplicates.patch  /tmp/oracledb.patch

RUN pip3 install --no-cache-dir "cython==${CYTHON_VERSION}" \
        "setuptools==${SETUPTOOLS_VERSION}" "wheel==${WHEEL_VERSION}"
RUN pip3 download --no-cache-dir --no-deps --no-binary :all: --no-build-isolation \
        "oracledb==${ORACLEDB_VERSION}" -d /tmp/sdist
RUN echo "${ORACLEDB_SHA256}  /tmp/sdist/oracledb-${ORACLEDB_VERSION}.tar.gz" | sha256sum -c -

WORKDIR /tmp/oracledb-src
RUN tar -xzf /tmp/sdist/oracledb-${ORACLEDB_VERSION}.tar.gz --strip-components=1
RUN patch -p1 < /tmp/oracledb.patch
RUN sed -i "s/^__version__ = \"${ORACLEDB_VERSION}\"$/__version__ = \"${ORACLEDB_VERSION}+${ORACLEDB_LOCAL}\"/" \
        src/oracledb/version.py \
    && grep -q "^__version__ = \"${ORACLEDB_VERSION}+${ORACLEDB_LOCAL}\"$" src/oracledb/version.py
# без отладочных символов: колесо с ними в четыре раза тяжелее
RUN CFLAGS="-g0" pip3 wheel . --no-cache-dir --no-deps --no-build-isolation -w /out && ls -l /out
RUN pip3 install --no-cache-dir /out/oracledb-*.whl \
    && python3 -c "import oracledb; print(oracledb.__version__)" | grep -q "+${ORACLEDB_LOCAL}$"


# колёса всех пакетов репозитория, constraints из uv.lock и переопределения
# зависимостей, с которыми собран uv.lock: деревья пакетов ставятся теми же версиями
FROM base AS wheels

COPY pyproject.toml  /tmp/boba/pyproject.toml
COPY uv.lock         /tmp/boba/uv.lock
COPY packages/       /tmp/boba/packages/
COPY --from=oracledb /out/  /tmp/wheels/
# find-links корневого pyproject ведёт сюда: колесо с патчем лежит там же, где у .venv
COPY --from=oracledb /out/  /tmp/boba/build/out/oracledb/

WORKDIR /tmp/boba
RUN uv build --all-packages --wheel --no-sources -o /tmp/wheels
RUN uv export --frozen --all-packages --all-extras --no-emit-workspace --no-hashes \
        -o /tmp/wheels/constraints.txt
RUN python3 -c "import tomllib, pathlib; uv = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['uv']; print('\\n'.join(uv['override-dependencies']))" \
        > /tmp/wheels/overrides.txt


# инструменты компиляции: Nuitka и patchelf из индекса pypi в /opt/nuitka, ccache из
# apt. Компилирует не сборка образа, а make: docker run дерева пакетов (стадия site)
# сценарием build/nuitka.sh без сети, с кэшем build/cache
FROM wheels AS nuitka

ARG NUITKA_VERSION
ARG PATCHELF_VERSION
ARG ORDERED_SET_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends ccache \
    && rm -rf /var/lib/apt/lists/*
RUN uv pip install --python "$(command -v python3)" --target /opt/nuitka \
        "nuitka==${NUITKA_VERSION}" "patchelf==${PATCHELF_VERSION}" \
        "ordered-set==${ORDERED_SET_VERSION}"

COPY build/plugin_rootfs.py  /opt/plugin_rootfs.py


# дерево пакетов программы (PACKAGES) или группы плагинов (PLUGINS — id через
# пробел) в /opt/site. У плагинов в дерево идёт замыкание их payload и поставщиков
# потребляемых групп entry points, а системные библиотеки ключа libs их деклараций
# ставятся в образ: Nuitka импортирует пакеты при сборке и кладёт копии этих
# библиотек в программу. Нативные пакеты из sdist — без отладочной информации,
# метки времени uv — вон: дерево зависит только от содержимого колёс
FROM nuitka AS site

ARG PACKAGES
ARG PLUGINS

RUN python3 /opt/plugin_rootfs.py libs /tmp/boba/packages ${PLUGINS} > /tmp/libs.txt \
    && if [ -s /tmp/libs.txt ]; then \
           apt-get update \
           && apt-get install -y --no-install-recommends $(cat /tmp/libs.txt) \
           && rm -rf /var/lib/apt/lists/*; \
       fi
RUN python3 /opt/plugin_rootfs.py names /tmp/boba/packages ${PLUGINS} > /tmp/names.txt \
    && CFLAGS="-g0" uv pip install --python "$(command -v python3)" --target /opt/site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        --override /tmp/wheels/overrides.txt \
        ${PACKAGES} $(cat /tmp/names.txt) \
    && find /opt/site -name uv_cache.json -delete \
    && sed -i '/uv_cache\.json,/d' /opt/site/*.dist-info/RECORD
