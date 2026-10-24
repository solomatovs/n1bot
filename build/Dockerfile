ARG PYTHON_VERSION=3.11
ARG GLIBC_IMAGE=dmp/glibc:2.28
ARG GCC_IMAGE=dmp/gcc:8.5.0
ARG PYTHON_IMAGE=dmp/python:${PYTHON_VERSION}-dist
ARG NODE_IMAGE=dmp/nodejs:20
# основа образа mcp: runtime-sandbox с образами корней плагинов и workspace, их make build
# кладёт отдельной сборкой build/sandbox.Dockerfile в контексте runtime/sandbox
ARG SANDBOX_IMAGE=runtime-sandbox

# Собирает make классическим сборщиком (DOCKER_BUILDKIT=0): он проходит все стадии до
# --target по порядку файла. Поэтому стадии идут по целям make — base, oracledb-wheel,
# sandbox-out, plugin-site-out и plugin-root, boba-bin-out, runtime-sandbox и runtime-mcp,
# runtime-aid, — а стадия, которой нужны аргументы одной цели, без них пуста
# (plugin-site без PLUGIN, bin-site и boba-bin без NUITKA_VERSION).
# Синтаксис только классический: без RUN --mount, COPY --chmod/--link и heredoc

FROM ${PYTHON_IMAGE} AS python-dist


# python рантайма без сборочного и учебного балласта: одно дерево для образа
# приложения и корней плагинов. Сборочные стадии берут полный python-dist
FROM ${GLIBC_IMAGE} AS python-slim

ARG PYTHON_VERSION=3.11

COPY --from=python-dist /opt/python/usr/local  /python/

RUN lib="/python/lib/python${PYTHON_VERSION}" \
    && rm -rf /python/include /python/share /python/lib/pkgconfig \
              /python/bin/2to3* /python/bin/idle3* /python/bin/pip* /python/bin/pydoc3* \
              /python/bin/python*-config \
              "$lib/test" "$lib/idlelib" "$lib/tkinter" "$lib/lib2to3" "$lib/ensurepip" \
              "$lib/config-${PYTHON_VERSION}-x86_64-linux-gnu" \
              "$lib/site-packages/pip" "$lib/site-packages/setuptools" \
              "$lib/site-packages/pkg_resources" "$lib/site-packages/"*.dist-info

RUN LD_LIBRARY_PATH=/python/lib PYTHONHOME=/python /python/bin/python3 -c "import ssl, sqlite3, lzma, bz2; print('python-slim ok')"


# закрытый контур: CA внутреннего TLS и индексы пакетов из build/conf.
# Стадии берут готовые деревья: /certs — всем, /index — только сборочным
FROM ${GLIBC_IMAGE} AS site-conf

COPY build/conf/  /tmp/conf/

RUN mkdir -p /certs/etc/ssl /certs/usr/local/share /index/etc/uv

RUN if [ -f /tmp/conf/ca-chain.crt ]; then \
        cp /tmp/conf/ca-chain.crt /usr/local/share/ca-certificates/ && update-ca-certificates; \
    fi
RUN cp -a /etc/ssl/certs /certs/etc/ssl/ \
    && cp -a /usr/local/share/ca-certificates /certs/usr/local/share/

RUN if [ -f /tmp/conf/pip.conf ]; then cp /tmp/conf/pip.conf /index/etc/pip.conf; fi
RUN if [ -f /tmp/conf/uv.toml ]; then cp /tmp/conf/uv.toml /index/etc/uv/uv.toml; fi


# среда сборки: gcc + наш python поверх системного, uv, CA и индексы контура
FROM ${GCC_IMAGE} AS base

# область кэша: make offline-check задаёт своё значение, и шаги стадии, а с ней и всех
# стадий от base, идут заново. Её же объявляют остальные стадии с сетью от образов-основ
ARG CACHE_SCOPE

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-dev                                              \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dist /opt/python  /
COPY --from=site-conf /certs/        /
COPY --from=site-conf /index/        /
COPY build/src/uv/uv        /usr/local/bin/uv

