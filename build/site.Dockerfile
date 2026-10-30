# Шаг prepare: дерево пакетов программы (PACKAGES) или группы плагинов (PLUGINS — id через
# пробел) в /opt/site. У плагинов в дерево идёт замыкание их payload и поставщиков
# потребляемых групп entry points, а системные библиотеки ключа libs их деклараций
# ставятся в образ: Nuitka импортирует пакеты при сборке и кладёт копии этих
# библиотек в программу. Нативные пакеты из sdist — без отладочной информации,
# метки времени uv — вон: дерево зависит только от содержимого колёс
ARG BUILD_IMAGE
FROM ${BUILD_IMAGE}

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
