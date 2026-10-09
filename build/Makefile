# Единая сборка boba: приложения chainlit, studio и mcp и образы корней плагинов. Из сети берутся две вещи: артефакты цели fetch (github,
# huggingface, kernel.org — в src/) и пакеты pypi/npm при сборке — из реестров образов
# (в закрытом контуре nexus: conf/pip.conf, conf/uv.toml, npmrc образа nodejs).
# Остальные цели работают из src/ и локальных образов.

# от каталога Makefile, а не от cwd: make -f build/Makefile из корня искал бы
# test/ и conf/ рядом с собой
# путь запоминается сразу: после include последним в MAKEFILE_LIST стоит уже не Makefile
THIS_MAKEFILE   := $(abspath $(lastword $(MAKEFILE_LIST)))
BASE_DIR        ?= $(patsubst %/,%,$(dir $(THIS_MAKEFILE)))
SRC_DIR         ?= $(abspath $(BASE_DIR)/..)
# вложенные вызовы: make -f build/Makefile не подставляет его в $(MAKE)
MAKEFILE        := $(BASE_DIR)/Makefile
BUILD_CONTEXT   ?= $(SRC_DIR)
TEST_DIR        := $(BASE_DIR)/test
SCRIPTS_DIR     := $(BASE_DIR)/scripts
CONF_DIR        := $(BASE_DIR)/conf
# значения переменных сборки для этого хоста (в git не попадает): например
# WEB_DEV_NETWORK и WEB_DEV_HOST для демона без проброса портов
-include $(CONF_DIR)/local.mk
# артефакты для сборки: tarball'ы, uv, webassets, колесо oracledb
DEPS_DIR        ?= $(BASE_DIR)/src
# runtime-зависимости приложений: модели, бинарники и библиотеки песочницы, её образы.
# Экземпляр один на репозиторий: его монтирует compose, читает отладка и берёт в
# контекст сборка образа
RUNTIME_DIR     := $(SRC_DIR)/runtime
MODELS_DIR      := $(RUNTIME_DIR)/models
THIRD_DIR       := $(RUNTIME_DIR)/third
RELEASE_DIR     := $(SRC_DIR)/release

# приложения сборки; цели build, test, dev и release работают по одному приложению
# (APP=<имя> либо <цель>-<имя>), без APP — по всем
APPS            := chainlit studio mcp
APP             ?=

# чат инструментов не исполняет: песочницы, cgroup и моделей плагинов у него нет
PACKAGES_chainlit = boba-chainlit boba-llm-http boba-llm-onnx[generation]
IMPORTS_chainlit := boba.chainlit onnxruntime_genai
MODULE_chainlit  := boba.chainlit.main
STAGE_chainlit   := runtime-chainlit
HEALTH_chainlit  := /
PORT_chainlit    := 8501
PREFIX_chainlit  := /boba
SANDBOX_chainlit :=
MODELS_chainlit  := onnx-genai
PARTS_chainlit   := app_root/public app_root/public/page.js app_root/public/vendor \
                    app_root/ui/index.html app_root/ui/index.js

PACKAGES_studio   = boba-studio $(PLUGIN_PACKAGES) boba-llm-http boba-llm-onnx boba-llm-fastembed
IMPORTS_studio   := boba.studio
MODULE_studio    := boba.studio
STAGE_studio     := runtime-studio
HEALTH_studio    := /api/openapi.json
PORT_studio      := 8502
PREFIX_studio    := /boba-studio
SANDBOX_studio   := yes
MODELS_studio    := fastembed rapidocr
PARTS_studio     := app_root/public app_root/public/workflow app_root/public/vendor

# boba-mcp — MCP-сервер с инструментами boba; фронта у него нет
PACKAGES_mcp      = boba-mcp $(PLUGIN_PACKAGES) boba-llm-fastembed
IMPORTS_mcp      := boba.mcp_server.app fastmcp
MODULE_mcp       := boba.mcp_server
STAGE_mcp        := runtime-mcp
HEALTH_mcp       := /health
PORT_mcp         := 8650
PREFIX_mcp       :=
SANDBOX_mcp      := yes
# образы корней плагинов и шаблон workspace лежат внутри образа приложения
EMBED_mcp        := yes
MODELS_mcp       := fastembed rapidocr
PARTS_mcp        :=

# лимиты пользовательского юнита установки без Docker: те же, что у контейнера compose
UNIT_LIMITS_chainlit := 4G 400% 512
UNIT_LIMITS_studio   := 16G 800% 2048
UNIT_LIMITS_mcp      := 16G 800% 2048

APP_PACKAGES      = $(PACKAGES_$(APP))
APP_IMPORTS       = $(IMPORTS_$(APP))
APP_MODULE        = $(MODULE_$(APP))
APP_STAGE         = $(STAGE_$(APP))
APP_HEALTH        = $(HEALTH_$(APP))
APP_PORT          = $(PORT_$(APP))
APP_PREFIX        = $(PREFIX_$(APP))
APP_SANDBOX       = $(SANDBOX_$(APP))
APP_EMBED         = $(EMBED_$(APP))
APP_MODELS        = $(MODELS_$(APP))
# общий конфиг приложения и его плагины: в пакете приложения, в git
APP_CONF          = $(SRC_DIR)/packages/apps/boba-$(APP)/conf

ifneq ($(APP),)
ifeq ($(filter $(APP),$(APPS)),)
$(error APP=$(APP): expected one of $(APPS))
endif
endif

.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL           := /bin/bash
.SHELLFLAGS     := -eu -o pipefail -c

# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set);
# скрипты сборки зовутся python'ом venv репозитория, системный может быть старым
PYTHON          ?= $(SRC_DIR)/.venv/bin/python
VERSION_PY      := $(PYTHON) $(SCRIPTS_DIR)/version.py
VERSION         := $(shell $(VERSION_PY) show $(SRC_DIR)/packages)
# плагины приложения — пакеты с entry points группы boba.tools; списка в extras нет
PLUGIN_PACKAGES := $(shell $(PYTHON) $(SCRIPTS_DIR)/plugin_rootfs.py list $(SRC_DIR)/packages)
BOBA_TAG        ?= $(VERSION)

# корень установки на сервере и данные, переживающие релизы; тот же корень в образе
# экземпляр установки: имя каталога, юнитов и среза. По умолчанию — имя приложения;
# второй узел того же приложения на одной машине получает своё имя
INSTANCE        ?= $(APP)
INSTALL_ROOT    ?= /app
# 1 — release кладёт в third/lib системные библиотеки образа: дерево запускается на
# системе, отличной от системы образа (нужна только glibc не старше, чем в образе)
BUNDLE_OS_LIBS  ?=
INSTALL_DIR      = $(INSTALL_ROOT)/boba-$(INSTANCE)
# дефис в имени среза systemd означает вложенность (boba-a-b.slice лежит в boba-a.slice),
# и каталог cgroup экземпляра оказался бы не там, где его ищут юниты
ifneq ($(findstring -,$(INSTANCE)),)
$(error INSTANCE=$(INSTANCE): expected a name without "-", for example $(APP)2)
endif
DATA_DIR         = $(INSTALL_DIR)/data
DEST_DIR        ?= /app/boba

IMAGE            = boba-$(APP):$(BOBA_TAG)
RELEASE_IMAGE    = $(IMAGE)
OUT              = $(RELEASE_DIR)/$(VERSION)/$(INSTANCE)

# режим sec.sh: all, sast, sca или rules
MODE            ?= all

PYTHON_VERSION  ?= 3.11
GLIBC_IMAGE     ?= dmp/glibc:2.28
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
NODE_IMAGE      ?= dmp/nodejs:20
STUDIO_NODE_IMAGE ?= dmp/nodejs:22
BASE_IMAGE      := boba-base:$(PYTHON_VERSION)
SANDBOX_DIR     := $(RUNTIME_DIR)/sandbox
# сколько build cache оставляет prune
empty           :=
space           := $(empty) $(empty)
PRUNE_KEEP      ?= 20GB

# --- что скачивает fetch -------------------------------------------------------

UV_VERSION      ?= 0.11.21
UV_PLATFORM     ?= x86_64-unknown-linux-musl
UV_URL          ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz
UV_BIN          := $(DEPS_DIR)/uv/uv

