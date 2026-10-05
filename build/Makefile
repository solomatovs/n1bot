# Единая сборка boba: приложения chainlit, studio и mcp и образы корней плагинов. Из сети берутся две вещи: артефакты цели fetch (github,
# huggingface, kernel.org — в src/) и пакеты pypi/npm при сборке — из реестров образов
# (в закрытом контуре nexus: conf/pip.conf, conf/uv.toml, npmrc образа nodejs).
# Остальные цели работают из src/ и локальных образов.

# от каталога Makefile, а не от cwd: make -f build/Makefile из корня искал бы
# test/ и conf/ рядом с собой
BASE_DIR        ?= $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SRC_DIR         ?= $(abspath $(BASE_DIR)/..)
# вложенные вызовы: make -f build/Makefile не подставляет его в $(MAKE)
MAKEFILE        := $(BASE_DIR)/Makefile
BUILD_CONTEXT   ?= $(SRC_DIR)
TEST_DIR        := $(BASE_DIR)/test
SCRIPTS_DIR     := $(BASE_DIR)/scripts
CONF_DIR        := $(BASE_DIR)/conf
# артефакты для сборки: tarball'ы, uv, модели OCR и эмбеддера, webassets, sandbox
DEPS_DIR        ?= $(BASE_DIR)/src
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
CGROUP_studio    := boba-sandbox-studio
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
CGROUP_mcp       := boba-sandbox-mcp
MODELS_mcp       := fastembed rapidocr
PARTS_mcp        :=

APP_PACKAGES      = $(PACKAGES_$(APP))
APP_IMPORTS       = $(IMPORTS_$(APP))
APP_MODULE        = $(MODULE_$(APP))
APP_STAGE         = $(STAGE_$(APP))
APP_HEALTH        = $(HEALTH_$(APP))
APP_PORT          = $(PORT_$(APP))
APP_PREFIX        = $(PREFIX_$(APP))
APP_SANDBOX       = $(SANDBOX_$(APP))
APP_EMBED         = $(EMBED_$(APP))
APP_CGROUP        = $(CGROUP_$(APP))
APP_MODELS        = $(MODELS_$(APP))
# юнит systemd: приложению с песочницей нужны устройства, namespaces и делегирование cgroup
APP_UNIT          = $(CONF_DIR)/$(if $(APP_SANDBOX),boba-sandbox.service,boba.service)

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
INSTALL_DIR      = /app/boba-$(APP)
DATA_DIR         = $(INSTALL_DIR)/data
DEST_DIR        ?= /app/boba

IMAGE            = boba-$(APP):$(BOBA_TAG)
RELEASE_IMAGE    = $(IMAGE)
OUT              = $(RELEASE_DIR)/$(VERSION)/$(APP)

# режим sec.sh: all, sast, sca или rules
MODE            ?= all

PYTHON_VERSION  ?= 3.11
GLIBC_IMAGE     ?= dmp/glibc:2.28
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
NODE_IMAGE      ?= dmp/nodejs:20
STUDIO_NODE_IMAGE ?= dmp/nodejs:22
BASE_IMAGE      := boba-base:$(PYTHON_VERSION)
SANDBOX_DIR     := $(DEPS_DIR)/sandbox
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
RAPIDOCR_FILES  := $(DEPS_DIR)/rapidocr/ch_PP-OCRv5_det_mobile.onnx \
                   $(DEPS_DIR)/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx \
                   $(RAPIDOCR_LANGS:%=$(DEPS_DIR)/rapidocr/%_PP-OCRv5_rec_mobile.onnx)

# веса эмбеддера: версия качалки фиксируется, иначе приедет произвольный набор моделей
FASTEMBED_VERSION ?= 0.8.0
EMBED_MODELS    ?= intfloat/multilingual-e5-large
FASTEMBED_STAMPS := $(foreach m,$(EMBED_MODELS),$(DEPS_DIR)/fastembed/.$(subst /,--,$(m)))

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
ONNX_DIRS       := $(ONNX_MODELS:%=$(DEPS_DIR)/onnx-genai/%)

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
WEB_DEV_URL      = http://127.0.0.1:$(WEB_DEV_PORT)$(BOBA_URL_PREFIX)/workflow-dev/
WEB_DEV_WAIT    ?= 180

