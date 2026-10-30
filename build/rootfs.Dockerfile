# Шаг prepare: корень песочницы для набора плагинов (PLUGINS — id через пробел, те,
# чей конфиг называет этот rootfs). Содержимое — объединение деклараций
# [tool.boba.sandbox] их пакетов: apt, точки данных, оверлей, setup. python с деревом
# payload ставится плагинам с python = true (bash) и всем при PYTHON=1 — это корень
# для тестов из исходников. make превращает дерево итога в ext4 (build/mkrootfs.sh).
# Сеть — только apt и pypi контура
ARG BUILD_IMAGE
ARG OS_IMAGE

# декларации и деревья, которые лягут в корень: python образа сборки, uv и колёса
FROM ${BUILD_IMAGE} AS decl

ARG PLUGINS
ARG PYTHON
ARG PYTHON_VERSION=3.11

RUN mkdir -p /out/tree /out/setup \
    && cd /tmp/boba/packages \
    && for command in apt libs data python; do \
           python3 /opt/plugin_rootfs.py "$command" . ${PLUGINS} > "/out/$command.txt"; \
       done \
    && for root in $(python3 /opt/plugin_rootfs.py root . ${PLUGINS}); do cp -a "$root/." /out/tree/; done \
    && index=0 && for setup in $(python3 /opt/plugin_rootfs.py setup . ${PLUGINS}); do \
           cp "$setup" "/out/setup/$index.sh"; index=$((index + 1)); \
       done

# libstdc++ сборочного gcc новее, чем у glibc образа: его ждут программы Nuitka и
# python-пакеты. Копия по настоящему имени, без номера версии в сборке
RUN mkdir -p /out/tree/usr/lib/x86_64-linux-gnu \
    && cp "$(readlink -f /usr/lib/x86_64-linux-gnu/libstdc++.so.6)" /out/tree/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
    && cp /usr/lib/x86_64-linux-gnu/libgcc_s.so.1 /out/tree/usr/lib/x86_64-linux-gnu/libgcc_s.so.1

# python и дерево payload: у плагинов с python = true — их пакеты, при PYTHON=1 —
# все плагины набора, код пакетов boba — в /usr/src (стенд тестов монтирует туда
# пакеты репозитория)
RUN if [ -n "${PYTHON}" ]; then python3 /opt/plugin_rootfs.py names . ${PLUGINS} > /tmp/names.txt; \
    elif [ -s /out/python.txt ]; then python3 /opt/plugin_rootfs.py names . ${PLUGINS} > /tmp/names.txt; \
    else : > /tmp/names.txt; fi \
    && if [ -s /tmp/names.txt ]; then \
           site="/out/tree/usr/local/lib/python${PYTHON_VERSION}/site-packages" \
           && mkdir -p /out/tree/usr/local "$site" \
           && cp -a /opt/python-slim/. /out/tree/usr/local/ \
           && uv pip install --python "$(command -v python3)" --target "$site" \
               --constraint /tmp/wheels/constraints.txt --find-links=/tmp/wheels \
               --override /tmp/wheels/overrides.txt $(cat /tmp/names.txt) \
           && rm -rf "$site/bin" \
           && echo python > /out/runtime.txt; \
       else : > /out/runtime.txt; fi \
    && if [ -n "${PYTHON}" ]; then \
           site="/out/tree/usr/local/lib/python${PYTHON_VERSION}/site-packages" \
           && mkdir -p /out/tree/usr/src \
           && mv "$site/boba" "$site"/boba_*.dist-info /out/tree/usr/src/ \
           && echo /usr/src > "$site/boba-src.pth" \
           && cat /out/libs.txt >> /out/apt.txt; \
       fi


# корень: голый glibc с CA (стадия os base.Dockerfile), системные пакеты деклараций,
# оверлей, точки монтирования
FROM ${OS_IMAGE} AS root

COPY --from=decl /out/apt.txt /out/data.txt /out/runtime.txt  /tmp/decl/
COPY --from=decl /out/setup/      /tmp/decl/setup/

RUN apt="$(cat /tmp/decl/apt.txt)" \
    && if [ -s /tmp/decl/runtime.txt ]; then \
           apt="$apt libssl1.1 libffi6 libsqlite3-0 libbz2-1.0 liblzma5 libreadline7 libncurses5 zlib1g libkrb5-3 libgssapi-krb5-2"; \
       fi \
    && if [ -n "$(echo $apt)" ]; then \
           apt-get update && apt-get install -y --no-install-recommends $apt \
           && rm -rf /var/lib/apt/lists/*; \
       fi

RUN rm -f /usr/lib/x86_64-linux-gnu/libstdc++.so.6 /usr/lib/x86_64-linux-gnu/libgcc_s.so.1
COPY --from=decl /out/tree/  /
RUN ldconfig

RUN for setup in /tmp/decl/setup/*.sh; do if [ -f "$setup" ]; then sh "$setup"; fi; done

# чистка до точек монтирования: /var/cache/* снёс бы точки данных
RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
           /usr/share/lintian /var/cache/* /var/log/*

RUN for path in $(cat /tmp/decl/data.txt); do mkdir -p "$path"; done \
    && mkdir -p /plugin /workspace /mnt/pgsock /usr/src \
    && touch /etc/krb5.conf \
    && rm -rf /tmp/decl

# glibc образа ищет локали в /usr/lib/x86_64-linux-gnu/locale, а libc-bin кладёт
# C.UTF-8 в /usr/lib/locale; LANG=C.UTF-8 песочницы без неё роняет программу на старте
RUN cp -a /usr/lib/locale/C.UTF-8 /usr/lib/x86_64-linux-gnu/locale/C.UTF-8 \
    && [ "$(env -i LANG=C.UTF-8 locale charmap)" = UTF-8 ]