BWRAP_VERSION   ?= 0.11.2
BWRAP_URL       ?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
BWRAP_TARBALL   := $(DEPS_DIR)/bubblewrap-$(BWRAP_VERSION).tar.xz
# 3.12.0 — последний libfuse, которому хватает meson 0.49 из apt Astra 2.12
LIBFUSE_VERSION ?= 3.12.0
LIBFUSE_URL     ?= https://github.com/libfuse/libfuse/releases/download/fuse-$(LIBFUSE_VERSION)/fuse-$(LIBFUSE_VERSION).tar.xz
LIBFUSE_TARBALL := $(DEPS_DIR)/fuse-$(LIBFUSE_VERSION).tar.xz
E2FSPROGS_VERSION ?= 1.47.2
E2FSPROGS_URL   ?= https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v$(E2FSPROGS_VERSION)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
E2FSPROGS_TARBALL := $(DEPS_DIR)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
SANDBOX_WS_SIZE ?= 1G

RAPIDOCR_VERSION ?= 3.9.2
RAPIDOCR_LANGS  ?= eslav
RAPIDOCR_REPO   ?= https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v$(RAPIDOCR_VERSION)/onnx
RAPIDOCR_FILES  := $(MODELS_DIR)/rapidocr/ch_PP-OCRv5_det_mobile.onnx \
                   $(MODELS_DIR)/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx \
                   $(RAPIDOCR_LANGS:%=$(MODELS_DIR)/rapidocr/%_PP-OCRv5_rec_mobile.onnx)

# версии качалок моделей (fastembed, huggingface_hub) и их зависимостей — из uv.lock,
# теми же constraints, что у образов (стадия boba-wheels): свежий huggingface_hub
# раскладывает кэш так, что onnxruntime из uv.lock модель не открывает
FETCH_CONSTRAINTS := $(DEPS_DIR)/constraints.txt
EMBED_MODELS    ?= intfloat/multilingual-e5-large
FASTEMBED_STAMPS := $(foreach m,$(EMBED_MODELS),$(MODELS_DIR)/fastembed/.$(subst /,--,$(m)))

# модели переформулировщика: onnxruntime-genai держит каждую каталогом
# genai_config.json + веса + токенайзер; какую берёт профиль, решает config.toml
ONNX_HUB_URL    ?= https://huggingface.co
ONNX_MODELS     ?= qwen3-0.6b-int4 qwen3-1.7b-int4 qwen3-4b-int4
ONNX_REPO_qwen3-0.6b-int4   := xiaoyao9184/Qwen3-0.6B-onnx-genai
ONNX_SUBDIR_qwen3-0.6b-int4 := cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4/
ONNX_REPO_qwen3-1.7b-int4   := Hug0007/qwen3-1.7b-int4-onnx-genai
ONNX_SUBDIR_qwen3-1.7b-int4 :=
ONNX_REPO_qwen3-4b-int4     := robinsmits/qwen3-4b-instruct-2507-onnx-int4
ONNX_SUBDIR_qwen3-4b-int4   :=
ONNX_DIRS       := $(ONNX_MODELS:%=$(MODELS_DIR)/onnx-genai/%)

# статика фронта: KaTeX (формулы), Inter (шрифт UI), mermaid (диаграммы) вместо CDN
WEBASSETS_DIR   := $(DEPS_DIR)/webassets
KATEX_VERSION   ?= 0.16.22
KATEX_URL       ?= https://github.com/KaTeX/KaTeX/releases/download/v$(KATEX_VERSION)/katex.tar.gz
INTER_VERSION   ?= 5.3.0
INTER_URL       ?= https://registry.npmjs.org/@fontsource/inter/-/inter-$(INTER_VERSION).tgz
INTER_WEIGHTS   ?= 400 500 700
MERMAID_VERSION ?= 11.16.1
MERMAID_URL     ?= https://registry.npmjs.org/mermaid/-/mermaid-$(MERMAID_VERSION).tgz
WEB_FILES       := $(WEBASSETS_DIR)/katex/katex.min.css $(WEBASSETS_DIR)/inter/inter.css $(WEBASSETS_DIR)/mermaid/mermaid.min.js

# python-oracledb с патчем (build/scripts/oracledb-*.patch): sdist ровно этой
# версии с индекса pypi, sha256 из uv.lock, сборка в образе base; локальный суффикс
# версии пинится в boba-db-oracle, колесо для .venv хоста лежит в src/oracledb
ORACLEDB_VERSION ?= 26.0.0
ORACLEDB_SHA256  ?= d15728bae7546984203ae17f8296153d52d10ffdaf4929bebe46b70d5819d270
ORACLEDB_LOCAL   ?= boba.1
CYTHON_VERSION   ?= 3.3.0
ORACLEDB_DIR     := $(DEPS_DIR)/oracledb
ORACLEDB_WHEEL   := $(ORACLEDB_DIR)/oracledb-$(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL)-cp$(subst .,,$(PYTHON_VERSION))-cp$(subst .,,$(PYTHON_VERSION))-linux_x86_64.whl

# исходники chainlit: UI собирается из тега, версия — в UPSTREAM рядом с overlay
WEB_DIR         := $(SRC_DIR)/packages/apps/boba-chainlit
UI_DIR          := $(WEB_DIR)/web/chainlit-ui
CHAINLIT_VERSION := $(shell cat $(UI_DIR)/UPSTREAM)
CHAINLIT_SRC_URL ?= https://codeload.github.com/Chainlit/chainlit/tar.gz/refs/tags/$(CHAINLIT_VERSION)
CHAINLIT_TARBALL := $(DEPS_DIR)/chainlit-$(CHAINLIT_VERSION).tar.gz

INPUTS          := $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL) $(UV_BIN) \
                   $(RAPIDOCR_FILES) $(FASTEMBED_STAMPS) $(ONNX_DIRS) $(WEB_FILES) $(CHAINLIT_TARBALL) \
                   $(ORACLEDB_WHEEL)

# --- фронт -----------------------------------------------------------------------