# --- что кладут sandbox и release ------------------------------------------------

SANDBOX_PARTS   := $(SANDBOX_DIR)/third/bin/bwrap $(SANDBOX_DIR)/third/bin/fuse2fs \
                   $(SANDBOX_DIR)/third/lib/libstdc++.so.6 $(SANDBOX_DIR)/third/lib/libgcc_s.so.1 \
                   $(SANDBOX_DIR)/workspace.ext4
# библиотеки времени исполнения колёс: нужны любому приложению
RUNTIME_LIBS    := $(SANDBOX_DIR)/third/lib/libstdc++.so.6 $(SANDBOX_DIR)/third/lib/libgcc_s.so.1
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
    --build-arg CYTHON_VERSION=$(CYTHON_VERSION)

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

# mke2fs из src/sandbox/tools в контейнере glibc: $(1) — скрипт, $(2) — каталог результата,
# $(3) — аргументы скрипта. root в контейнере нужен, чтобы сохранить владельцев из tar
define mke2fs-run
docker run --rm -i -e MKE2FS_CONFIG=/tools/mke2fs.conf -e PATH=/tools:/usr/sbin:/usr/bin:/sbin:/bin \
    -v "$(SANDBOX_DIR)/tools:/tools:ro" -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(2):/out" \
    --entrypoint sh $(GLIBC_IMAGE) /scripts/$(1) $(3) "$$(id -u):$$(id -g)"
endef

# загрузка: $(1) — url, $(2) — файл
define fetch
mkdir -p "$(dir $(2))"
curl -fSL $(CURL_OPTS) -o "$(2)" "$(1)"
endef

# скрипт загрузки моделей в образе base: $(1) — pip-пакет качалки, $(2) — каталог назначения,
# $(3) — аргументы fetch_models.py. Файлы отдаются владельцу хоста
define fetch-model
mkdir -p "$(2)"
docker run --rm $(PROXY_ENV) -e HF_ENDPOINT="$(ONNX_HUB_URL)"       \
    -v "$(2)":/out -v "$(SCRIPTS_DIR)":/scripts:ro                  \
    --entrypoint sh $(BASE_IMAGE) -c '
        set -e
        pip3 install --no-cache-dir "$(1)"
        python3 /scripts/fetch_models.py $(3)
        chown -R $(shell id -u):$(shell id -g) /out'
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

# опустошить каталог, не пересоздавая: compose держит его bind-mount'ом по inode,
# после rm -rf контейнер до перезапуска видел бы пустоту
define refill
mkdir -p "$(1)"
find "$(1)" -mindepth 1 -delete
endef

# --- справка --------------------------------------------------------------------

define HELP
Сборка boba $(VERSION): приложения $(APPS) и образы корней плагинов

Сеть (артефакты -> $(DEPS_DIR)):
  fetch                 tarball'ы bwrap/fuse/e2fsprogs, uv, модели rapidocr, веса fastembed,
                        модели onnx-genai, katex/Inter/mermaid, исходники chainlit $(CHAINLIT_VERSION),
                        колесо python-oracledb $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL) с патчем
  oracledb-wheel        только колесо python-oracledb (стадия oracledb-wheel -> src/oracledb)

Общее для приложений (сеть — только реестры pypi/npm, результат файлами в src/):
  sandbox               bwrap, fuse2fs, шаблон workspace.ext4 и библиотеки колёс -> src/sandbox;
                        песочница нужна приложениям, исполняющим инструменты: $(SANDBOX_APPS)
  plugin-rootfs         rootfs.ext4 одного плагина: PLUGIN=<пакет>
  plugin-rootfs-all     rootfs.ext4 всех плагинов с entry points boba.tools
  web                   фронт для dev-деревьев: web-chainlit и web-studio
  web-chainlit          page.js и UI chainlit (фронт + copilot); chainlit-ui — UI заново
  web-studio            страница workflow -> assets/workflow
  web-dev               vite dev-сервер страницы workflow на 127.0.0.1:$(WEB_DEV_PORT); STOP=1 — остановить