# число заданий компилирующих шагов: наследуют все сборочные стадии, uv читает сам
ARG JOBS=4
ENV JOBS=${JOBS} UV_CONCURRENT_BUILDS=${JOBS} UV_CONCURRENT_INSTALLS=${JOBS}

# закрытый контур: pip не спрашивает индекс о своей новой версии, uv не качает python
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 UV_PYTHON_DOWNLOADS=never

RUN ldconfig && gcc --version | head -n1 && python3 --version && pip3 --version

# у каждой программы свой способ узнать CA bundle
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt             \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt       \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt


# python-oracledb с патчем: sdist ровно той версии из индекса pypi с проверкой sha256,
# патч из scripts/, локальный суффикс версии — чтобы установщик не подменил колесо
# готовым с индекса. Колесо уходит и в образ (boba-wheels), и в src/ для .venv хоста
FROM base AS oracledb-wheel

ARG ORACLEDB_VERSION=26.0.0
ARG ORACLEDB_SHA256=d15728bae7546984203ae17f8296153d52d10ffdaf4929bebe46b70d5819d270
ARG ORACLEDB_LOCAL=boba.1
ARG CYTHON_VERSION=3.3.0
ARG SETUPTOOLS_VERSION=84.0.0
ARG WHEEL_VERSION=0.48.0

RUN apt-get update && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*

COPY build/scripts/oracledb-${ORACLEDB_VERSION}-arrow-duplicates.patch  /tmp/oracledb.patch

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
# проверка, что собранное колесо импортируется: зависимости драйвера — из индекса
RUN pip3 install --no-cache-dir /out/oracledb-*.whl \
    && python3 -c "import oracledb; print(oracledb.__version__)" | grep -q "+${ORACLEDB_LOCAL}$"


# bubblewrap статически с libcap: у meson bwrap нет такой опции, поэтому
# в сгенерированном build.ninja .so подменяется на .a
FROM base AS bwrap-build

ARG BWRAP_VERSION=0.11.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcap-dev meson ninja-build pkg-config xz-utils         \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/bubblewrap-${BWRAP_VERSION}.tar.xz  /tmp/bwrap.tar.xz

WORKDIR /tmp/bwrap-src
RUN tar -xJf /tmp/bwrap.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr -Dselinux=disabled -Dman=disabled \
    && sed -i 's|/usr/lib/x86_64-linux-gnu/libcap\.so|/usr/lib/x86_64-linux-gnu/libcap.a|g' _build/build.ninja \
    && ninja -j "${JOBS}" -C _build bwrap
RUN strip _build/bwrap && install -D -m 0755 _build/bwrap /out/bwrap

RUN ! ldd /out/bwrap | grep libcap && /out/bwrap --version


# fuse2fs статически с libfuse, плюс шаблон workspace тем же mke2fs, что уедет в релиз.
# Патч: монтирование ro открывает образ только на чтение — без него fuse2fs пишет суперблок
# образа, смонтированного ro, и не монтирует образ под ro-биндом (EROFS)
FROM base AS fuse2fs-build