ASSETS_DIR      := $(WEB_DIR)/assets
ASSETS_VENDOR   := $(ASSETS_DIR)/public/vendor
# скрипт страницы: в образ его собирает стадия web-build, в dev-дерево — цель web
WEB_PROJECT     := $(WEB_DIR)/web/page
PAGE_JS         := $(ASSETS_DIR)/public/page.js
PAGE_SRCS       := $(shell find $(WEB_PROJECT)/src -type f) \
                   $(wildcard $(WEB_PROJECT)/*.json $(WEB_PROJECT)/*.ts $(WEB_PROJECT)/*.js)
# UI chainlit (фронт + copilot) с overlay: в образ его собирает стадия chainlit-ui-build
UI_DIST         := $(ASSETS_DIR)/ui
UI_SRCS         := $(shell find $(UI_DIR) -type f) $(SCRIPTS_DIR)/chainlit_ui.sh

# страница workflow studio: в образ её собирает стадия studio-web-build, в dev-дерево —
# цель web-studio; типы api берутся из openapi.json, который печатает приложение
STUDIO_DIR      := $(SRC_DIR)/packages/apps/boba-studio
STUDIO_ASSETS   := $(STUDIO_DIR)/assets
STUDIO_PROJECT  := $(STUDIO_DIR)/web/workflow
STUDIO_DIST     := $(STUDIO_ASSETS)/workflow
STUDIO_SRCS     := $(shell find $(STUDIO_PROJECT)/src -type f -not -name schema.d.ts) \
                   $(wildcard $(STUDIO_PROJECT)/*.json $(STUDIO_PROJECT)/*.ts $(STUDIO_PROJECT)/*.js $(STUDIO_PROJECT)/index.html)
OPENAPI_JSON    := $(STUDIO_PROJECT)/openapi.json
# vite dev-сервер страницы: make web-dev
WEB_DEV_NAME    ?= boba-web-dev
WEB_DEV_PORT    ?= 5173
BOBA_URL_PREFIX ?= /boba-studio-debug
# где vite слышен с хоста. По умолчанию порт контейнера публикуется на 127.0.0.1.
# Демон без проброса портов (rootless с port driver none) так не умеет: там контейнер
# подключают к сети, маршрутизируемой с хоста (WEB_DEV_NETWORK), и называют его адрес
# или имя (WEB_DEV_HOST)
WEB_DEV_HOST    ?= 127.0.0.1
WEB_DEV_NETWORK ?=
WEB_DEV_LISTEN   = $(if $(WEB_DEV_NETWORK),--network $(WEB_DEV_NETWORK),-p 127.0.0.1:$(WEB_DEV_PORT):5173)
WEB_DEV_URL      = http://$(WEB_DEV_HOST):$(WEB_DEV_PORT)$(BOBA_URL_PREFIX)/workflow-dev/
WEB_DEV_WAIT    ?= 180

# --- что кладут sandbox и release ------------------------------------------------

SANDBOX_PARTS   := $(THIRD_DIR)/bin/bwrap $(THIRD_DIR)/bin/fuse2fs \
                   $(THIRD_DIR)/lib/libstdc++.so.6 $(THIRD_DIR)/lib/libgcc_s.so.1 \
                   $(SANDBOX_DIR)/workspace.ext4
# библиотеки времени исполнения колёс: нужны любому приложению
RUNTIME_LIBS    := $(THIRD_DIR)/lib/libstdc++.so.6 $(THIRD_DIR)/lib/libgcc_s.so.1
# образы корней всех плагинов репозитория
PLUGIN_IMAGES    = $(PLUGIN_PACKAGES:%=$(SANDBOX_DIR)/plugins/%/rootfs.ext4)
# приложения, которые исполняют инструменты в песочнице
SANDBOX_APPS    := $(foreach app,$(APPS),$(if $(SANDBOX_$(app)),$(app)))
# mke2fs из сборки: им make делает workspace.ext4 и rootfs плагинов вне docker build
SANDBOX_TOOLS   := $(SANDBOX_DIR)/tools/mke2fs $(SANDBOX_DIR)/tools/mke2fs.conf

RELEASE_SANDBOX := sandbox/plugins sandbox/workspace.ext4 third/bin/bwrap third/bin/fuse2fs
RELEASE_PARTS    = $(if $(APP_SANDBOX),$(RELEASE_SANDBOX)) \
                   third/lib/libstdc++.so.6 third/lib/libgcc_s.so.1 app/bin \
                   $(PARTS_$(APP)) $(APP_MODELS:%=models/%)

# каталоги krb dev-деревьев: точки монтирования в образе корня плагина
SANDBOX_DEV_KRB := $(foreach app,$(SANDBOX_APPS),$(SRC_DIR)/compose/$(app)/conf/krb)

# --- docker ---------------------------------------------------------------------

# число заданий одного компилирующего шага сборки (ninja, make, compileall, сборки и
# установки uv). BuildKit ведёт несколько стадий сразу, поэтому суммарно заданий
# бывает в два-три раза больше
JOBS            ?= 4

# от кого идёт docker run с томами хоста и кому отдаются его результаты. Rootless-демон
# отображает root контейнера на пользователя демона, а его же uid внутри контейнера —
# на чужой subuid, которому каталоги хоста закрыты: там контейнер идёт от root.
# Обычному демону называется пользователь хоста, иначе результаты достались бы root
RUN_OWNER       := $(shell id -u):$(shell id -g)
ifneq ($(findstring name=rootless,$(shell docker info --format '{{.SecurityOptions}}' 2>/dev/null)),)
RUN_OWNER       := 0:0
endif

NO_CACHE        ?= 0
NO_CACHE_OPT    :=
ifeq ($(NO_CACHE),1)
NO_CACHE_OPT    := --no-cache
endif

# --progress есть только у buildkit
PROGRESS        ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: CA внутреннего TLS для curl хоста; в образы его кладёт Dockerfile из conf/
CA_CHAIN        := $(wildcard $(CONF_DIR)/ca-chain.crt)
CURL_OPTS       := $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")

# прокси: в docker build аргументами, в docker run окружением
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))
PROXY_ENV       := $(foreach v,$(PROXY_VARS),-e $(v))

DOCKER_ARGS      = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)              \
    --build-arg GCC_IMAGE=$(GCC_IMAGE)                                 \
    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)                           \
    --build-arg PYTHON_VERSION=$(PYTHON_VERSION)                       \
    --build-arg BWRAP_VERSION=$(BWRAP_VERSION)                         \
    --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)                     \
    --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION)                 \
    --build-arg NODE_IMAGE=$(NODE_IMAGE)                               \
    --build-arg STUDIO_NODE_IMAGE=$(STUDIO_NODE_IMAGE)                 \
    --build-arg CHAINLIT_VERSION=$(CHAINLIT_VERSION)                   \
    --build-arg ORACLEDB_VERSION=$(ORACLEDB_VERSION)                   \
    --build-arg ORACLEDB_SHA256=$(ORACLEDB_SHA256)                     \
    --build-arg ORACLEDB_LOCAL=$(ORACLEDB_LOCAL)                       \
    --build-arg CYTHON_VERSION=$(CYTHON_VERSION)                       \
    --build-arg JOBS=$(JOBS)

# аргументы стадий одного приложения
APP_ARGS         = --build-arg APP=$(APP)                           \
    --build-arg APP_PACKAGES="$(APP_PACKAGES)"                      \
    --build-arg APP_IMPORTS="$(APP_IMPORTS)"                        \
    --build-arg HEALTH_PATH="$(APP_HEALTH)"                         \
    --build-arg BOBA_DIR="$(DEST_DIR)" --build-arg BOBA_VERSION="$(VERSION)"

# аргументы стадий образа корня плагина: образ один на все приложения
PLUGIN_ARGS      = --build-arg SANDBOX_DEV_KRB="$(SANDBOX_DEV_KRB)" \
    --build-arg BOBA_DIR="$(DEST_DIR)" --build-arg BOBA_VERSION="$(VERSION)"

# сборка стадии в образ: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --tag $(2) $(PROGRESS) $(BUILD_CONTEXT)
endef

# образ стадии без тега, работает и без buildkit: $(1) — стадия, $(2) — файл для ID
# образа, $(3) — аргументы. Лог сборки уходит в stderr: stdout рецепта бывает занят tar'ом
define docker-image
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --iidfile "$(2)" $(PROGRESS) $(BUILD_CONTEXT) >&2
endef

# mke2fs из runtime/sandbox/tools в контейнере glibc: $(1) — скрипт, $(2) — каталог результата,
# $(3) — аргументы скрипта. root в контейнере нужен, чтобы сохранить владельцев из tar
define mke2fs-run
docker run --rm -i -e MKE2FS_CONFIG=/tools/mke2fs.conf -e PATH=/tools:/usr/sbin:/usr/bin:/sbin:/bin \
    -v "$(SANDBOX_DIR)/tools:/tools:ro" -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(2):/out" \
    --entrypoint sh $(GLIBC_IMAGE) /scripts/$(1) $(3) "$(RUN_OWNER)"
endef

# загрузка: $(1) — url, $(2) — файл
define fetch
mkdir -p "$(dir $(2))"
curl -fSL $(CURL_OPTS) -o "$(2)" "$(1)"
endef

# скрипт загрузки моделей в образе base: $(1) — pip-пакет качалки, $(2) — каталог назначения,
# $(3) — аргументы fetch_models.py. Версии пакетов держит $(FETCH_CONSTRAINTS), файлы
# отдаются владельцу хоста
define fetch-model
mkdir -p "$(2)"
docker run --rm $(PROXY_ENV) -e HF_ENDPOINT="$(ONNX_HUB_URL)"       \
    -v "$(2)":/out -v "$(SCRIPTS_DIR)":/scripts:ro                  \
    -v "$(FETCH_CONSTRAINTS)":/constraints.txt:ro                   \
    --entrypoint sh $(BASE_IMAGE) -c '
        set -e
        pip3 install --no-cache-dir --constraint /constraints.txt "$(1)"
        python3 /scripts/fetch_models.py $(3)
        chown -R $(RUN_OWNER) /out'
endef

# дерево релиза из образа: $(1) — образ, $(2) — путь внутри, $(3) — куда
define image-copy
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# наличие путей: $(1) — список, $(2) — подсказка, что сделать
define check
for path in $(1); do
    if [ ! -e "$$path" ]; then
        echo "нет $$path — $(2)" >&2
        exit 1
    fi
done
endef

# установка в runtime/ без окна: результат готовится во временном каталоге внутри
# runtime/ (та же файловая система) и ставится на место переименованием. У работающих
# приложения и тестов всегда есть либо прежний файл, либо новый; открытый образ
# процесс дочитывает прежним
STAGE_TEMPLATE  := $(RUNTIME_DIR)/.stage.XXXXXX

# файлы дерева $(1) — на те же места в каталоге $(2), каждый переименованием
define install-tree
find "$(1)" ! -type d -printf '%P\n' | while IFS= read -r name; do
    mkdir -p "$(2)/$$(dirname "$$name")"
    mv -f "$(1)/$$name" "$(2)/$$name"
done
endef

# опустошить каталог, не пересоздавая: compose держит его bind-mount'ом по inode,
# после rm -rf контейнер до перезапуска видел бы пустоту
define refill
mkdir -p "$(1)"
find "$(1)" -mindepth 1 -delete
endef

# --- справка --------------------------------------------------------------------

define HELP
Сборка boba $(VERSION): приложения $(APPS) и образы корней плагинов

Сеть (артефакты сборки -> $(DEPS_DIR), модели -> $(MODELS_DIR)):
  fetch                 tarball'ы bwrap/fuse/e2fsprogs, uv, модели rapidocr, веса fastembed,
                        модели onnx-genai, katex/Inter/mermaid, исходники chainlit $(CHAINLIT_VERSION),
                        колесо python-oracledb $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL) с патчем
  oracledb-wheel        только колесо python-oracledb (стадия oracledb-wheel -> src/oracledb)

Общее для приложений (сеть — только реестры pypi/npm, результат файлами в src/):
  sandbox               bwrap, fuse2fs и библиотеки колёс -> runtime/third, шаблон workspace.ext4
                        -> runtime/sandbox;
                        песочница нужна приложениям, исполняющим инструменты: $(SANDBOX_APPS)
  plugin-rootfs         rootfs.ext4 одного плагина: PLUGIN=<пакет>
  plugin-rootfs-all     rootfs.ext4 всех плагинов с entry points boba.tools
  web                   фронт для dev-деревьев: web-chainlit и web-studio
  web-chainlit          page.js и UI chainlit (фронт + copilot); chainlit-ui — UI заново
  web-studio            страница workflow -> assets/workflow
  web-dev               vite dev-сервер страницы workflow на $(WEB_DEV_HOST):$(WEB_DEV_PORT); STOP=1 — остановить;
                        без проброса портов у демона: WEB_DEV_NETWORK=<сеть> WEB_DEV_HOST=<имя или адрес>

По приложению: APP=<$(APPS)> либо <цель>-<приложение>; без APP — все приложения
  build                 образ boba-<приложение>:$(BOBA_TAG); в образ mcp уезжают образы корней
                        плагинов и шаблон workspace из runtime/sandbox
  test                  проверка релиза внутри образа
  dev                   дерево compose в $(SRC_DIR)/compose/<приложение>: conf, data, app_root
  debug                 дерево отладки и тестов в $(SRC_DIR)/debug/<приложение>: свой site.toml
                        (пишет администратор) и свои data
  site-keys             перечень ключей site.toml приложения по ссылкам общего конфига
  release               дерево установки из образа -> $(RELEASE_DIR)/$(VERSION)/<экземпляр>, рассчитано на
                        $(INSTALL_ROOT)/boba-<экземпляр>; INSTANCE=<имя> (по умолчанию имя приложения),
                        INSTALL_ROOT=<каталог>
  install               установка дерева релиза и пользовательских юнитов systemd (запуск без Docker)

Прочее:
  all                   fetch sandbox plugin-rootfs-all web build dev
  clean                 удалить release, src/ и фронт, кроме моделей
  prune                 снести образы прошлых версий и промежуточные теги, build cache до PRUNE_KEEP=$(PRUNE_KEEP)
  sec                   sec.sh, MODE=all|sast|sca|rules
  version               показать версию пакетов
  version-set           проставить версию: VER=X.Y.Z
  version-check         версии и пины boba-* совпадают

Закрытый контур: conf/pip.conf, conf/uv.toml — индексы pypi; npm — npmrc образа nodejs;
conf/ca-chain.crt — CA; прокси из env уходит в docker. Зеркала артефактов: UV_URL,
BWRAP_URL, LIBFUSE_URL, E2FSPROGS_URL, RAPIDOCR_REPO, KATEX_URL, INTER_URL, MERMAID_URL,
ONNX_HUB_URL, CHAINLIT_SRC_URL.

Переменные: BOBA_TAG, NO_CACHE=1, JOBS=$(JOBS) (заданий на шаг сборки), PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE, GCC_IMAGE,
PYTHON_IMAGE, NODE_IMAGE, STUDIO_NODE_IMAGE, DEST_DIR; хосту нужен python >= 3.11.
endef

.PHONY: help
help:
	@:$(info $(HELP))

# --- версия ---------------------------------------------------------------------

.PHONY: version version-set version-check
version:
	@echo $(VERSION)

version-set:
	@$(VERSION_PY) set $(SRC_DIR)/packages "$(VER)"

version-check:
	@$(VERSION_PY) check $(SRC_DIR)/packages

# --- fetch: артефакты из сети -----------------------------------------------------

.PHONY: fetch
fetch: $(INPUTS)
	@rm -rf "$(ASSETS_VENDOR)"
	mkdir -p "$(dir $(ASSETS_VENDOR))"
	cp -a "$(WEBASSETS_DIR)" "$(ASSETS_VENDOR)"
	du -sh $(DEPS_DIR)/* $(MODELS_DIR)/*

$(BWRAP_TARBALL):
	@$(call fetch,$(BWRAP_URL),$@)

$(LIBFUSE_TARBALL):
	@$(call fetch,$(LIBFUSE_URL),$@)

$(E2FSPROGS_TARBALL):
	@$(call fetch,$(E2FSPROGS_URL),$@)

$(CHAINLIT_TARBALL):
	@$(call fetch,$(CHAINLIT_SRC_URL),$@)

# колесо патченного python-oracledb: стадия oracledb-wheel, наружу для .venv хоста
.PHONY: oracledb-wheel
oracledb-wheel: $(ORACLEDB_WHEEL)

$(ORACLEDB_WHEEL): $(SCRIPTS_DIR)/oracledb-$(ORACLEDB_VERSION)-arrow-duplicates.patch $(BASE_DIR)/Dockerfile | base
	@echo "== python-oracledb $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL) =="
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(call docker-image,oracledb-wheel,$$tmp/oracledb.iid)
	rm -rf "$(ORACLEDB_DIR)"
	$(call image-copy,$$(cat "$$tmp/oracledb.iid"),/out,$(ORACLEDB_DIR))
	$(call check,$@,колесо не собралось)
	ls -l "$(ORACLEDB_DIR)"

$(UV_BIN):
	@$(call fetch,$(UV_URL),/tmp/uv-$(UV_VERSION).tar.gz)
	tar -xzf /tmp/uv-$(UV_VERSION).tar.gz -C /tmp
	install -D -m 0755 /tmp/uv-$(UV_PLATFORM)/uv "$@"
	rm -rf /tmp/uv-$(UV_PLATFORM) /tmp/uv-$(UV_VERSION).tar.gz
	"$@" --version

# модели OCR: детектор и классификатор общие, распознаватель — на язык (boba.doc OcrModel)
$(MODELS_DIR)/rapidocr/ch_PP-OCRv5_det_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv5/det/$(notdir $@),$@)

$(MODELS_DIR)/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv4/cls/$(notdir $@),$@)

$(MODELS_DIR)/rapidocr/%_PP-OCRv5_rec_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv5/rec/$(notdir $@),$@)

# order-only: смена uv.lock не перекачивает гигабайты уже лежащих моделей
$(FETCH_CONSTRAINTS): $(SRC_DIR)/uv.lock | $(UV_BIN)
	@mkdir -p "$(@D)"
	"$(UV_BIN)" export --directory "$(SRC_DIR)" --frozen --all-packages --all-extras \
	    --no-emit-workspace --no-hashes -o "$@" > /dev/null

$(MODELS_DIR)/fastembed/.%: | base $(FETCH_CONSTRAINTS)
	@$(call fetch-model,fastembed,$(MODELS_DIR)/fastembed,fastembed "$(subst --,/,$*)" /out)
	touch "$@"

$(MODELS_DIR)/onnx-genai/%: | base $(FETCH_CONSTRAINTS)
	@$(call fetch-model,huggingface_hub,$(MODELS_DIR)/onnx-genai,onnx "$(ONNX_REPO_$*)" "$(ONNX_SUBDIR_$*)" /out/$*)

$(WEBASSETS_DIR)/katex/katex.min.css:
	@tmp=$$(mktemp -d)
	$(call fetch,$(KATEX_URL),$$tmp/katex.tar.gz)
	tar -xzf "$$tmp/katex.tar.gz" -C "$$tmp" --wildcards katex/katex.min.css 'katex/fonts/*.woff2'
	mkdir -p "$(@D)/fonts"
	cp "$$tmp/katex/katex.min.css" "$(@D)/"
	cp "$$tmp"/katex/fonts/*.woff2 "$(@D)/fonts/"
	rm -rf "$$tmp"

$(WEBASSETS_DIR)/inter/inter.css:
	@tmp=$$(mktemp -d)
	$(call fetch,$(INTER_URL),$$tmp/inter.tgz)
	tar -xzf "$$tmp/inter.tgz" -C "$$tmp"
	mkdir -p "$(@D)/files"
	: > "$@"
	for weight in $(INTER_WEIGHTS); do
	    cat "$$tmp/package/$$weight.css" >> "$@"
	    cp "$$tmp"/package/files/inter-*-$$weight-normal.woff2 "$(@D)/files/"
	done
	rm -rf "$$tmp"

$(WEBASSETS_DIR)/mermaid/mermaid.min.js:
	@tmp=$$(mktemp -d)
	$(call fetch,$(MERMAID_URL),$$tmp/mermaid.tgz)
	tar -xzf "$$tmp/mermaid.tgz" -C "$$tmp" package/dist/mermaid.min.js
	mkdir -p "$(@D)"
	cp "$$tmp/package/dist/mermaid.min.js" "$@"
	rm -rf "$$tmp"

# среда сборки и качалок моделей: gcc + python + индексы и CA закрытого контура
.PHONY: base
base: $(UV_BIN)
	@$(VERSION_PY) python-check $(SRC_DIR)/packages $(PYTHON_VERSION)
	$(call docker-stage,base,$(BASE_IMAGE),--tag boba-base:latest)

# --- песочница и плагины ---------------------------------------------------------

# песочница приложения: bwrap и fuse2fs, шаблон workspace
.PHONY: sandbox
sandbox: version-check
	@$(call check,$(INPUTS),запусти: make fetch)
	echo "== песочница =="
	mkdir -p "$(SANDBOX_DIR)"
	tmp= stage=
	trap 'rm -rf "$$tmp" "$$stage"' EXIT
	tmp=$$(mktemp -d)
	stage=$$(mktemp -d "$(STAGE_TEMPLATE)")
	$(call docker-image,sandbox-out,$$tmp/sandbox.iid)
	$(call image-copy,$$(cat "$$tmp/sandbox.iid"),/third,$$stage)
	$(call image-copy,$$(cat "$$tmp/sandbox.iid"),/tools,$$stage)
	chmod 0755 "$$stage/third/bin/bwrap" "$$stage/third/bin/fuse2fs" "$$stage/tools/mke2fs"
	# mke2fs-run берёт mke2fs из runtime/sandbox/tools: инструменты ставятся первыми
	$(call install-tree,$$stage/tools,$(SANDBOX_DIR)/tools)
	$(call mke2fs-run,mkworkspace.sh,$$stage,/out/workspace.ext4 $(SANDBOX_WS_SIZE)) < /dev/null
	$(call install-tree,$$stage/third,$(THIRD_DIR))
	mv -f "$$stage/workspace.ext4" "$(SANDBOX_DIR)/workspace.ext4"
	# TrustedBinaries отвергает каталог, открытый группе на запись
	chmod -R go-w "$(THIRD_DIR)"
	chmod go-w "$(RUNTIME_DIR)"
	ls -lh $(THIRD_DIR)/bin $(SANDBOX_DIR)/workspace.ext4

# образ корня одного плагина: цель читает декларации [tool.boba.sandbox] пакета
PLUGIN_ROOTFS    = $(SANDBOX_DIR)/plugins/$(PLUGIN)/rootfs.ext4

.PHONY: plugin-rootfs
plugin-rootfs: version-check
	@$(if $(PLUGIN),,$(error нужен PLUGIN=<имя пакета>: make plugin-rootfs PLUGIN=boba-tool-postgres))
	$(call check,$(INPUTS),запусти: make fetch)
	$(call check,$(SANDBOX_TOOLS),запусти: make sandbox)
	echo "== образ корня плагина $(PLUGIN) =="
	# корень и site-packages стримятся в mke2fs двумя tar'ами через docker cp в stdout:
	# ext4 не оседает ни в образах, ни на диске хоста. Пути site и кода раскладывает
	# стадия plugin-site-out, переписывать tar на хосте не нужно
	tmp= stage= root_cid= site_cid=
	trap 'docker rm -f $$root_cid $$site_cid > /dev/null 2>&1 || true; rm -rf "$$tmp" "$$stage"' EXIT
	tmp=$$(mktemp -d)
	stage=$$(mktemp -d "$(STAGE_TEMPLATE)")
	$(call docker-image,plugin-root,$$tmp/root.iid,$(PLUGIN_ARGS) --build-arg PLUGIN=$(PLUGIN))
	$(call docker-image,plugin-site-out,$$tmp/site.iid,$(PLUGIN_ARGS) --build-arg PLUGIN=$(PLUGIN))
	root_cid=$$(docker create "$$(cat "$$tmp/root.iid")" /bin/true)
	site_cid=$$(docker create "$$(cat "$$tmp/site.iid")" /bin/true)
	{
	docker cp "$$root_cid:/." -
	docker cp "$$site_cid:/usr" -
	} | $(call mke2fs-run,mkrootfs.sh,$$stage,/out/rootfs.ext4 $(PYTHON_VERSION) 16 $(JOBS))
	mkdir -p "$(dir $(PLUGIN_ROOTFS))"
	mv -f "$$stage/rootfs.ext4" "$(PLUGIN_ROOTFS)"

# образы всех плагинов с entry points группы boba.tools
.PHONY: plugin-rootfs-all
plugin-rootfs-all:
	@for name in $(PLUGIN_PACKAGES); do
	    $(MAKE) -f "$(MAKEFILE)" plugin-rootfs PLUGIN=$$name
	done

# --- фронт: цели web-* для dev-деревьев, в образы то же собирают стадии Dockerfile ---

.PHONY: web web-chainlit web-studio web-mcp
web: web-chainlit web-studio

web-chainlit: $(PAGE_JS) $(UI_DIST)/index.html

web-studio: $(STUDIO_DIST)/index.html

# у boba-mcp фронта нет: цель существует, чтобы dev APP=mcp шёл общим путём
web-mcp:

# скрипт страницы node'ом образа: на хосте node не нужен
$(PAGE_JS): $(PAGE_SRCS)
	@echo "== page script: $(NODE_IMAGE) =="
	docker run --rm --entrypoint /bin/sh -u "$(RUN_OWNER)" -e HOME=/tmp \
	    -v "$(WEB_DIR):/pkg" -w /pkg/web/page $(NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run check && npm run build'
	ls -la "$@"

# UI chainlit (фронт + copilot) с overlay в assets/ui: тот же скрипт, что у стадии chainlit-ui-build;
# пересобирается, когда overlay, UPSTREAM, тарболл или скрипт новее сборки
$(UI_DIST)/index.html: $(UI_SRCS) $(CHAINLIT_TARBALL)
	@echo "== UI chainlit $(CHAINLIT_VERSION) (фронт + copilot) + overlay: $(NODE_IMAGE) -> $(UI_DIST) =="
	mkdir -p "$(UI_DIST)"
	docker run --rm --entrypoint /bin/sh -u "$(RUN_OWNER)" \
	    -v "$(DEPS_DIR):/deps:ro" -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(UI_DIR):/ui:ro" \
	    -v "$(UI_DIST):/out" $(NODE_IMAGE) \
	    /scripts/chainlit_ui.sh "/deps/$(notdir $(CHAINLIT_TARBALL))" /ui/overlay /out
	touch "$@"
	ls -la "$(UI_DIST)/assets" | head -5

.PHONY: chainlit-ui
chainlit-ui:
	rm -f "$(UI_DIST)/index.html"
	$(MAKE) -f "$(MAKEFILE)" "$(UI_DIST)/index.html"

# контракт api studio печатает приложение из venv репозитория; страница генерирует по нему типы
$(OPENAPI_JSON): $(shell find $(STUDIO_DIR)/src -name "*.py")
	@$(PYTHON) -m boba.studio.api.schema > "$@"
	echo "== openapi: $@ =="

# страница workflow node'ом образа: на хосте node не нужен
$(STUDIO_DIST)/index.html: $(STUDIO_SRCS) $(OPENAPI_JSON)
	@echo "== workflow page: $(STUDIO_NODE_IMAGE) =="
	docker run --rm --entrypoint /bin/sh -u "$(RUN_OWNER)" -e HOME=/tmp \
	    -v "$(STUDIO_DIR):/pkg" -w /pkg/web/workflow $(STUDIO_NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run generate && npm run check && npm run build'
	touch "$@"
	du -sh "$(STUDIO_DIST)"

# vite с HMR в контейнере; studio с workflow_page = http://$(WEB_DEV_HOST):$(WEB_DEV_PORT)
# проксирует его под {BOBA_URL_PREFIX}/workflow-dev. Живой контейнер не трогается
.PHONY: web-dev
ifeq ($(STOP),1)
web-dev:
	docker rm -f $(WEB_DEV_NAME) > /dev/null 2>&1 || true
else
web-dev: $(OPENAPI_JSON)
	@running=$$(docker ps --filter name=^$(WEB_DEV_NAME)$$ --format '{{.Label "boba.url_prefix"}}')
	if [ "$$running" = "$(BOBA_URL_PREFIX)" ] && curl -sf -m 3 -o /dev/null "$(WEB_DEV_URL)"; then
	    echo "  ✓ $(WEB_DEV_NAME) is running: $(WEB_DEV_URL)"
	    exit 0
	fi
	docker rm -f $(WEB_DEV_NAME) > /dev/null 2>&1 || true
	docker run -d --name $(WEB_DEV_NAME) --entrypoint /bin/sh \
	    --label boba.url_prefix=$(BOBA_URL_PREFIX) \
	    -u "$(RUN_OWNER)" -e HOME=/tmp -e BOBA_URL_PREFIX=$(BOBA_URL_PREFIX) \
	    -e __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=$(WEB_DEV_HOST) \
	    $(WEB_DEV_LISTEN) \
	    -v "$(STUDIO_DIR):/pkg" -w /pkg/web/workflow $(STUDIO_NODE_IMAGE) \
	    -c '[ -d node_modules ] || npm ci --no-audit --no-fund; npm run generate; exec npm run dev -- --host 0.0.0.0 --port 5173 --strictPort --base $(BOBA_URL_PREFIX)/workflow-dev/'
	for i in $$(seq $(WEB_DEV_WAIT)); do
	    if curl -sf -m 3 -o /dev/null "$(WEB_DEV_URL)"; then
	        echo "== $(WEB_DEV_NAME): vite is ready at $(WEB_DEV_URL), logs: docker logs -f $(WEB_DEV_NAME) =="
	        exit 0
	    fi
	    if [ -z "$$(docker ps -q --filter name=^$(WEB_DEV_NAME)$$)" ]; then
	        echo "== $(WEB_DEV_NAME) exited; last log lines: =="
	        docker logs --tail 30 $(WEB_DEV_NAME)
	        exit 1
	    fi
	    sleep 1
	done
	echo "== $(WEB_DEV_NAME): vite did not answer at $(WEB_DEV_URL) in $(WEB_DEV_WAIT)s =="
	docker logs --tail 30 $(WEB_DEV_NAME)
	exit 1
endif

# --- по приложению: образ, проверка, деревья ---------------------------------------

# <цель>-<приложение> — то же, что <цель> APP=<приложение>
build-% test-% dev-% debug-% release-% install-%:
	@$(MAKE) -f "$(MAKEFILE)" $(firstword $(subst -, ,$@)) APP=$*

.PHONY: build test dev debug release

ifeq ($(APP),)

build: $(APPS:%=build-%)
test: $(APPS:%=test-%)
dev: $(APPS:%=dev-%)
debug: $(APPS:%=debug-%)
release: $(APPS:%=release-%)

else

# содержимое app_root dev-дерева: у каждого приложения своё
# app_root дерева compose: том нужен только studio — страница workflow меняется чаще
# образа. У chainlit и mcp статика лежит в образе
define DEV_ROOT_chainlit
endef

define DEV_ROOT_studio
$(call refill,$(OUT)/app_root)
mkdir -p "$(OUT)/app_root/public"
cp -a "$(STUDIO_DIST)" "$(OUT)/app_root/public/workflow"
cp -a "$(WEBASSETS_DIR)" "$(OUT)/app_root/public/vendor"
endef

define DEV_ROOT_mcp
endef

# каталог cgroup песочницы экземпляра $(1) у текущего пользователя: подкаталог sandbox
# юнита boba-sandbox@$(1).service
HOST_UID        := $(shell id -u)
sandbox-cgroup   = /sys/fs/cgroup/user.slice/user-$(HOST_UID).slice/user@$(HOST_UID).service/boba.slice/boba-$(1).slice/boba-sandbox@$(1).service/sandbox
USER_UNITS      := $(HOME)/.config/systemd/user

# шаблон юнита песочницы -> каталог юнитов пользователя и запуск экземпляра $(1).
# Работающий экземпляр не перезапускается: перезапуск пересоздаёт каталог sandbox, а у
# приложения остался бы старый. Обновлённый шаблон применится при следующем старте
define sandbox-unit
export XDG_RUNTIME_DIR="$${XDG_RUNTIME_DIR:-/run/user/$(HOST_UID)}"
export DBUS_SESSION_BUS_ADDRESS="$${DBUS_SESSION_BUS_ADDRESS:-unix:path=$$XDG_RUNTIME_DIR/bus}"
mkdir -p "$(USER_UNITS)"
if ! cmp -s "$(CONF_DIR)/boba-sandbox@.service" "$(USER_UNITS)/boba-sandbox@.service"; then
    install -m 0644 "$(CONF_DIR)/boba-sandbox@.service" "$(USER_UNITS)/boba-sandbox@.service"
    systemctl --user daemon-reload
    echo "    юнит песочницы: $(USER_UNITS)/boba-sandbox@.service обновлён из $(CONF_DIR)"
fi
systemctl --user enable --quiet "boba-sandbox@$(1).service"
if systemctl --user --quiet is-active "boba-sandbox@$(1).service"; then
    echo "    cgroup песочницы: boba-sandbox@$(1).service уже работает, не трогаю"
else
    systemctl --user start "boba-sandbox@$(1).service"
    echo "    cgroup песочницы: boba-sandbox@$(1).service запущен"
fi
endef

# .env дерева compose называет каждую переменную docker-compose.yml: значений по
# умолчанию в compose-файлах нет, и пропущенная переменная молча стала бы пустой строкой
COMPOSE_VARS     = BOBA_USER BOBA_NETWORK BOBA_NETWORK_EXTERNAL BOBA_ADDRESS \
                   $(if $(filter-out studio,$(APP)),BOBA_ADDRESS_2) $(if $(APP_SANDBOX),BOBA_SANDBOX_CGROUP)
COMPOSE_REQUIRED = BOBA_USER BOBA_NETWORK BOBA_NETWORK_EXTERNAL $(if $(APP_SANDBOX),BOBA_SANDBOX_CGROUP)
define compose-env-check
for name in $(COMPOSE_VARS); do
    if ! grep -q "^$$name=" "$(OUT)/.env"; then
        echo "$(OUT)/.env: нет строки $$name= — добавь её по образцу $(CONF_DIR)/compose.env.example" >&2
        exit 1
    fi
done
for name in $(COMPOSE_REQUIRED); do
    if grep -q "^$$name=$$" "$(OUT)/.env"; then
        echo "$(OUT)/.env: $$name пуста — значение обязательно, см. $(CONF_DIR)/compose.env.example" >&2
        exit 1
    fi
done
if [ -n "$(APP_SANDBOX)" ]; then
    cgroup=$$(sed -n 's|^BOBA_SANDBOX_CGROUP=||p' "$(OUT)/.env")
    if [ ! -d "$$cgroup" ]; then
        echo "$(OUT)/.env: каталога BOBA_SANDBOX_CGROUP нет: $$cgroup — запусти юнит: systemctl --user start boba-sandbox@$(APP).service" >&2
        exit 1
    fi
fi
network=$$(sed -n 's|^BOBA_NETWORK=||p' "$(OUT)/.env")
if grep -q "^BOBA_NETWORK_EXTERNAL=true$$" "$(OUT)/.env" && ! docker network inspect "$$network" > /dev/null 2>&1; then
    echo "$(OUT)/.env: сети $$network нет, а BOBA_NETWORK_EXTERNAL=true — создай её: docker network create $$network" >&2
    exit 1
fi
echo "    .env проверен: $(OUT)/.env"
endef

# site.toml случая запуска пишет администратор: в нём адреса и секреты машины. Цель
# его не порождает, а говорит, откуда взять перечень ключей. $(1) — каталог conf
define site-hint
if [ -e "$(1)/site.toml" ]; then
    echo "    site.toml на месте: $(1)/site.toml"
else
    echo "    нет $(1)/site.toml — напиши его по перечню ключей: make site-keys APP=$(APP)"
    echo "    (docs/admin-guide.md, раздел «Конфигурация»)"
fi
endef

# проверка машины: $(1) — compose, debug или systemd
define host-check
"$(SCRIPTS_DIR)/hostcheck.sh" $(1) $(if $(APP_SANDBOX),sandbox,plain)
endef

# конфиг окружения приложения из шаблона conf/boba.env: $(1) — корень установки,
# $(2) — каталог данных
define app-env
export BOBA_BASE="$(1)" BOBA_VERSION="$(VERSION)" BOBA_DATA="$(2)" \
    BOBA_PORT="$(APP_PORT)" BOBA_URL_PREFIX="$(APP_PREFIX)"
envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA} $${BOBA_PORT} $${BOBA_URL_PREFIX}' < "$(CONF_DIR)/boba.env"
endef

build: version-check
	@$(call check,$(INPUTS),запусти: make fetch)
	if [ -n "$(APP_EMBED)" ]; then
	    $(call check,$(SANDBOX_DIR)/workspace.ext4,запусти: make sandbox)
	    $(call check,$(PLUGIN_IMAGES),запусти: make plugin-rootfs-all)
	    # в образ уезжает каталог целиком: образ плагина, которого больше нет, там лишний
	    for dir in "$(SANDBOX_DIR)/plugins"/*/; do
	        name=$$(basename "$$dir")
	        case " $(PLUGIN_PACKAGES) " in
	            *" $$name "*) ;;
	            *) echo "$$dir: в репозитории нет плагина $$name — удали каталог" >&2; exit 1 ;;
	        esac
	    done
	fi
	echo "== образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR) =="
	$(call docker-stage,$(APP_STAGE),$(IMAGE),--tag boba-$(APP):latest $(APP_ARGS))