По приложению: APP=<$(APPS)> либо <цель>-<приложение>; без APP — все приложения
  build                 образ boba-<приложение>:$(BOBA_TAG); в образ mcp уезжают образы корней
                        плагинов и шаблон workspace из src/sandbox
  test                  проверка релиза внутри образа
  dev                   рантайм в $(SRC_DIR)/compose/<приложение> из src/ для отладки и compose
  release               дерево установки из образа -> $(RELEASE_DIR)/$(VERSION)/<приложение>;
                        ставится в /app/boba-<приложение>

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

Переменные: BOBA_TAG, NO_CACHE=1, PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE, GCC_IMAGE,
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
	du -sh $(DEPS_DIR)/*

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
$(DEPS_DIR)/rapidocr/ch_PP-OCRv5_det_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv5/det/$(notdir $@),$@)

$(DEPS_DIR)/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv4/cls/$(notdir $@),$@)

$(DEPS_DIR)/rapidocr/%_PP-OCRv5_rec_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv5/rec/$(notdir $@),$@)

$(DEPS_DIR)/fastembed/.%: | base
	@$(call fetch-model,fastembed==$(FASTEMBED_VERSION),$(DEPS_DIR)/fastembed,fastembed "$(subst --,/,$*)" /out)
	touch "$@"

$(DEPS_DIR)/onnx-genai/%: | base
	@$(call fetch-model,huggingface_hub,$(DEPS_DIR)/onnx-genai,onnx "$(ONNX_REPO_$*)" "$(ONNX_SUBDIR_$*)" /out/$*)

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
	rm -f $(SANDBOX_PARTS) $(SANDBOX_TOOLS)
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(call docker-image,sandbox-out,$$tmp/sandbox.iid)
	$(call image-copy,$$(cat "$$tmp/sandbox.iid"),/third,$(SANDBOX_DIR))
	$(call image-copy,$$(cat "$$tmp/sandbox.iid"),/tools,$(SANDBOX_DIR))
	chmod 0755 "$(SANDBOX_DIR)/third/bin/bwrap" "$(SANDBOX_DIR)/third/bin/fuse2fs" "$(SANDBOX_DIR)/tools/mke2fs"
	$(call mke2fs-run,mkworkspace.sh,$(SANDBOX_DIR),/out/workspace.ext4 $(SANDBOX_WS_SIZE)) < /dev/null
	# TrustedBinaries отвергает каталог, открытый группе на запись
	chmod -R go-w "$(SANDBOX_DIR)/third"
	ls -lh $(SANDBOX_DIR)/third/bin $(SANDBOX_DIR)/workspace.ext4

# образ корня одного плагина: цель читает декларации [tool.boba.sandbox] пакета
PLUGIN_ROOTFS    = $(SANDBOX_DIR)/plugins/$(PLUGIN)/rootfs.ext4

.PHONY: plugin-rootfs
plugin-rootfs: version-check
	@$(if $(PLUGIN),,$(error нужен PLUGIN=<имя пакета>: make plugin-rootfs PLUGIN=boba-tool-postgres))
	$(call check,$(INPUTS),запусти: make fetch)
	$(call check,$(SANDBOX_TOOLS),запусти: make sandbox)
	echo "== образ корня плагина $(PLUGIN) =="
	mkdir -p "$(dir $(PLUGIN_ROOTFS))"
	# корень и site-packages стримятся в mke2fs двумя tar'ами через docker cp в stdout:
	# ext4 не оседает ни в образах, ни на диске хоста. Пути site и кода раскладывает
	# стадия plugin-site-out, переписывать tar на хосте не нужно
	tmp=$$(mktemp -d)
	root_cid= site_cid=
	trap 'docker rm -f $$root_cid $$site_cid > /dev/null 2>&1 || true; rm -rf "$$tmp"' EXIT
	$(call docker-image,plugin-root,$$tmp/root.iid,$(PLUGIN_ARGS) --build-arg PLUGIN=$(PLUGIN))
	$(call docker-image,plugin-site-out,$$tmp/site.iid,$(PLUGIN_ARGS) --build-arg PLUGIN=$(PLUGIN))
	root_cid=$$(docker create "$$(cat "$$tmp/root.iid")" /bin/true)
	site_cid=$$(docker create "$$(cat "$$tmp/site.iid")" /bin/true)
	{
	docker cp "$$root_cid:/." -
	docker cp "$$site_cid:/usr" -
	} | $(call mke2fs-run,mkrootfs.sh,$(dir $(PLUGIN_ROOTFS)),/out/rootfs.ext4 $(PYTHON_VERSION) 16)

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
	docker run --rm --entrypoint /bin/sh -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
	    -v "$(WEB_DIR):/pkg" -w /pkg/web/page $(NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run check && npm run build'
	ls -la "$@"

# UI chainlit (фронт + copilot) с overlay в assets/ui: тот же скрипт, что у стадии chainlit-ui-build;
# пересобирается, когда overlay, UPSTREAM, тарболл или скрипт новее сборки
$(UI_DIST)/index.html: $(UI_SRCS) $(CHAINLIT_TARBALL)
	@echo "== UI chainlit $(CHAINLIT_VERSION) (фронт + copilot) + overlay: $(NODE_IMAGE) -> $(UI_DIST) =="
	mkdir -p "$(UI_DIST)"
	docker run --rm --entrypoint /bin/sh -u "$$(id -u):$$(id -g)" \
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
	docker run --rm --entrypoint /bin/sh -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
	    -v "$(STUDIO_DIR):/pkg" -w /pkg/web/workflow $(STUDIO_NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run generate && npm run check && npm run build'
	touch "$@"
	du -sh "$(STUDIO_DIST)"

# vite с HMR в контейнере; studio с BOBA_WORKFLOW_PAGE=http://127.0.0.1:$(WEB_DEV_PORT)
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
	    -u "$$(id -u):$$(id -g)" -e HOME=/tmp -e BOBA_URL_PREFIX=$(BOBA_URL_PREFIX) \
	    -p 127.0.0.1:$(WEB_DEV_PORT):5173 \
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
build-% test-% dev-% release-%:
	@$(MAKE) -f "$(MAKEFILE)" $(firstword $(subst -, ,$@)) APP=$*

.PHONY: build test dev release

ifeq ($(APP),)

build: $(APPS:%=build-%)
test: $(APPS:%=test-%)
dev: $(APPS:%=dev-%)
release: $(APPS:%=release-%)

else

# содержимое app_root dev-дерева: у каждого приложения своё
define DEV_ROOT_chainlit
mkdir -p "$(OUT)/app_root/public" "$(OUT)/app_root/.chainlit" "$(OUT)/app_root/.files"
cp -a "$(ASSETS_DIR)/.chainlit/config.toml" "$(ASSETS_DIR)/.chainlit/translations" "$(OUT)/app_root/.chainlit/"
cp -a "$(ASSETS_DIR)/chainlit.md" "$(OUT)/app_root/"
cp -a "$(ASSETS_DIR)/public/." "$(OUT)/app_root/public/"
cp -a "$(UI_DIST)" "$(OUT)/app_root/ui"
endef

define DEV_ROOT_studio
mkdir -p "$(OUT)/app_root/public"
cp -a "$(STUDIO_DIST)" "$(OUT)/app_root/public/workflow"
cp -a "$(WEBASSETS_DIR)" "$(OUT)/app_root/public/vendor"
endef

define DEV_ROOT_mcp
endef

# конфиг окружения и юнит приложения из шаблонов conf/: $(1) — корень установки,
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
	    $(foreach model,$(APP_MODELS),-v "$(DEPS_DIR)/$(model):$(DEST_DIR)/models/$(model):ro") \
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
	    cp -al "$(DEPS_DIR)/$$model" "$(OUT)/models/$$model"
	done
	sed -i "1s|^#!.*python3.*|#!$(INSTALL_DIR)/third/bin/python3|" "$(OUT)/app/bin"/*
	export INSTALL_DIR="$(INSTALL_DIR)" BOBA_APP="$(APP)" BOBA_MODULE="$(APP_MODULE)"
	$(call app-env,$(INSTALL_DIR),$(DATA_DIR)) > "$(OUT)/conf/boba.env"
	envsubst '$${INSTALL_DIR} $${BOBA_DATA} $${BOBA_APP} $${BOBA_MODULE}' < "$(APP_UNIT)" > "$(OUT)/boba-$(APP).service"
	chmod go-w "$(OUT)"
	chmod -R go-w "$(OUT)/third"
	if [ -n "$(APP_SANDBOX)" ]; then
	    install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"
	    chmod -R go-w "$(OUT)/sandbox"
	fi
	$(call check,$(addprefix $(OUT)/,$(RELEASE_PARTS)),собери заново: make build release APP=$(APP))
	echo ">>> релиз готов: $(OUT) ($$(du -sh --apparent-size "$(OUT)" | cut -f1))"
	echo "    conf/config.toml в дереве нет — положи его при установке"
	if [ -n "$(APP_SANDBOX)" ]; then
	    echo "    cgroup:    BOBA_CGROUP=$(APP_CGROUP) $(INSTALL_DIR)/cgroup-init.sh"
	fi
	echo "    установка: cp -a $(OUT)/. $(INSTALL_DIR)/"

# рабочее дерево приложения для отладки и docker compose: раскладка релиза, но из
# src/, без образа; docker-compose*.yml лежат в нём же и трекаются git'ом
dev: OUT = $(SRC_DIR)/compose/$(APP)
dev: web-$(APP)
	@$(call check,$(INPUTS),запусти: make fetch)
	$(call check,$(RUNTIME_LIBS),запусти: make sandbox)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	fi
	echo "== рантайм $(APP) в $(OUT) =="
	mkdir -p "$(OUT)/conf" "$(OUT)/data/dump" "$(OUT)/data/krb"
	$(call refill,$(OUT)/models)
	for model in $(APP_MODELS); do
	    cp -al "$(DEPS_DIR)/$$model" "$(OUT)/models/$$model"
	done
	rm -rf "$(OUT)/sandbox"
	$(call refill,$(OUT)/third)
	cp -al "$(SANDBOX_DIR)/third/lib" "$(OUT)/third/lib"
	# образы песочницы лежат в образе приложения либо монтируются compose из
	# src/sandbox; bwrap и fuse2fs нужны и process-режиму отладки: хранилище файлов
	# монтирует ими workspace.ext4
	if [ -n "$(APP_SANDBOX)" ]; then
	    mkdir -p "$(OUT)/data/workspace" "$(OUT)/data/tool-logs"
	    install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"
	    cp -al "$(SANDBOX_DIR)/third/bin" "$(OUT)/third/bin"
	fi
	chmod -R go-w "$(OUT)/third"
	$(call refill,$(OUT)/app_root)
	$(DEV_ROOT_$(APP))
	$(call app-env,$(DEST_DIR),$(DEST_DIR)/data) > "$(OUT)/conf/boba.env"
	chmod go-w "$(OUT)"
	$(call check,$(OUT)/conf/config.toml,положи конфиг приложения $(APP))
	echo ">>> дерево готово: $(OUT)"
	if [ -n "$(APP_SANDBOX)" ]; then
	    echo "    cgroup:  BOBA_CGROUP=$(APP_CGROUP) BOBA_USER=<пользователь> $(OUT)/cgroup-init.sh"
	fi
	echo "    debug:   python -m $(APP_MODULE) --config $(OUT)/conf/config.toml"
	echo "    compose: cd $(OUT) && docker compose up -d"

endif

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
	rm -rf $(RELEASE_DIR) $(DEPS_DIR)/uv $(DEPS_DIR)/rapidocr $(WEBASSETS_DIR) $(SANDBOX_DIR) $(ORACLEDB_DIR) \
	       $(DEPS_DIR)/*.tar.xz $(DEPS_DIR)/*.tar.gz $(PAGE_JS) $(ASSETS_VENDOR) $(UI_DIST) $(STUDIO_DIST)