ARG LIBFUSE_VERSION=3.12.0
ARG E2FSPROGS_VERSION=1.47.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        meson ninja-build patch pkg-config xz-utils              \
    && rm -rf /var/lib/apt/lists/*

COPY build/src/fuse-${LIBFUSE_VERSION}.tar.xz         /tmp/fuse.tar.xz
COPY build/src/e2fsprogs-${E2FSPROGS_VERSION}.tar.xz  /tmp/e2fsprogs.tar.xz
COPY build/scripts/e2fsprogs-${E2FSPROGS_VERSION}-ro-mount.patch  /tmp/e2fsprogs.patch

WORKDIR /tmp/fuse-src
RUN tar -xJf /tmp/fuse.tar.xz --strip-components=1
RUN meson setup _build --buildtype=release --prefix=/usr/local \
        -Ddefault_library=static -Dexamples=false -Dtests=false -Dutils=false \
    && ninja -j "${JOBS}" -C _build install

WORKDIR /tmp/e2fs-src
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/x86_64-linux-gnu/pkgconfig
RUN tar -xJf /tmp/e2fsprogs.tar.xz --strip-components=1
RUN patch -p1 < /tmp/e2fsprogs.patch
RUN LIBS="-lpthread -ldl" ./configure --enable-fuse2fs --disable-nls && make -j"${JOBS}"
RUN strip misc/fuse2fs && install -D -m 0755 misc/fuse2fs /out/fuse2fs

RUN ! ldd /out/fuse2fs | grep fuse && /out/fuse2fs -V

# mke2fs линкован только с libc: им make делает workspace.ext4 и rootfs плагинов вне сборки
RUN install -D -m 0755 misc/mke2fs /out/mke2fs && cp misc/mke2fs.conf /out/mke2fs.conf


# артефакты песочницы для make sandbox: make забирает /third и /tools через docker cp
FROM scratch AS sandbox-out

ARG LIBSTDCXX_VERSION=6.0.25

COPY --from=bwrap-build   /out/bwrap        /third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs      /third/bin/fuse2fs
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.6                    /third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION} /third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                     /third/lib/
COPY --from=fuse2fs-build /out/mke2fs       /tools/mke2fs
COPY --from=fuse2fs-build /out/mke2fs.conf  /tools/mke2fs.conf


# колёса всех пакетов репозитория и constraints из uv.lock: обе стадии установки
# получают версии хоста, иначе uv ставил бы свежайшие
FROM base AS boba-wheels

COPY pyproject.toml  /tmp/boba/pyproject.toml
COPY uv.lock         /tmp/boba/uv.lock
COPY packages/       /tmp/boba/packages/

COPY --from=oracledb-wheel /out/  /tmp/wheels/
# find-links корневого pyproject ведёт сюда: колесо с патчем лежит там же, где
# на хосте его держит build/src
COPY --from=oracledb-wheel /out/  /tmp/boba/build/src/oracledb/

WORKDIR /tmp/boba
RUN uv build --all-packages --wheel --no-sources -o /tmp/wheels
RUN uv export --frozen --all-packages --all-extras --no-emit-workspace --no-hashes \
        -o /tmp/wheels/constraints.txt
# uv pip install не читает [tool.uv] проекта: переопределения зависимостей,
# с которыми собран uv.lock, едут к установке приложения отдельным файлом
RUN python3 -c "import tomllib, pathlib; uv = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['uv']; print('\\n'.join(uv['override-dependencies']))" \
        > /tmp/wheels/overrides.txt


# --- образ корня одного плагина: make plugin-rootfs PLUGIN=<пакет> ---------------

# site-packages плагина: закрытие payload одного пакета и его сборочные декларации.
# Без PLUGIN (стадию проходят цели ниже) стадия отдаёт пустые заготовки, которые ждут
# plugin-root и plugin-site-out, а шаги плагина пропускаются
FROM base AS plugin-site

ARG PLUGIN

COPY --from=boba-wheels /tmp/wheels/          /tmp/wheels/
COPY --from=boba-wheels /tmp/boba/packages/   /tmp/packages/
COPY build/scripts/plugin_rootfs.py  /tmp/plugin_rootfs.py

RUN mkdir -p /opt/site /opt/code-sandbox /opt/plugin-root /opt/plugin-setup.d \
    && : > /opt/plugin-apt.txt && : > /opt/plugin-data.txt

RUN [ -z "${PLUGIN}" ] || { \
        python3 /tmp/plugin_rootfs.py names /tmp/packages ${PLUGIN} > /tmp/names.txt && cat /tmp/names.txt; }
RUN [ -z "${PLUGIN}" ] || uv pip install --python "$(command -v python3)" --target /opt/site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        $(cat /tmp/names.txt)

RUN [ -z "${PLUGIN}" ] || python3 /tmp/plugin_rootfs.py apt /tmp/packages ${PLUGIN} > /opt/plugin-apt.txt
RUN [ -z "${PLUGIN}" ] || python3 /tmp/plugin_rootfs.py data /tmp/packages ${PLUGIN} > /opt/plugin-data.txt

RUN [ -z "${PLUGIN}" ] || for root in $(python3 /tmp/plugin_rootfs.py root /tmp/packages ${PLUGIN}); do \
        cp -a "$root/." /opt/plugin-root/; \
    done

RUN [ -z "${PLUGIN}" ] || { index=0 \
    && for setup in $(python3 /tmp/plugin_rootfs.py setup /tmp/packages ${PLUGIN}); do \
           cp "$setup" "/opt/plugin-setup.d/$index.sh"; index=$((index + 1)); \
       done; }

ENV PYTHONPATH=/opt/site
RUN [ -z "${PLUGIN}" ] || for name in $(python3 /tmp/plugin_rootfs.py imports /tmp/packages ${PLUGIN}); do \
        python3 -c "import $name; print('$name ok')"; \
    done

RUN [ -z "${PLUGIN}" ] || { \
        mv /opt/site/boba /opt/code-sandbox/boba \
        && mv /opt/site/boba_*.dist-info /opt/code-sandbox/ \
        && echo /usr/src > /opt/site/boba-src.pth; }

# гость зиготы стартует в образе без биндов кода: контракт проверяется здесь
RUN [ -z "${PLUGIN}" ] || PYTHONPATH=/opt/code-sandbox:/opt/site python3 -c "import boba.sandbox.guest; print('guest ok')"


# site-packages и код плагина по путям корня: make plugin-rootfs забирает /usr этой
# стадии docker cp и стримит в mke2fs вторым tar'ом после plugin-root.
# Стадия стоит сразу за plugin-site: sandbox-base и plugin-root ради неё не собираются
FROM scratch AS plugin-site-out

ARG PYTHON_VERSION=3.11

COPY --from=plugin-site /opt/site          /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=plugin-site /opt/code-sandbox  /usr/src


# общая база корней плагинов: glibc, библиотеки интерпретатора python и kerberos,
# libstdc++ сборочного gcc и python. Одна на все плагины и версии; утилиты и
# нативные пакеты тела объявляет плагин ключом apt декларации
FROM ${GLIBC_IMAGE} AS sandbox-base

ARG CACHE_SCOPE
ARG LIBSTDCXX_VERSION=6.0.25
ARG LIBSTDCXX_OLD=6.0.22

COPY --from=site-conf /certs/  /

RUN apt-get update && apt-get install -y --no-install-recommends        \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7 \
        libncurses5 zlib1g                                              \
        libkrb5-3 libgssapi-krb5-2                                      \
    && rm -rf /var/lib/apt/lists/*

RUN rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.6 /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_OLD}
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  /usr/lib/x86_64-linux-gnu/libstdc++.so.6
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                      /lib/x86_64-linux-gnu/
COPY --from=python-slim /python/                                              /usr/local/
RUN ldconfig

RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

# /etc/krb5.conf — точка бинда krb5.conf из [sandbox] binds сетевых секций
RUN mkdir -p /usr/src /workspace /mnt/pgsock && touch /etc/krb5.conf


# корень одного плагина поверх базы: apt и оверлей из декларации, точки монтирования.
# site-packages сюда не копируются: make plugin-rootfs стримит эту стадию и
# plugin-site-out двумя tar'ами (docker cp в stdout) в mke2fs, ext4 нигде не оседает
FROM sandbox-base AS plugin-root

ARG BOBA_DIR=/app/boba
# каталоги krb dev-деревьев приложений через пробел: образ корня у плагина один
# на все приложения, точка монтирования нужна каждому
ARG SANDBOX_DEV_KRB=/app/boba/conf/krb

COPY --from=plugin-site /opt/plugin-apt.txt   /tmp/plugin-apt.txt
COPY --from=plugin-site /opt/plugin-setup.d/  /tmp/plugin-setup.d/
COPY --from=plugin-site /opt/plugin-data.txt  /tmp/plugin-data.txt

RUN if [ -s /tmp/plugin-apt.txt ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends $(tr '\n' ' ' < /tmp/plugin-apt.txt) \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=plugin-site /opt/plugin-root/  /
RUN for setup in /tmp/plugin-setup.d/*.sh; do \
        if [ -f "$setup" ]; then sh "$setup"; fi; \
    done

# чистка идёт до точек монтирования: /var/cache/* снёс бы созданный ниже
# /var/cache/fastembed, а в read-only корне bwrap его уже не создаст
RUN rm -rf /tmp/plugin-setup.d /tmp/plugin-apt.txt \
           /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

# данные (веса эмбеддера, модели OCR) монтирует рантайм: образ несёт точки монтирования
RUN for path in $(cat /tmp/plugin-data.txt); do mkdir -p "$path"; done \
    && for path in $(cat /tmp/plugin-data.txt); do test -d "$path"; done \
    && rm -f /tmp/plugin-data.txt
RUN mkdir -p "${BOBA_DIR}/conf/krb" ${SANDBOX_DEV_KRB}


# --- boba: агент каталогом standalone (make boba-bin, образы aid и agent) -----------

# boba-cli из колёс репозитория — вход компиляции: правка пакетов вне его закрытия даёт
# то же дерево, и шаг Nuitka берётся из кэша. Для этого дерево зависит только от
# содержимого колёс: нативные пакеты из sdist (gssapi, krb5) — без отладочной информации
# с путями временных каталогов, метки времени uv (uv_cache.json) — вон.
# BIN_ONNX — с рантаймом onnxruntime-genai. Без NUITKA_VERSION (стадию проходят цели
# ниже) стадия пуста
FROM base AS bin-site

ARG NUITKA_VERSION
ARG BIN_ONNX

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/

RUN mkdir -p /opt/bin-site && { [ -z "${NUITKA_VERSION}" ] || { \
        CFLAGS="-g0" uv pip install --python "$(command -v python3)" --target /opt/bin-site \
            --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
            --override /tmp/wheels/overrides.txt \
            boba-cli $( [ -n "${BIN_ONNX}" ] && echo "boba-llm-onnx[generation]" ) \
        && find /opt/bin-site -name uv_cache.json -delete \
        && sed -i '/uv_cache\.json,/d' /opt/bin-site/*.dist-info/RECORD; }; }


# boba каталогом standalone: Nuitka и patchelf из индекса pypi образа; каталог
# boba.dist самодостаточен — gssapi (через транспорт http), данные пакетов и, по флагу
# BIN_ONNX, рантайм onnxruntime-genai входят целиком; libpython линкуется статически.
# Каталог, а не один файл: onefile на каждом запуске распаковывает себя (163 МБ) во
# временный каталог, а в режиме кэша читает его целиком и сверяет CRC; каталогу не нужно
# ни того ни другого, и ему не нужна запись — годится для корня только на чтение. Данные пакетов boba
# называет опись boba.cli.package_data по RECORD дерева колёс: она же даёт флаги Nuitka
# и едет в бинарник, а `boba self-check` в конце шага проверяет по ней, что все файлы
# внутри, — пропущенный каталог данных роняет сборку. onnxruntime_genai
# находит onnxruntime через find_spec, не импортом, поэтому пакет onnxruntime с его
# библиотеками включается явно. Локальные модели внутрь не попадают: веса лежат в
# каталоге models/ релиза. Без NUITKA_VERSION стадия отдаёт пустой /out
FROM base AS boba-bin

ARG NUITKA_VERSION
ARG PATCHELF_VERSION
ARG ORDERED_SET_VERSION
ARG BIN_ONNX

RUN mkdir -p /out /opt/bin-tools && { [ -z "${NUITKA_VERSION}" ] || \
        uv pip install --python "$(command -v python3)" --target /opt/bin-tools \
            "nuitka==${NUITKA_VERSION}" "patchelf==${PATCHELF_VERSION}" \
            "ordered-set==${ORDERED_SET_VERSION}"; }

COPY build/conf/nuitka-boba.yml       /tmp/nuitka-boba.yml
COPY --from=bin-site /opt/bin-site/   /opt/bin-site/

ENV PYTHONPATH=/opt/bin-site:/opt/bin-tools PATH=/opt/bin-tools/bin:${PATH}
# сборочные каталоги Nuitka остаются в /tmp и уходят тем же шагом: в /out — только каталог
# boba.dist с исполняемым boba
RUN [ -z "${NUITKA_VERSION}" ] || { onnx=""; \
    if [ -n "${BIN_ONNX}" ]; then \
        onnx="--include-package=onnxruntime_genai --include-package-data=onnxruntime_genai \
              --include-package=onnxruntime"; \
    fi; \
    python3 -m boba.cli.package_data /opt/bin-site /tmp/package_data.json > /tmp/package_data.flags \
    && python3 -m nuitka --standalone --static-libpython=yes --output-filename=boba.bin --update-check=never \
        --output-dir=/tmp/nuitka --jobs=${JOBS} \
        --user-package-configuration-file=/tmp/nuitka-boba.yml \
        --include-package=gssapi --nofollow-import-to=gssapi.tests --include-distribution-metadata=boba-cli \
        $(cat /tmp/package_data.flags) ${onnx} \
        /opt/bin-site/boba/cli/app.py \
    && mv /tmp/nuitka/app.dist /out/boba.dist \
    && rm -rf /tmp/nuitka /tmp/package_data.json /tmp/package_data.flags \
    && /out/boba.dist/boba.bin --help > /dev/null \
    && /out/boba.dist/boba.bin self-check; }


# каталог агента для make boba-bin: make забирает /boba.dist через docker cp
FROM scratch AS boba-bin-out

COPY --from=boba-bin /out/  /


# --- образы приложений -----------------------------------------------------------------

# site-packages приложения: ставится по своему графу зависимостей, парсеры и
# эмбеддер сюда не попадают — они объявлены как extra payload у tool-пакетов.
# Состав задаёт make build: APP_PACKAGES и APP_IMPORTS
FROM base AS app-site

ARG APP_PACKAGES
ARG APP_IMPORTS

COPY --from=boba-wheels /tmp/wheels/  /tmp/wheels/

RUN uv pip install --python "$(command -v python3)" --target /opt/app-site \
        --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
        --override /tmp/wheels/overrides.txt \
        ${APP_PACKAGES}

ENV PYTHONPATH=/opt/app-site
RUN for name in ${APP_IMPORTS}; do python3 -c "import $name; print('$name ok')"; done
RUN pip3 list --path /opt/app-site --format=freeze | grep '^boba-' | cut -d= -f1 > /opt/app-packages.txt


# общая основа образов приложений: python и библиотеки. Код приложения стадии образов
# кладут последним слоем — его правка не трогает слои ниже
FROM ${GLIBC_IMAGE} AS runtime-os

ARG CACHE_SCOPE
ARG PYTHON_VERSION=3.11
ARG LIBSTDCXX_VERSION=6.0.25
ARG BOBA_DIR=/app/boba
ARG HEALTH_PATH=/

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 libffi6 libsqlite3-0 libbz2-1.0                \
        liblzma5 libreadline7 libncurses5 zlib1g                 \
        libkrb5-3 libgssapi-krb5-2 krb5-user                     \
    && rm -rf /var/lib/apt/lists/*

COPY --from=site-conf /certs/                                                 /
COPY --from=python-slim /python/                                              ${BOBA_DIR}/third/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.6                     ${BOBA_DIR}/third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libstdc++.so.${LIBSTDCXX_VERSION}  ${BOBA_DIR}/third/lib/
COPY --from=base /usr/lib/x86_64-linux-gnu/libgcc_s.so.1                      ${BOBA_DIR}/third/lib/
COPY build/scripts/healthcheck.py                                    ${BOBA_DIR}/app/healthcheck.py

RUN mkdir -p ${BOBA_DIR}/conf ${BOBA_DIR}/app_root/public

RUN chmod -R go-w ${BOBA_DIR}/third

WORKDIR /app
ENV BOBA_DIR=${BOBA_DIR} \
    BOBA_HEALTH_PATH=${HEALTH_PATH}

# порт, префикс и сам python приходят из boba.env: проба идёт тем же интерпретатором
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python3 "${BOBA_DIR}/app/healthcheck.py"

ENTRYPOINT ["python3"]


# --- boba-mcp: MCP-сервер с инструментами boba -------------------------------------

# основа приложений, которые исполняют инструменты в песочнице (mcp): bwrap, fuse2fs
# и каталог образов корней плагинов и workspace. Образы кладёт поверх неё
# build/sandbox.Dockerfile — это и есть SANDBOX_IMAGE стадии runtime-mcp
FROM runtime-os AS runtime-sandbox

ARG BOBA_DIR=/app/boba

COPY --from=bwrap-build /out/bwrap      ${BOBA_DIR}/third/bin/bwrap
COPY --from=fuse2fs-build /out/fuse2fs  ${BOBA_DIR}/third/bin/fuse2fs

RUN mkdir -p ${BOBA_DIR}/sandbox

# TrustedBinaries не примет third/bin, доступный на запись посторонним
RUN chmod -R go-w ${BOBA_DIR}/third/bin ${BOBA_DIR}/sandbox \
    && test -z "$(find ${BOBA_DIR}/third/bin -maxdepth 1 -type f -perm /022)" \
    && ${BOBA_DIR}/third/bin/bwrap --version \
    && ${BOBA_DIR}/third/bin/fuse2fs -V


# образ сервиса: образы корней плагинов и шаблон workspace внутри (снаружи приложению
# нужны только конфиг и данные, и пересборка плагинов не меняет файлы под живым
# контейнером), общий конфиг внутри, site.toml и данные приходят деревом compose/mcp
FROM ${SANDBOX_IMAGE} AS runtime-mcp

ARG BOBA_DIR=/app/boba
ARG BOBA_UID=1000
ARG BOBA_GID=1000

COPY packages/apps/boba-mcp/conf/  ${BOBA_DIR}/conf/

COPY --from=app-site /opt/app-site          ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt  ${BOBA_DIR}/app/packages.txt
# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

EXPOSE 8650
USER ${BOBA_UID}:${BOBA_GID}
CMD ["-m", "boba.mcp_server", "--config", "/app/boba/conf/config.toml", "--site", "/app/boba/conf/site.toml"]


# --- агент как служба: boba serve --------------------------------------------------

# образ службы агента: агент каталогом из стадии boba-bin сам слушает ACP по сети
# (`boba serve`), сессии всех подключений — потоки одного процесса. app-site — тот же
# boba-cli модулем: им проверяется образ и работает установка без Docker. Песочницы и
# моделей нет: агент ходит к модели по сети, инструменты ему дают клиенты по
# MCP-over-ACP. Общий конфиг внутри, site.toml и данные приходят деревом compose/agent
FROM runtime-os AS runtime-agent

ARG BOBA_DIR=/app/boba
ARG BOBA_UID=1000
ARG BOBA_GID=1000

COPY packages/apps/boba-cli/conf/  ${BOBA_DIR}/conf/
COPY --from=boba-bin /out/         ${BOBA_DIR}/bin/

COPY --from=app-site /opt/app-site          ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt  ${BOBA_DIR}/app/packages.txt
# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

EXPOSE 8660
USER ${BOBA_UID}:${BOBA_GID}
ENTRYPOINT ["/app/boba/bin/boba.dist/boba.bin"]
CMD ["serve", "--config", "/app/boba/conf/config.toml", "--site", "/app/boba/conf/site.toml"]


# --- boba-aid: чат -------------------------------------------------------------------

# UI chainlit (фронт + copilot) из исходников тега с overlay web/chainlit-ui поверх;
# зависимости pnpm берёт из реестра npm образа nodejs. Store pnpm живёт только в этом
# шаге и удаляется, в слой попадает лишь /out/ui
FROM ${NODE_IMAGE} AS chainlit-ui-build

ARG CACHE_SCOPE
ARG CHAINLIT_VERSION

COPY build/scripts/chainlit_ui.sh                   /tmp/chainlit_ui.sh
COPY build/src/chainlit-${CHAINLIT_VERSION}.tar.gz  /tmp/chainlit.tar.gz
COPY packages/apps/boba-aid/web/chainlit-ui/overlay/  /tmp/overlay/
COPY packages/apps/boba-aid/assets/public/elements/   /tmp/elements/

RUN sh /tmp/chainlit_ui.sh /tmp/chainlit.tar.gz /tmp/overlay /tmp/elements /out/ui \
    && rm -rf /tmp/chainlit-ui


# скрипт страницы chainlit (web/page, TypeScript)
FROM ${NODE_IMAGE} AS aid-page-build

ARG CACHE_SCOPE
# закрытый контур: npm не спрашивает реестр о своей новой версии
ENV npm_config_update_notifier=false

COPY packages/apps/boba-aid/web/page/  /src/web/page/

WORKDIR /src/web/page
RUN npm ci --no-audit --no-fund && npm run check && npm run build


# образ чата: общий конфиг, ассеты chainlit, vendor-шрифты и стили формул вместо CDN,
# страница песочницы MCP Apps со шрифтом чата рядом (его берут приложения).
# Агента в образе нет: чат ходит в службу агента (образ agent). Вложения сессий
# приложение держит в данных ([aid].files_dir), app_root не пишется
FROM runtime-os AS runtime-aid

ARG BOBA_DIR=/app/boba
ARG BOBA_UID=1000
ARG BOBA_GID=1000

COPY --chown=${BOBA_UID}:${BOBA_GID} packages/apps/boba-aid/assets/.chainlit/config.toml   ${BOBA_DIR}/app_root/.chainlit/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/apps/boba-aid/assets/.chainlit/translations/ ${BOBA_DIR}/app_root/.chainlit/translations/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/apps/boba-aid/assets/chainlit.md             ${BOBA_DIR}/app_root/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/apps/boba-aid/assets/public/                 ${BOBA_DIR}/app_root/public/
COPY --chown=${BOBA_UID}:${BOBA_GID} packages/apps/boba-aid/assets/mcp-apps/               ${BOBA_DIR}/app_root/mcp-apps/
COPY --chown=${BOBA_UID}:${BOBA_GID} build/src/webassets/inter/files/                    ${BOBA_DIR}/app_root/mcp-apps/fonts/inter/files/
COPY --chown=${BOBA_UID}:${BOBA_GID} --from=aid-page-build /src/assets/public/page.js                  ${BOBA_DIR}/app_root/public/page.js
COPY --chown=${BOBA_UID}:${BOBA_GID} --from=chainlit-ui-build /out/ui/                            ${BOBA_DIR}/app_root/ui/
COPY --chown=${BOBA_UID}:${BOBA_GID} build/src/webassets/                                ${BOBA_DIR}/app_root/public/vendor/

# файлы уже принадлежат пользователю, каталогам владелец ставится без рекурсии
RUN chown ${BOBA_UID}:${BOBA_GID} ${BOBA_DIR}/app_root \
       ${BOBA_DIR}/app_root/.chainlit ${BOBA_DIR}/app_root/public

COPY packages/apps/boba-aid/conf/  ${BOBA_DIR}/conf/

COPY --from=app-site /opt/app-site          ${BOBA_DIR}/app/
COPY --from=app-site /opt/app-packages.txt  ${BOBA_DIR}/app/packages.txt
# шебанги стадии сборки ведут на /usr/local/bin/python3, которого здесь нет
RUN sed -i "1s|^#!.*python3.*|#!${BOBA_DIR}/third/bin/python3|" ${BOBA_DIR}/app/bin/*

EXPOSE 8501
USER ${BOBA_UID}:${BOBA_GID}
CMD ["-m", "boba.aid.main", "--config", "/app/boba/conf/config.toml", "--site", "/app/boba/conf/site.toml"]