# бинды образов песочницы для проверки приложения, которое исполняет инструменты
TEST_SANDBOX_BINDS = $(if $(APP_EMBED),,$(TEST_SANDBOX_MOUNTS))
TEST_SANDBOX_MOUNTS = -v "$(SANDBOX_DIR)/plugins:$(DEST_DIR)/sandbox/plugins:ro" \
                     -v "$(SANDBOX_DIR)/workspace.ext4:$(DEST_DIR)/sandbox/workspace.ext4:ro"

# проверка релиза внутри образа тем же окружением, что уедет в compose и systemd
test: build
	@if [ -n "$(APP_SANDBOX)" ]; then
	    $(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	    $(call check,$(SANDBOX_DIR)/plugins,запусти: make plugin-rootfs-all)
	fi
	echo "== проверка релиза $(VERSION) в образе $(IMAGE) =="
	# окружение контейнера — шаблон conf/boba.env с путями образа, живёт только на время проверки
	env_file=$$(mktemp)
	trap 'rm -f "$$env_file"' EXIT
	$(call app-env,$(DEST_DIR),$(DEST_DIR)/data) > "$$env_file"
	# модели в образе не лежат, песочница — только у приложения с плагинами внутри;
	# в релизе они рядом: подставляем их биндами
	docker run --rm -i --env-file "$$env_file" --entrypoint sh                              \
	    $(foreach model,$(APP_MODELS),-v "$(MODELS_DIR)/$(model):$(DEST_DIR)/models/$(model):ro") \
	    $(if $(APP_SANDBOX),$(TEST_SANDBOX_BINDS))                                            \
	    $(IMAGE)                                                                              \
	    -c '"$$PYTHONHOME/bin/python3" - --app $(APP) $(if $(APP_SANDBOX),--sandbox) --names "$(DEST_DIR)/app/packages.txt"' < "$(TEST_DIR)/bobacheck.py"

# дерево установки из готового образа: пути и шебанги переводятся с DEST_DIR на INSTALL_DIR
release:
	@docker image inspect "$(RELEASE_IMAGE)" > /dev/null
	$(call check,$(INPUTS),запусти: make fetch)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	    $(call check,$(SANDBOX_DIR)/plugins,запусти: make plugin-rootfs-all)
	fi
	rm -rf "$(OUT)"
	mkdir -p "$(OUT)/conf" "$(OUT)/models"
	$(call image-copy,$(RELEASE_IMAGE),$(DEST_DIR)/.,$(OUT)/)
	# у приложения с образами плагинов внутри sandbox/ уже приехал из образа
	if [ -n "$(APP_SANDBOX)" ] && [ -z "$(APP_EMBED)" ]; then
	    mkdir -p "$(OUT)/sandbox"
	    cp -a "$(SANDBOX_DIR)/plugins" "$(OUT)/sandbox/plugins"
	    cp -a "$(SANDBOX_DIR)/workspace.ext4" "$(OUT)/sandbox/workspace.ext4"
	fi
	for model in $(APP_MODELS); do
	    cp -a "$(MODELS_DIR)/$$model" "$(OUT)/models/$$model"
	done
	# системные библиотеки образа (libssl1.1, libffi6, krb5 ...) в third/lib: нужны там,
	# где система не та, на которой собран образ, и этих версий в ней нет
	if [ -n "$(BUNDLE_OS_LIBS)" ]; then
	    docker run --rm --network none -v "$(SCRIPTS_DIR):/scripts:ro" --entrypoint sh "$(RELEASE_IMAGE)" \
	        /scripts/oslibs.sh "$(DEST_DIR)" | tar -C "$(OUT)/third/lib" -xf -
	    echo "    системные библиотеки образа добавлены в $(OUT)/third/lib"
	fi
	sed -i "1s|^#!.*python3.*|#!$(INSTALL_DIR)/third/bin/python3|" "$(OUT)/app/bin"/*
	$(call app-env,$(INSTALL_DIR),$(DATA_DIR)) > "$(OUT)/conf/boba.env"
	chmod go-w "$(OUT)"
	chmod -R go-w "$(OUT)/third"
	if [ -n "$(APP_SANDBOX)" ]; then
	    install -m 0644 "$(CONF_DIR)/boba-sandbox@.service" "$(OUT)/boba-sandbox@.service"
	    chmod -R go-w "$(OUT)/sandbox"
	fi
	sed -e 's|@APP@|$(APP)|g' -e 's|@INSTANCE@|$(INSTANCE)|g' -e 's|@INSTALL_DIR@|$(INSTALL_DIR)|g' \
	    -e 's|@MODULE@|$(APP_MODULE)|g'                                                              \
	    -e 's|@SANDBOX_UNIT@|$(if $(APP_SANDBOX),boba-sandbox@$(INSTANCE).service)|'                 \
	    -e 's|@REQUIRES@|$(if $(APP_SANDBOX),Requires=boba-sandbox@$(INSTANCE).service)|'            \
	    -e 's|@MEMORY_MAX@|$(word 1,$(UNIT_LIMITS_$(APP)))|' -e 's|@CPU_QUOTA@|$(word 2,$(UNIT_LIMITS_$(APP)))|' \
	    -e 's|@TASKS_MAX@|$(word 3,$(UNIT_LIMITS_$(APP)))|'                                          \
	    "$(CONF_DIR)/boba-app.service" > "$(OUT)/boba-$(INSTANCE).service"
	$(call check,$(addprefix $(OUT)/,$(RELEASE_PARTS)),собери заново: make build release APP=$(APP))
	echo ">>> релиз готов: $(OUT) ($$(du -sh --apparent-size "$(OUT)" | cut -f1))"
	echo "    установка без Docker: make install APP=$(APP)$(if $(filter-out $(APP),$(INSTANCE)), INSTANCE=$(INSTANCE))$(if $(filter-out /app,$(INSTALL_ROOT)), INSTALL_ROOT=$(INSTALL_ROOT))"

# установка дерева релиза в INSTALL_DIR и пользовательских юнитов systemd: приложение
# работает без Docker от того пользователя, который запустил цель. Код, интерпретатор,
# статика, модели, образы песочницы и общий конфиг с плагинами заменяются целиком;
# conf/site.toml, conf/boba.env, conf/krb и data остаются. Работающий экземпляр цель
# не трогает — его сначала останавливают
.PHONY: install
install:
	@$(call check,$(OUT)/app $(OUT)/third $(OUT)/boba-$(INSTANCE).service,запусти: make release APP=$(APP))
	export XDG_RUNTIME_DIR="$${XDG_RUNTIME_DIR:-/run/user/$(HOST_UID)}"
	export DBUS_SESSION_BUS_ADDRESS="$${DBUS_SESSION_BUS_ADDRESS:-unix:path=$$XDG_RUNTIME_DIR/bus}"
	echo "== установка $(APP) ($(INSTANCE)) в $(INSTALL_DIR) =="
	$(call host-check,systemd)
	if [ ! -d "$(INSTALL_DIR)" ] || [ ! -w "$(INSTALL_DIR)" ]; then
	    echo "нет каталога $(INSTALL_DIR) либо он закрыт на запись — попросить root: install -d -o $$(id -un) -g $$(id -gn) -m 0755 $(INSTALL_DIR)" >&2
	    exit 1
	fi
	if systemctl --user --quiet is-active "boba-$(INSTANCE).service"; then
	    echo "boba-$(INSTANCE).service работает — останови его: systemctl --user stop boba-$(INSTANCE).service" >&2
	    exit 1
	fi
	for part in app third app_root models sandbox; do
	    if [ ! -e "$(OUT)/$$part" ]; then
	        continue
	    fi
	    rm -rf "$(INSTALL_DIR)/$$part"
	    cp -a "$(OUT)/$$part" "$(INSTALL_DIR)/$$part"
	done
	mkdir -p "$(INSTALL_DIR)/conf/krb" "$(INSTALL_DIR)/data/dump" "$(INSTALL_DIR)/data/krb"
	if [ -n "$(APP_SANDBOX)" ]; then
	    mkdir -p "$(INSTALL_DIR)/data/workspace" "$(INSTALL_DIR)/data/tool-logs"
	fi
	if [ -e "$(INSTALL_DIR)/conf/boba.env" ]; then
	    echo "    окружение уже есть, не трогаю: $(INSTALL_DIR)/conf/boba.env"
	else
	    install -m 0644 "$(OUT)/conf/boba.env" "$(INSTALL_DIR)/conf/boba.env"
	fi
	rm -rf "$(INSTALL_DIR)/conf/plugins"
	cp -a "$(OUT)/conf/config.toml" "$(INSTALL_DIR)/conf/config.toml"
	if [ -d "$(OUT)/conf/plugins" ]; then
	    cp -a "$(OUT)/conf/plugins" "$(INSTALL_DIR)/conf/plugins"
	fi
	$(call site-hint,$(INSTALL_DIR)/conf)
	mkdir -p "$(USER_UNITS)"
	install -m 0644 "$(OUT)/boba-$(INSTANCE).service" "$(USER_UNITS)/boba-$(INSTANCE).service"
	systemctl --user daemon-reload
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call sandbox-unit,$(INSTANCE))
	fi
	systemctl --user enable --quiet "boba-$(INSTANCE).service"
	echo ">>> установлено: $(INSTALL_DIR)"
	echo "    запуск:  systemctl --user start boba-$(INSTANCE).service"
	echo "    журнал:  journalctl --user -u boba-$(INSTANCE).service -f"

# рабочее дерево приложения для docker compose: conf, data, .env и у studio app_root;
# docker-compose*.yml лежат в нём же и трекаются git'ом. Модели и образы песочницы
# compose монтирует из runtime/, общий конфиг лежит в образе. В conf дерева — только
# специфика машины: site.toml (пишет администратор), keytab и krb5.conf. Готовые
# .env и site.toml цель не трогает
dev: OUT = $(SRC_DIR)/compose/$(APP)
dev: web-$(APP)
	@$(call check,$(INPUTS),запусти: make fetch)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	fi
	echo "== дерево compose $(APP) в $(OUT) =="
	$(call host-check,compose)
	mkdir -p "$(OUT)/data/dump" "$(OUT)/data/krb"
	if [ -n "$(APP_SANDBOX)" ]; then
	    mkdir -p "$(OUT)/data/workspace" "$(OUT)/data/tool-logs"
	    $(call sandbox-unit,$(APP))
	fi
	$(DEV_ROOT_$(APP))
	( umask 027; mkdir -p "$(OUT)/conf/krb" )
	$(call app-env,$(DEST_DIR),$(DEST_DIR)/data) > "$(OUT)/conf/boba.env"
	$(call site-hint,$(OUT)/conf)
	if [ -e "$(OUT)/.env" ]; then
	    echo "    .env уже есть, не трогаю: $(OUT)/.env"
	else
	    {
	        echo "BOBA_USER=1000:1000"
	        if [ -n "$(APP_SANDBOX)" ]; then
	            echo "BOBA_SANDBOX_CGROUP=$(call sandbox-cgroup,$(APP))"
	        fi
	        echo "BOBA_NETWORK=boba"
	        echo "BOBA_NETWORK_EXTERNAL=true"
	        echo "BOBA_ADDRESS="
	        if [ "$(APP)" != studio ]; then
	            echo "BOBA_ADDRESS_2="
	        fi
	    } > "$(OUT)/.env"
	    echo "    .env создан: $(OUT)/.env (сеть и адреса — по образцу $(CONF_DIR)/compose.env.example)"
	fi
	chmod go-w "$(OUT)"
	$(compose-env-check)
	"$(SCRIPTS_DIR)/composeown.sh" "$(OUT)" "$$(sed -n 's|^BOBA_USER=||p' "$(OUT)/.env")"
	echo ">>> дерево готово: $(OUT)"
	echo "    compose: cd $(OUT) && docker compose up -d $(APP)"
	echo "    debug:   make debug APP=$(APP)"

# дерево отладки и тестов на хосте: свои данные и свой site.toml, чтобы процесс на хосте
# и контейнер compose не писали в одни каталоги. Общий конфиг отладка читает прямо из
# пакета приложения, модели, third и образы песочницы — из runtime/, статику — из
# ассетов пакета. site.toml отладки пишет администратор; цель его не порождает
debug: OUT = $(SRC_DIR)/debug/$(APP)
debug: web-$(APP) vscode
	@$(call check,$(RUNTIME_LIBS),запусти: make sandbox)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	fi
	echo "== дерево отладки $(APP) в $(OUT) =="
	$(call host-check,debug)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call sandbox-unit,debug)
	fi
	( umask 077; mkdir -p "$(OUT)/conf" )
	mkdir -p "$(OUT)/data/dump" "$(OUT)/data/krb"
	if [ -n "$(APP_SANDBOX)" ]; then
	    mkdir -p "$(OUT)/data/workspace" "$(OUT)/data/tool-logs"
	fi
	$(call site-hint,$(OUT)/conf)
	echo ">>> дерево отладки готово: $(OUT)"
	echo "    debug:   python -m $(APP_MODULE) --config $(APP_CONF)/config.toml --site $(OUT)/conf/site.toml"
	echo "    cgroup песочницы отладки для site.toml: $(call sandbox-cgroup,debug)"

# перечень ключей site.toml приложения: всё, на что общий конфиг и плагины ссылаются
# как ${env.*} и ${site.*}
.PHONY: site-keys
site-keys:
	@$(PYTHON) "$(SCRIPTS_DIR)/site_keys.py" "$(APP_CONF)"

endif

# --- VS Code ---------------------------------------------------------------------

# цели запуска и задачи VS Code из образцов conf/vscode: .vscode/ в git не входит (кроме
# settings.json), а в launch.json стоит путь cgroup с номером пользователя. Готовые
# файлы цель не перезаписывает
VSCODE_DIR      := $(SRC_DIR)/.vscode
VSCODE_FILES    := launch.json tasks.json python-debug-slice.sh

.PHONY: vscode
vscode:
	@mkdir -p "$(VSCODE_DIR)"
	for name in $(VSCODE_FILES); do
	    if [ -e "$(VSCODE_DIR)/$$name" ]; then
	        continue
	    fi
	    sed 's|@DEBUG_CGROUP@|$(call sandbox-cgroup,debug)|g' "$(CONF_DIR)/vscode/$$name" > "$(VSCODE_DIR)/$$name"
	    echo "    vscode: $(VSCODE_DIR)/$$name создан из образца"
	done
	chmod 0755 "$(VSCODE_DIR)/python-debug-slice.sh"

# --- прочее ---------------------------------------------------------------------

# всё целиком: артефакты, песочница, образы корней плагинов, фронт, образы и
# compose-деревья всех приложений. Compose studio монтирует app_root поверх образа,
# поэтому фронт нужен и в дереве, а не только в образе
.PHONY: all
all: fetch sandbox plugin-rootfs-all web
	@$(MAKE) -f "$(MAKEFILE)" build APP=
	$(MAKE) -f "$(MAKEFILE)" dev APP=

.PHONY: sec
sec:
	@"$(SRC_DIR)/sec.sh" $(MODE)

# образы прошлых сборок и промежуточные теги старых схем, затем build cache до PRUNE_KEEP
.PHONY: prune
prune:
	@docker images --format '{{.Repository}}:{{.Tag}}' \
	    | { grep -E '^boba-(plugin|bwrap|fuse2fs|sandbox)' || true; } | xargs -r docker rmi
	docker images --format '{{.Repository}}:{{.Tag}}' \
	    | { grep -E '^boba-($(subst $(space),|,$(APPS))):' || true; } \
	    | { grep -v -E ':($(BOBA_TAG)|latest)$$' || true; } \
	    | { xargs -r -n1 docker rmi || true; }   # образ под живым контейнером остаётся
	# образы стадий без тега (docker-image): кэш классического builder'а
	docker image prune -f
	docker builder prune -f --keep-storage $(PRUNE_KEEP)
	docker system df

# модели не трогаем: качаются долго
.PHONY: clean
clean:
	rm -rf $(RELEASE_DIR) $(DEPS_DIR)/uv $(MODELS_DIR)/rapidocr $(WEBASSETS_DIR) $(SANDBOX_DIR) $(ORACLEDB_DIR) \
	       $(FETCH_CONSTRAINTS) \
	       $(DEPS_DIR)/*.tar.xz $(DEPS_DIR)/*.tar.gz $(PAGE_JS) $(ASSETS_VENDOR) $(UI_DIST) $(STUDIO_DIST)
