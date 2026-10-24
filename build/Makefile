# Сборка boba: образы boba-aid и boba-mcp, образы корней плагинов, агент boba каталогом,
# деревья запуска. Образы собирает классический сборщик docker (DOCKER_BUILDKIT=0).
# Сеть: в интернет ходит только fetch. Остальные цели — только в реестры через прокси:
# apt-зеркало из sources.list образов dmp/*, pypi из conf/pip.conf и conf/uv.toml, npm из
# npmrc образа nodejs. Проверка этого — make offline-check.

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
# значения переменных сборки для этого хоста (в git не попадает)
-include $(CONF_DIR)/local.mk
# артефакты для сборки: tarball'ы, uv, webassets, колесо oracledb
DEPS_DIR        ?= $(BASE_DIR)/src
# runtime-зависимости приложений: модели, бинарники и библиотеки песочницы, её образы.
# Экземпляр один на репозиторий: его монтирует compose, читает отладка, а образы корней
# плагинов и workspace из него берёт сборка образа mcp
RUNTIME_DIR     := $(SRC_DIR)/runtime
MODELS_DIR      := $(RUNTIME_DIR)/models
THIRD_DIR       := $(RUNTIME_DIR)/third
SANDBOX_DIR     := $(RUNTIME_DIR)/sandbox
RELEASE_DIR     := $(SRC_DIR)/release

# приложения сборки; цели build, test, dev, debug, release, install и site-keys работают
# по одному приложению (APP=<имя> либо <цель>-<имя>), без APP — по всем
APPS            := aid mcp agent
APP             ?=

# чат инструментов не исполняет: песочницы, cgroup и моделей у него нет
PACKAGES_aid      = boba-aid boba-llm-http
IMPORTS_aid      := boba.aid
MODULE_aid       := boba.aid.main
STAGE_aid        := runtime-aid
HEALTH_aid       := /
PORT_aid         := 8501
PREFIX_aid       := /boba
SANDBOX_aid      :=
MODELS_aid       :=
PARTS_aid        := app_root/public app_root/public/page.js app_root/public/vendor \
                    app_root/ui/index.html app_root/ui/index.js app_root/mcp-apps/sandbox.html \
                    app_root/mcp-apps/fonts/inter/files
# агента в образе чата нет: на выкатке чат ходит в службу агента (образ agent, boba serve)
BUILD_ARGS_aid    =
# фронт на хосте нужен только отладке чата: образ собирает его сам
WEB_aid          := web

# boba-mcp — MCP-сервер с инструментами boba; фронта у него нет, образы корней плагинов
# и шаблон workspace лежат внутри образа
PACKAGES_mcp      = boba-mcp $(PLUGIN_PACKAGES) boba-llm-fastembed
IMPORTS_mcp      := boba.mcp_server.app fastmcp
MODULE_mcp       := boba.mcp_server
STAGE_mcp        := runtime-mcp
HEALTH_mcp       := /health
PORT_mcp         := 8650
PREFIX_mcp       :=
SANDBOX_mcp      := yes
MODELS_mcp       := fastembed rapidocr
PARTS_mcp        :=
BUILD_ARGS_mcp    =
WEB_mcp          :=

# агент как служба — `boba serve` пакета boba-cli: агент сам слушает ACP по сети,
# сессии — потоки одного процесса; образ запускает бинарник boba-bin, установка без
# Docker — модуль интерпретатором; песочницы, моделей и фронта нет
PACKAGES_agent    = boba-cli
IMPORTS_agent    := boba.cli.serve
MODULE_agent     := boba.cli.app serve
CONF_agent       := boba-cli
STAGE_agent      := runtime-agent
HEALTH_agent     := /health
PORT_agent       := 8660
PREFIX_agent     :=
SANDBOX_agent    :=
MODELS_agent     :=
PARTS_agent      := bin/boba.dist/boba.bin
BUILD_ARGS_agent  = $(BIN_TOOL_ARGS) --build-arg BIN_ONNX=
WEB_agent        :=

# лимиты пользовательского юнита установки без Docker: те же, что у контейнера compose
UNIT_LIMITS_aid      := 4G 400% 512
UNIT_LIMITS_mcp      := 16G 800% 2048
UNIT_LIMITS_agent    := 8G 400% 1024

APP_PACKAGES      = $(PACKAGES_$(APP))
APP_IMPORTS       = $(IMPORTS_$(APP))
APP_MODULE        = $(MODULE_$(APP))
APP_STAGE         = $(STAGE_$(APP))
APP_HEALTH        = $(HEALTH_$(APP))
APP_PORT          = $(PORT_$(APP))
APP_PREFIX        = $(PREFIX_$(APP))
APP_SANDBOX       = $(SANDBOX_$(APP))
APP_MODELS        = $(MODELS_$(APP))
APP_BUILD_ARGS    = $(BUILD_ARGS_$(APP))
# общий конфиг приложения и его плагины: в пакете приложения, в git; CONF_<app> —
# пакет, если он назван не по приложению
APP_CONF          = $(SRC_DIR)/packages/apps/$(or $(CONF_$(APP)),boba-$(APP))/conf

ifneq ($(APP),)
ifeq ($(filter $(APP),$(APPS)),)
$(error APP=$(APP): такого приложения нет, есть $(APPS))
endif
endif

.DEFAULT_GOAL := help

# цели делят runtime/, build/src и демон docker: по одной за раз, в том числе при make -j
.NOTPARALLEL:

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель.
# Поэтому make -n исполняет рецепты с $(MAKE) целиком — их читают глазами
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
$(error INSTANCE=$(INSTANCE): имя без «-», например $(APP)2)
endif
# экземпляр принадлежит одному приложению: release и install без APP шли бы по обоим
# приложениям с одним именем
ifeq ($(APP),)
ifneq ($(filter release install,$(MAKECMDGOALS)),)
ifeq ($(origin INSTANCE),command line)
$(error INSTANCE=$(INSTANCE) задаётся вместе с APP, например make release APP=mcp INSTANCE=mcp2)
endif
endif
endif
DATA_DIR         = $(INSTALL_DIR)/data
# корень релиза в образе: его же называют CMD образов и тома compose
DEST_DIR        := /app/boba

IMAGE            = boba-$(APP):$(BOBA_TAG)
# основа образа приложения с песочницей: runtime-sandbox и образы корней плагинов
SANDBOX_IMAGE    = boba-$(APP)-sandbox:$(BOBA_TAG)
OUT              = $(RELEASE_DIR)/$(VERSION)/$(INSTANCE)

# режим sec.sh: all, full, sast, sca или rules
MODE            ?= all

PYTHON_VERSION  ?= 3.11
GLIBC_IMAGE     ?= dmp/glibc:2.28
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
NODE_IMAGE      ?= dmp/nodejs:20
BASE_IMAGE      := boba-base:$(PYTHON_VERSION)
# сколько кэша BuildKit оставляет prune
PRUNE_KEEP      ?= 20GB
empty           :=
space           := $(empty) $(empty)

# --- агент boba каталогом standalone -----------------------------------------------

# 1 — агент boba-bin с рантаймом onnxruntime-genai (отдельная компиляция Nuitka, каталог
# bin/boba-onnx.dist), а fetch качает модели onnx-genai. Пусто — без него, как в образе aid:
# профиль модели onnx агент отвергает на старте
BIN_ONNX           ?=
BIN_DIST            = $(RELEASE_DIR)/$(VERSION)/bin/boba$(if $(BIN_ONNX),-onnx).dist
BIN_FILE            = $(BIN_DIST)/boba.bin
NUITKA_VERSION     ?= 4.3
PATCHELF_VERSION   ?= 0.19.1.0
ZSTANDARD_VERSION  ?= 0.25.0
ORDERED_SET_VERSION ?= 4.1.0
BIN_TOOL_ARGS       = --build-arg NUITKA_VERSION=$(NUITKA_VERSION)           \
    --build-arg PATCHELF_VERSION=$(PATCHELF_VERSION)                     \
    --build-arg ZSTANDARD_VERSION=$(ZSTANDARD_VERSION)                   \
    --build-arg ORDERED_SET_VERSION=$(ORDERED_SET_VERSION)
BIN_ARGS            = $(BIN_TOOL_ARGS) --build-arg BIN_ONNX=$(BIN_ONNX)

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
# патч fuse2fs к версии — build/scripts/e2fsprogs-<версия>-ro-mount.patch
E2FSPROGS_VERSION ?= 1.47.4
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
# зеркало huggingface для всех моделей fetch: веса fastembed и onnx-genai
HF_URL          ?= https://huggingface.co
EMBED_MODELS    ?= intfloat/multilingual-e5-large
FASTEMBED_STAMPS := $(foreach m,$(EMBED_MODELS),$(MODELS_DIR)/fastembed/.$(subst /,--,$(m)))

# локальные модели агента (BIN_ONNX=1): onnxruntime-genai держит каждую каталогом
# genai_config.json + веса + токенайзер; какую берёт профиль, решают настройки агента
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
AID_DIR         := $(SRC_DIR)/packages/apps/boba-aid
UI_DIR          := $(AID_DIR)/web/chainlit-ui
CHAINLIT_VERSION := $(shell cat $(UI_DIR)/UPSTREAM)
CHAINLIT_SRC_URL ?= https://codeload.github.com/Chainlit/chainlit/tar.gz/refs/tags/$(CHAINLIT_VERSION)
CHAINLIT_TARBALL := $(DEPS_DIR)/chainlit-$(CHAINLIT_VERSION).tar.gz

# входы по потребителю. Песочница — tarball'ы и uv; классический сборщик проходит её
# стадии в каждой сборке ниже по Dockerfile, поэтому они нужны всем сборкам образов
SANDBOX_INPUTS  := $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL) $(UV_BIN)
WEB_INPUTS      := $(WEB_FILES) $(CHAINLIT_TARBALL)
IMAGE_INPUTS    := $(SANDBOX_INPUTS) $(WEB_INPUTS)
# модели приложения по именам MODELS_<приложение>: лежат рядом с релизом, не в образе
MODEL_FILES_fastembed := $(FASTEMBED_STAMPS)
MODEL_FILES_rapidocr  := $(RAPIDOCR_FILES)
APP_MODEL_FILES  = $(foreach model,$(APP_MODELS),$(MODEL_FILES_$(model)))
# модели, которые секции песочницы монтируют образом данных, а не каталогом: тогда в
# mountinfo тела нет пути хоста. Образ лежит рядом с каталогом, процессный режим читает
# сам каталог
MODEL_IMAGES    := fastembed rapidocr
APP_MODEL_IMAGES = $(if $(APP_SANDBOX),$(filter $(MODEL_IMAGES),$(APP_MODELS)))
# всё, что качает fetch: входы образов, модели, колесо oracledb для .venv хоста
INPUTS          := $(IMAGE_INPUTS) $(RAPIDOCR_FILES) $(FASTEMBED_STAMPS) $(ORACLEDB_WHEEL) \
                   $(if $(BIN_ONNX),$(ONNX_DIRS))

# --- фронт -----------------------------------------------------------------------

ASSETS_DIR      := $(AID_DIR)/assets
ASSETS_VENDOR   := $(ASSETS_DIR)/public/vendor
# скрипт страницы: в образ его собирает стадия aid-page-build, для отладки — цель web
WEB_PROJECT     := $(AID_DIR)/web/page
PAGE_JS         := $(ASSETS_DIR)/public/page.js
PAGE_SRCS       := $(shell find $(WEB_PROJECT)/src -type f) \
                   $(wildcard $(WEB_PROJECT)/*.json $(WEB_PROJECT)/*.ts $(WEB_PROJECT)/*.js)
# UI chainlit (фронт + copilot) с overlay: в образ его собирает стадия chainlit-ui-build
UI_DIST         := $(ASSETS_DIR)/ui
UI_ELEMENTS     := $(ASSETS_DIR)/public/elements
UI_SRCS         := $(shell find $(UI_DIR) $(UI_ELEMENTS) -type f) $(SCRIPTS_DIR)/chainlit_ui.sh

# --- что кладут sandbox и release ------------------------------------------------

# библиотеки времени исполнения колёс: нужны любому приложению
RUNTIME_LIBS    := $(THIRD_DIR)/lib/libstdc++.so.6 $(THIRD_DIR)/lib/libgcc_s.so.1
SANDBOX_PARTS   := $(THIRD_DIR)/bin/bwrap $(THIRD_DIR)/bin/fuse2fs $(RUNTIME_LIBS) \
                   $(SANDBOX_DIR)/workspace.ext4
# образы корней всех плагинов репозитория
PLUGIN_IMAGES    = $(PLUGIN_PACKAGES:%=$(SANDBOX_DIR)/plugins/%/rootfs.ext4)
# приложения, которые исполняют инструменты в песочнице
SANDBOX_APPS    := $(strip $(foreach app,$(APPS),$(if $(SANDBOX_$(app)),$(app))))
# mke2fs из сборки: им make делает workspace.ext4 и rootfs плагинов вне docker build
SANDBOX_TOOLS   := $(SANDBOX_DIR)/tools/mke2fs $(SANDBOX_DIR)/tools/mke2fs.conf

RELEASE_SANDBOX := sandbox/plugins sandbox/workspace.ext4 third/bin/bwrap third/bin/fuse2fs
RELEASE_PARTS    = $(if $(APP_SANDBOX),$(RELEASE_SANDBOX)) \
                   third/bin/python3 third/lib/libstdc++.so.6 third/lib/libgcc_s.so.1 \
                   app/bin conf/config.toml $(PARTS_$(APP)) $(APP_MODELS:%=models/%) \
                   $(APP_MODEL_IMAGES:%=models/%.ext4)

# каталоги krb dev-деревьев: точки монтирования в образе корня плагина
SANDBOX_DEV_KRB := $(foreach app,$(SANDBOX_APPS),$(SRC_DIR)/compose/$(app)/conf/krb)

# --- docker ---------------------------------------------------------------------

# число заданий одного компилирующего шага сборки (ninja, make, Nuitka, сборки и
# установки uv). Значение — ENV стадии base: оно входит в ключ кэша всех сборочных
# стадий от неё, и другое число пересобирает их вместе с apt-шагами. На машине держат
# одно значение — в conf/local.mk
JOBS            ?= 4

# сеть шагов docker build и запусков docker run, которым нужен реестр (npm фронта):
# пусто — сеть docker по умолчанию; offline-check ставит container:<ловушка>
BUILD_NETWORK   ?=
NETWORK_OPT      = $(if $(BUILD_NETWORK),--network $(BUILD_NETWORK))
# своя область кэша сборщика: стадии от образов-основ в build/Dockerfile и
# testapps.Dockerfile объявляют ARG CACHE_SCOPE, и с новым значением каждый шаг сборки
# исполняется заново ровно один раз. Пусто — обычный кэш
CACHE_SCOPE     ?=
CACHE_SCOPE_ARG  = $(if $(CACHE_SCOPE),--build-arg CACHE_SCOPE=$(CACHE_SCOPE))

# классический сборщик, как в закрытом контуре, где BuildKit нет; --force-rm убирает
# контейнер упавшего шага
DOCKER_BUILD     = DOCKER_BUILDKIT=0 docker build --force-rm $(NETWORK_OPT)

# от кого идёт docker run с томами хоста и кому отдаются его результаты. Rootless-демон
# отображает root контейнера на пользователя демона, а его же uid внутри контейнера —
# на чужой subuid, которому каталоги хоста закрыты: там контейнер идёт от root.
# Обычному демону называется пользователь хоста, иначе результаты достались бы root
HOST_UID        := $(shell id -u)
RUN_OWNER       := $(HOST_UID):$(shell id -g)
ifneq ($(findstring name=rootless,$(shell docker info --format '{{.SecurityOptions}}' 2>/dev/null)),)
RUN_OWNER       := 0:0
endif

NO_CACHE        ?= 0
NO_CACHE_OPT    :=
ifeq ($(NO_CACHE),1)
NO_CACHE_OPT    := --no-cache
endif

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
    --build-arg CHAINLIT_VERSION=$(CHAINLIT_VERSION)                   \
    --build-arg ORACLEDB_VERSION=$(ORACLEDB_VERSION)                   \
    --build-arg ORACLEDB_SHA256=$(ORACLEDB_SHA256)                     \
    --build-arg ORACLEDB_LOCAL=$(ORACLEDB_LOCAL)                       \
    --build-arg CYTHON_VERSION=$(CYTHON_VERSION)                       \
    --build-arg JOBS=$(JOBS) $(CACHE_SCOPE_ARG)

# аргументы стадий одного приложения
APP_ARGS         = --build-arg APP_PACKAGES="$(APP_PACKAGES)"       \
    --build-arg APP_IMPORTS="$(APP_IMPORTS)"                        \
    --build-arg HEALTH_PATH="$(APP_HEALTH)"                         \
    --build-arg BOBA_DIR="$(DEST_DIR)" $(APP_BUILD_ARGS)

# аргументы стадий образа корня плагина: образ один на все приложения
PLUGIN_ARGS      = --build-arg SANDBOX_DEV_KRB="$(SANDBOX_DEV_KRB)" --build-arg BOBA_DIR="$(DEST_DIR)"

# сборка стадии в образ: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
$(DOCKER_BUILD) -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --tag $(2) $(BUILD_CONTEXT)
endef

# образ стадии без тега: $(1) — стадия, $(2) — файл для ID образа, $(3) — аргументы.
# Лог сборки уходит в stderr: stdout рецепта бывает занят tar'ом
define docker-image
$(DOCKER_BUILD) -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --iidfile "$(2)" $(BUILD_CONTEXT) >&2
endef

# основа образа приложения с песочницей: стадия runtime-sandbox (ID образа в файле $(1)) и
# поверх неё образы корней плагинов и workspace — сборка build/sandbox.Dockerfile в
# контексте $(SANDBOX_DIR). Отпечаток основы и файлов (размер, время) лежит меткой образа:
# с тем же отпечатком образ берётся готовым и гигабайты контекста в демон не уходят
define sandbox-image
base=$$(cat "$(1)")
stamp=$$({ echo "$$base"; cat "$(BASE_DIR)/sandbox.Dockerfile"; find "$(SANDBOX_DIR)/plugins" "$(SANDBOX_DIR)/workspace.ext4" -type f -printf '%p %s %T@\n' | sort; } | sha256sum | cut -c1-16)
built=$$(docker image inspect --format '{{index .Config.Labels "boba.sandbox"}}' "$(SANDBOX_IMAGE)" 2>/dev/null || true)
if [ "$$built" = "$$stamp" ] && [ "$(NO_CACHE)" != 1 ]; then
    echo "    $(SANDBOX_IMAGE): образы плагинов и основа не менялись, образ готов"
else
    $(DOCKER_BUILD) -f "$(BASE_DIR)/sandbox.Dockerfile" $(NO_CACHE_OPT) --build-arg RUNTIME_SANDBOX="$$base" \
        --build-arg BOBA_DIR="$(DEST_DIR)" --label boba.sandbox="$$stamp" --tag "$(SANDBOX_IMAGE)" "$(SANDBOX_DIR)"
fi
endef

# mke2fs из runtime/sandbox/tools в контейнере glibc: $(1) — скрипт, $(2) — каталог результата,
# $(3) — аргументы скрипта, $(4) — дополнительные тома docker (источник дерева образа).
# root в контейнере нужен, чтобы сохранить владельцев из tar
define mke2fs-run
docker run --rm -i --network none -e MKE2FS_CONFIG=/tools/mke2fs.conf -e PATH=/tools:/usr/sbin:/usr/bin:/sbin:/bin \
    -v "$(SANDBOX_DIR)/tools:/tools:ro" -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(2):/out" $(4) \
    --entrypoint sh $(GLIBC_IMAGE) /scripts/$(1) $(3) "$(RUN_OWNER)"
endef

# образ данных песочницы $(1) из уже скачанного каталога весов $(MODELS_DIR)/$(1): ext4
# рядом с каталогом; в сеть не ходит, источник монтируется в контейнер read-only.
# Пересобирается, когда образа нет или в каталоге модели либо в скрипте есть файл новее
define model-image
image="$(MODELS_DIR)/$(1).ext4"
if [ -e "$$image" ] && [ -z "$$(find "$(MODELS_DIR)/$(1)" "$(SCRIPTS_DIR)/mkmodels.sh" -newer "$$image" -print -quit)" ]; then
    echo "    $$image: модель не менялась, образ готов"
else
    echo "== образ данных $(1): $(MODELS_DIR)/$(1) -> $$image =="
    stage=$$(mktemp -d "$(STAGE_TEMPLATE)")
    $(call mke2fs-run,mkmodels.sh,$$stage,/out/$(1).ext4 /src $(1) 16,-v "$(MODELS_DIR)/$(1):/src:ro") < /dev/null
    chmod 0644 "$$stage/$(1).ext4"
    mv -f "$$stage/$(1).ext4" "$$image"
    rm -rf "$$stage"
fi
endef

# в интернет ходит только fetch (и all, который начинается с него): цель, которой не
# хватило входа, падает с подсказкой, а не качает его сама мимо реестров
FETCH_GOALS     := $(filter fetch all,$(MAKECMDGOALS))
define fetch-only
if [ -z "$(FETCH_GOALS)" ]; then
    echo "нет $@ — его кладёт make fetch, другие цели в интернет не ходят" >&2
    exit 1
fi
endef

# загрузка: $(1) — url, $(2) — файл
define fetch
$(fetch-only)
mkdir -p "$(dir $(2))"
curl -fSL $(CURL_OPTS) -o "$(2)" "$(1)"
endef

# скрипт загрузки моделей в образе base: $(1) — pip-пакет качалки, $(2) — каталог назначения,
# $(3) — аргументы fetch_models.py. Версии пакетов держит $(FETCH_CONSTRAINTS), файлы
# отдаются владельцу хоста
define fetch-model
$(fetch-only)
mkdir -p "$(2)"
docker run --rm $(PROXY_ENV) -e HF_ENDPOINT="$(HF_URL)"             \
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

# каталоги данных $(1): дампы и билеты, у приложения с песочницей — ещё workspace
# пользователей и журналы вызовов
define data-dirs
mkdir -p "$(1)/dump" "$(1)/krb"
if [ -n "$(APP_SANDBOX)" ]; then
    mkdir -p "$(1)/workspace" "$(1)/tool-logs"
fi
endef

# каталог cgroup песочницы экземпляра $(1) у текущего пользователя: подкаталог sandbox
# юнита boba-sandbox@$(1).service
sandbox-cgroup   = /sys/fs/cgroup/user.slice/user-$(HOST_UID).slice/user@$(HOST_UID).service/boba.slice/boba-$(1).slice/boba-sandbox@$(1).service/sandbox
USER_UNITS      := $(HOME)/.config/systemd/user

# шина пользовательского systemd: systemctl --user из make без сеанса входа её не находит
define user-bus
export XDG_RUNTIME_DIR="$${XDG_RUNTIME_DIR:-/run/user/$(HOST_UID)}"
export DBUS_SESSION_BUS_ADDRESS="$${DBUS_SESSION_BUS_ADDRESS:-unix:path=$$XDG_RUNTIME_DIR/bus}"
endef

# шаблон юнита песочницы -> каталог юнитов пользователя и запуск экземпляра $(1).
# Работающий экземпляр не перезапускается: перезапуск пересоздаёт каталог sandbox, а у
# приложения остался бы старый. Обновлённый шаблон применится при следующем старте
define sandbox-unit
$(user-bus)
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
                   BOBA_ADDRESS_2 $(if $(APP_SANDBOX),BOBA_SANDBOX_CGROUP)
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
export BOBA_BASE="$(1)" BOBA_DATA="$(2)" BOBA_PORT="$(APP_PORT)" BOBA_URL_PREFIX="$(APP_PREFIX)"
envsubst '$${BOBA_BASE} $${BOBA_DATA} $${BOBA_PORT} $${BOBA_URL_PREFIX}' < "$(CONF_DIR)/boba.env"
endef

# --- справка --------------------------------------------------------------------

define HELP
Сборка boba $(VERSION): образы boba-aid и boba-mcp, образы корней плагинов, агент boba

С нуля по порядку (каждый шаг кладёт то, что нужно следующему):
  fetch                 артефакты и модели из сети -> build/src, runtime/models
  sandbox               bwrap, fuse2fs, mke2fs, шаблон workspace.ext4 -> runtime/third, runtime/sandbox
  models-image          образы данных из скачанных моделей -> runtime/models/<модель>.ext4
  plugin-rootfs-all     образы корней плагинов -> runtime/sandbox/plugins (уезжают в образ mcp)
  test                  собрать оба образа и проверить их: test = build + проверка в образе
  all                   всё это по порядку, затем фронт для отладки (web) и деревья compose (dev)

Входы:
  fetch                 tarball'ы bwrap/libfuse/e2fsprogs, uv, исходники chainlit $(CHAINLIT_VERSION),
                        katex/Inter/mermaid, модели rapidocr и fastembed, колесо python-oracledb
                        $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL); с BIN_ONNX=1 — ещё модели onnx-genai
  base                  образ сборки $(BASE_IMAGE): gcc, python, uv, CA и индексы контура
  oracledb-wheel        только колесо python-oracledb -> build/src/oracledb (для .venv, его зовёт dev.sh)

Песочница и плагины:
  sandbox               песочница приложений, исполняющих инструменты: $(SANDBOX_APPS)
  plugin-rootfs         образ корня одного плагина: PLUGIN=<пакет>
  plugin-rootfs-all     образы корней всех плагинов с entry points boba.tools, всегда заново
  models-image          образы данных песочницы ($(MODEL_IMAGES)): ext4 рядом с каталогом модели,
                        секции монтируют их read-only вместо каталога; пересобираются по файлам моделей

Закрытый контур:
  offline-check         сборка без интернета: цели OFFLINE_TARGETS (по умолчанию все, кроме
                        fetch и dev), у шагов сборки сеть none, наружу — только прокси реестров
                        pypi, apt и npm из конфигурации сборки; обращение мимо них роняет
                        проверку с целью, стадией и хостом. Итог — release/offline-check/report.txt

Тестовые образы (не поставка):
  testapps              boba-testapps:$(BOBA_TAG) и :latest — серверы MCP для тестов клиента:
                        примеры MCP Apps, свой сервер на TypeScript SDK 1.x и серверы fastmcp[apps]
                        с рендерером Prefab статикой; TESTAPPS_TAGS=<образ> — свой тег, тесты берут
                        его --testapps-image <образ>; TESTAPPS_PIP_INDEX=<url> — индекс pypi контура

Образы — APP=$(subst $(space),|,$(APPS)) либо <цель>-<приложение>, без APP — оба:
  build                 boba-<приложение>:$(BOBA_TAG) и :latest; в agent — агент bin/boba.dist (Nuitka),
                        в mcp — образы корней плагинов и workspace из runtime/sandbox
  test                  build, затем проверка в образе без сети: пакеты, точки входа, у mcp —
                        образы плагинов и модели, у agent — ход агента; итог — RESULT: ALL OK
  boba-bin              агент каталогом -> release/$(VERSION)/bin/boba.dist; BIN_ONNX=1 —
                        bin/boba-onnx.dist с рантаймом onnxruntime-genai (вторая компиляция Nuitka)

Деревья запуска (тоже по APP):
  dev                   compose/<приложение>: boba.env, data, .env, юнит песочницы (mcp), владельцы
  debug                 debug/<приложение> и .vscode из образцов; для aid собирает фронт (web)
  site-keys             ключи site.toml, на которые ссылается общий конфиг
  release               дерево установки из образа -> release/$(VERSION)/<экземпляр>;
                        INSTANCE=<имя> (по умолчанию имя приложения), INSTALL_ROOT=$(INSTALL_ROOT),
                        BUNDLE_OS_LIBS=1 — с системными библиотеками образа в third/lib
  install               дерево релиза в INSTALL_ROOT/boba-<экземпляр> и юниты systemd пользователя

Фронт чата на хосте (для debug; образ собирает его сам):
  web                   page.js, UI chainlit (фронт + copilot) и vendor в assets пакета boba-aid
  chainlit-ui           UI chainlit заново

Обслуживание:
  version               версия пакетов
  version-set           проставить версию во все pyproject.toml: VER=X.Y.Z
  version-check         версии и пины boba-* совпадают
  vscode                .vscode из образцов build/conf/vscode; готовые файлы не трогает
  prune                 старые теги образов, образы без тега, кэш BuildKit до PRUNE_KEEP=$(PRUNE_KEEP).
                        Образы без тега — кэш классического сборщика: после prune сборке
                        снова нужны apt-зеркало и реестры
  clean                 release, артефакты fetch в build/src, собранный фронт, runtime/third и
                        runtime/sandbox (песочница и образы плагинов); модели остаются
  sec                   sec.sh, MODE=all|full|sast|sca|rules

Переменные: JOBS=$(JOBS) — заданий на шаг сборки; входит в ключ кэша, другое значение
пересобирает всё от base — держать одно. NO_CACHE=1, BOBA_TAG, BIN_ONNX=1, PLUGIN, VER,
MODE, INSTANCE, INSTALL_ROOT, BUNDLE_OS_LIBS=1, PRUNE_KEEP, PYTHON_VERSION=$(PYTHON_VERSION),
GLIBC_IMAGE, GCC_IMAGE, PYTHON_IMAGE, NODE_IMAGE, TESTAPPS_NODE_IMAGE. Версии и состав
входов: UV_VERSION, BWRAP_VERSION, LIBFUSE_VERSION, E2FSPROGS_VERSION, CHAINLIT (файл
UPSTREAM), KATEX_VERSION, INTER_VERSION, MERMAID_VERSION, ORACLEDB_VERSION, NUITKA_VERSION,
RAPIDOCR_LANGS, EMBED_MODELS, ONNX_MODELS, SANDBOX_WS_SIZE (размер workspace). PYTHON — python хоста
для скриптов сборки (.venv). Значения для машины — в conf/local.mk.

Сеть: в интернет ходит только fetch, остальные цели — только в реестры. apt — зеркало из
sources.list образов dmp/*: стадии base, oracledb-wheel, bwrap-build, fuse2fs-build,
sandbox-base, plugin-root, runtime-os. Классический сборщик проходит стадии до цели по
порядку Dockerfile, повторно — из кэша. pypi — conf/pip.conf, conf/uv.toml; npm — npmrc
образа nodejs; CA — conf/ca-chain.crt; прокси из env уходит в docker. BUILD_NETWORK — сеть
шагов сборки, CACHE_SCOPE — своя область кэша; их ставит offline-check. Зеркала fetch:
UV_URL, BWRAP_URL, LIBFUSE_URL, E2FSPROGS_URL, RAPIDOCR_REPO, KATEX_URL, INTER_URL,
MERMAID_URL, HF_URL, CHAINLIT_SRC_URL. Хосту нужны docker, make и python >= 3.11 (.venv).
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

# --- входы: fetch и образ сборки ---------------------------------------------------

.PHONY: fetch
fetch: $(INPUTS)
	@du -sh $(DEPS_DIR)/* $(MODELS_DIR)/*

$(BWRAP_TARBALL):
	@$(call fetch,$(BWRAP_URL),$@)

$(LIBFUSE_TARBALL):
	@$(call fetch,$(LIBFUSE_URL),$@)

$(E2FSPROGS_TARBALL):
	@$(call fetch,$(E2FSPROGS_URL),$@)

$(CHAINLIT_TARBALL):
	@$(call fetch,$(CHAINLIT_SRC_URL),$@)

$(UV_BIN):
	@tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(call fetch,$(UV_URL),$$tmp/uv.tar.gz)
	tar -xzf "$$tmp/uv.tar.gz" -C "$$tmp"
	install -D -m 0755 "$$tmp/uv-$(UV_PLATFORM)/uv" "$@"
	"$@" --version

# колесо патченного python-oracledb: стадия oracledb-wheel, наружу для .venv хоста.
# Пересобирается по патчу: прочие правки Dockerfile колесо не меняют
.PHONY: oracledb-wheel
oracledb-wheel: $(ORACLEDB_WHEEL)

$(ORACLEDB_WHEEL): $(SCRIPTS_DIR)/oracledb-$(ORACLEDB_VERSION)-arrow-duplicates.patch | base
	@echo "== колесо python-oracledb $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL) =="
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(call docker-image,oracledb-wheel,$$tmp/oracledb.iid)
	rm -rf "$(ORACLEDB_DIR)"
	$(call image-copy,$$(cat "$$tmp/oracledb.iid"),/out,$(ORACLEDB_DIR))
	$(call check,$@,колесо не собралось: смотри вывод стадии oracledb-wheel выше)
	echo ">>> колесо: $@"

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
	echo "== образ сборки $(BASE_IMAGE) =="
	$(call docker-stage,base,$(BASE_IMAGE),--tag boba-base:latest)

# --- песочница и плагины ---------------------------------------------------------

# песочница приложения: bwrap и fuse2fs, шаблон workspace
.PHONY: sandbox
sandbox: version-check
	@$(call check,$(SANDBOX_INPUTS),запусти: make fetch)
	echo "== песочница: bwrap, fuse2fs, mke2fs и шаблон workspace =="
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
	# режим файла уезжает в образ mcp как есть: классический COPY не умеет --chmod
	chmod 0644 "$$stage/workspace.ext4"
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
	$(call check,$(SANDBOX_INPUTS),запусти: make fetch)
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
	echo ">>> образ корня: $(PLUGIN_ROOTFS)"

# образы всех плагинов с entry points группы boba.tools
.PHONY: plugin-rootfs-all
plugin-rootfs-all: version-check
	@if [ -z "$(PLUGIN_PACKAGES)" ]; then
	    echo "плагинов с entry points boba.tools в $(SRC_DIR)/packages не найдено — проверь $(PYTHON): нужен .venv репозитория (source dev.sh)" >&2
	    exit 1
	fi
	for name in $(PLUGIN_PACKAGES); do
	    $(MAKE) -f "$(MAKEFILE)" plugin-rootfs PLUGIN=$$name
	done

# образы данных песочницы: веса моделей ext4-файлом из уже скачанного; входы проверяются,
# а не делаются — модели кладёт fetch
.PHONY: models-image
models-image: version-check
	@$(call check,$(SANDBOX_TOOLS),запусти: make sandbox)
	$(call check,$(foreach model,$(MODEL_IMAGES),$(MODEL_FILES_$(model))),запусти: make fetch)
	stage=
	trap 'rm -rf "$$stage"' EXIT
	$(foreach model,$(MODEL_IMAGES),$(call model-image,$(model))
	)

# --- тестовые образы -------------------------------------------------------------

# серверы MCP для тестов клиента; контекст — их каталог, пакеты npm ci и dist рендерера
# Prefab берёт из реестра образа nodejs, пакеты python — pip из pypi (TESTAPPS_PIP_INDEX —
# индекс закрытого контура) в дерево python образа PYTHON_IMAGE
TESTAPPS_DIR        := $(SRC_DIR)/tests/services/testapps
TESTAPPS_NODE_IMAGE ?= dmp/nodejs:22
TESTAPPS_PIP_INDEX  ?=
TESTAPPS_TAGS       ?= boba-testapps:$(BOBA_TAG) boba-testapps:latest

.PHONY: testapps
testapps:
	@echo "== тестовые серверы MCP: $(TESTAPPS_TAGS) от $(TESTAPPS_NODE_IMAGE) и $(PYTHON_IMAGE) =="
	$(DOCKER_BUILD) -f "$(BASE_DIR)/testapps.Dockerfile" $(NO_CACHE_OPT) $(PROXY_ARGS) $(CACHE_SCOPE_ARG) \
	    --build-arg NODE_IMAGE=$(TESTAPPS_NODE_IMAGE) --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) \
	    --build-arg PIP_INDEX_URL=$(TESTAPPS_PIP_INDEX) \
	    $(addprefix --tag ,$(TESTAPPS_TAGS)) "$(TESTAPPS_DIR)"

# --- проверка закрытого контура ---------------------------------------------------

# сборка целей OFFLINE_TARGETS без выхода в интернет. Шаги docker build и docker run
# сборки делят сеть контейнера-ловушки (docker --network container:<ловушка>), у которой
# сеть none: есть только loopback, маршрута наружу нет. На 127.0.0.1:53 ловушка пишет
# каждое имя, которое инструмент разрешает сам, мимо прокси; на 127.0.0.1:$(OFFLINE_PORT)
# передаёт соединения через unix-сокет в OFFLINE_DIR прокси, который стоит в сети docker
# по умолчанию и пускает только хосты реестров из конфигурации сборки (conf/pip.conf,
# conf/uv.toml, sources.list и npm config образов). Между контейнерами нет ни одного
# сетевого пакета: только файл сокета. Кэш сборщика — своя область CACHE_SCOPE: каждый шаг
# исполняется заново один раз. Журналы и итог — в OFFLINE_DIR: allow.jsonl, proxy.jsonl,
# dns.jsonl, marks.jsonl, build.log, report.txt
OFFLINE_TARGETS ?= sandbox models-image plugin-rootfs-all chainlit-ui web test testapps boba-bin
OFFLINE_DIR     ?= $(RELEASE_DIR)/offline-check
OFFLINE_NAME    := boba-offline-check
OFFLINE_PORT    := 3128
OFFLINE_CHECK   := $(PYTHON) $(SCRIPTS_DIR)/offline_check.py
OFFLINE_SOCKET  := /log/proxy.sock
# образы-основы стадий: недостающий демон взял бы из registry мимо прокси
OFFLINE_IMAGES   = $(GLIBC_IMAGE) $(GCC_IMAGE) $(PYTHON_IMAGE) $(NODE_IMAGE) $(TESTAPPS_NODE_IMAGE)

# контейнер проверки на python образа PYTHON_IMAGE (скрипт — только stdlib):
# $(1) — роль, $(2) — сеть, $(3) — команда offline_check.py
define offline-daemon
docker run -d --name "$(OFFLINE_NAME)-$(1)" $(2) -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(OFFLINE_DIR):/log" \
    -e LD_LIBRARY_PATH=/opt/python/usr/local/lib --entrypoint /opt/python/usr/local/bin/python3 \
    $(PYTHON_IMAGE) -u /scripts/offline_check.py $(3) > /dev/null
endef

# контейнеры проверки: и до запуска (остались от прерванного), и по выходу
offline-clean    = docker rm -f $(OFFLINE_NAME)-proxy $(OFFLINE_NAME)-jail > /dev/null 2>&1 || true

.PHONY: offline-check
offline-check: version-check
	@$(call check,$(IMAGE_INPUTS),запусти: make fetch)
	for image in $(OFFLINE_IMAGES); do
	    if ! docker image inspect "$$image" > /dev/null 2>&1; then
	        echo "нет образа $$image: docker build взял бы его из registry мимо прокси — загрузи его: docker load" >&2
	        exit 1
	    fi
	done
	$(offline-clean)
	trap '$(offline-clean)' EXIT
	rm -rf "$(OFFLINE_DIR)"
	mkdir -p "$(OFFLINE_DIR)"
	$(OFFLINE_CHECK) hosts --pip-conf "$(CONF_DIR)/pip.conf" --uv-toml "$(CONF_DIR)/uv.toml" \
	    $(OFFLINE_IMAGES:%=--apt-image %) --npm-image $(NODE_IMAGE) --npm-image $(TESTAPPS_NODE_IMAGE) \
	    --out "$(OFFLINE_DIR)/allow.jsonl"
	$(call offline-daemon,proxy,,proxy --allow /log/allow.jsonl --log /log/proxy.jsonl --socket $(OFFLINE_SOCKET))
	$(call offline-daemon,jail,--network none --dns 127.0.0.1,jail --log /log/dns.jsonl --socket $(OFFLINE_SOCKET) --port $(OFFLINE_PORT))
	for attempt in $$(seq 20); do
	    if [ -S "$(OFFLINE_DIR)/proxy.sock" ]; then
	        break
	    fi
	    sleep 0.5
	done
	if [ ! -S "$(OFFLINE_DIR)/proxy.sock" ]; then
	    echo "offline-check: прокси не открыл сокет $(OFFLINE_DIR)/proxy.sock за 10 с — смотри docker logs $(OFFLINE_NAME)-proxy" >&2
	    docker logs "$(OFFLINE_NAME)-proxy" >&2
	    exit 1
	fi
	proxy="http://127.0.0.1:$(OFFLINE_PORT)"
	scope="offline-$$(date +%Y%m%d%H%M%S)"
	echo "== проверка закрытого контура: $(OFFLINE_TARGETS); прокси $$proxy, кэш $$scope, журналы $(OFFLINE_DIR) =="
	build=0
	{
	    for target in $(OFFLINE_TARGETS); do
	        echo "== offline-check: make $$target =="
	        $(MAKE) -f "$(MAKEFILE)" "$$target" BUILD_NETWORK="container:$(OFFLINE_NAME)-jail" CACHE_SCOPE="$$scope" \
	            http_proxy="$$proxy" https_proxy="$$proxy" HTTP_PROXY="$$proxy" HTTPS_PROXY="$$proxy" \
	            no_proxy= NO_PROXY= || exit $$?
	    done
	} 2>&1 | $(OFFLINE_CHECK) stamp --marks "$(OFFLINE_DIR)/marks.jsonl" | tee "$(OFFLINE_DIR)/build.log" || build=$$?
	verdict=0
	$(OFFLINE_CHECK) report --allow "$(OFFLINE_DIR)/allow.jsonl" --proxy "$(OFFLINE_DIR)/proxy.jsonl" \
	    --dns "$(OFFLINE_DIR)/dns.jsonl" --marks "$(OFFLINE_DIR)/marks.jsonl" | tee "$(OFFLINE_DIR)/report.txt" || verdict=$$?
	if [ "$$build" != 0 ]; then
	    echo "offline-check: сборка упала с кодом $$build — причина в $(OFFLINE_DIR)/build.log; нарушения сети, если были, — выше" >&2
	    exit 1
	fi
	exit $$verdict

# --- агент boba каталогом standalone ---------------------------------------------

# стадия boba-bin компилирует boba-cli Nuitka внутри образа base; образ agent берёт ту же
# стадию с BIN_ONNX= — при пустом BIN_ONNX здесь компиляция одна на обе цели
.PHONY: boba-bin
boba-bin: version-check
	@$(call check,$(SANDBOX_INPUTS),запусти: make fetch)
	echo "== агент boba каталогом: Nuitka $(NUITKA_VERSION), JOBS=$(JOBS), BIN_ONNX=$(BIN_ONNX) =="
	tmp=
	trap 'rm -rf "$$tmp"' EXIT
	tmp=$$(mktemp -d)
	$(call docker-image,boba-bin-out,$$tmp/boba.iid,$(BIN_ARGS))
	mkdir -p "$(dir $(BIN_DIST))"
	rm -rf "$(BIN_DIST)"
	$(call image-copy,$$(cat "$$tmp/boba.iid"),/boba.dist,$(BIN_DIST))
	"$(BIN_FILE)" --help > /dev/null
	"$(BIN_FILE)" self-check
	echo ">>> агент: $(BIN_FILE) ($$(du -sh "$(BIN_DIST)" | cut -f1))"

# --- по приложению: образ, проверка, деревья ---------------------------------------

# <цель>-<приложение> — то же, что <цель> APP=<приложение>
build-% test-% dev-% debug-% release-% install-% site-keys-%:
	@$(MAKE) -f "$(MAKEFILE)" $(patsubst %-$*,%,$@) APP=$*

.PHONY: build test dev debug release install site-keys

ifeq ($(APP),)

build: $(APPS:%=build-%)
test: $(APPS:%=test-%)
dev: $(APPS:%=dev-%)
debug: $(APPS:%=debug-%)
release: $(APPS:%=release-%)
install: $(APPS:%=install-%)
site-keys: $(APPS:%=site-keys-%)

else

# образ приложения. У приложения с песочницей сборок три: стадия runtime-sandbox, поверх
# неё образы корней плагинов и workspace (sandbox-image), затем код приложения
build: version-check
	@$(call check,$(IMAGE_INPUTS),запусти: make fetch)
	tmp=
	trap 'rm -rf "$$tmp"' EXIT
	tmp=$$(mktemp -d)
	if [ -n "$(APP_SANDBOX)" ]; then
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
	    echo "== основа $(SANDBOX_IMAGE): runtime-sandbox и образы плагинов из $(SANDBOX_DIR) =="
	    $(call docker-image,runtime-sandbox,$$tmp/runtime-sandbox.iid,$(APP_ARGS))
	    $(call sandbox-image,$$tmp/runtime-sandbox.iid)
	fi
	echo "== образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR) =="
	$(call docker-stage,$(APP_STAGE),$(IMAGE),--tag boba-$(APP):latest $(APP_ARGS) $(if $(APP_SANDBOX),--build-arg SANDBOX_IMAGE=$(SANDBOX_IMAGE)))
	echo ">>> образ готов: $(IMAGE); проверка: make test APP=$(APP)"

# проверка образа тем же окружением, что уедет в compose и systemd: шаблон conf/boba.env
# с путями образа. Модели в образе не лежат — в релизе и compose они рядом, здесь —
# биндами. Сети у контейнера нет: модель хода агента живёт в нём же
test: build
	@$(call check,$(APP_MODEL_FILES),запусти: make fetch)
	$(call check,$(APP_MODEL_IMAGES:%=$(MODELS_DIR)/%.ext4),запусти: make models-image)
	echo "== проверка образа $(IMAGE) =="
	env_file=$$(mktemp)
	trap 'rm -f "$$env_file"' EXIT
	$(call app-env,$(DEST_DIR),$(DEST_DIR)/data) > "$$env_file"
	docker run --rm -i --network none --env-file "$$env_file" --tmpfs "$(DEST_DIR)/data:uid=1000,gid=1000" \
	    $(foreach model,$(APP_MODELS),-v "$(MODELS_DIR)/$(model):$(DEST_DIR)/models/$(model):ro") \
	    $(foreach model,$(APP_MODEL_IMAGES),-v "$(MODELS_DIR)/$(model).ext4:$(DEST_DIR)/models/$(model).ext4:ro") \
	    --entrypoint sh $(IMAGE) \
	    -c '"$$PYTHONHOME/bin/python3" - --app $(APP) --base $(DEST_DIR) --names $(DEST_DIR)/app/packages.txt $(if $(APP_SANDBOX),--sandbox --ocr $(notdir $(RAPIDOCR_FILES)) --model-images $(APP_MODEL_IMAGES))' \
	    < "$(TEST_DIR)/bobacheck.py"

# дерево установки из готового образа: пути и шебанги переводятся с DEST_DIR на INSTALL_DIR
release:
	@if ! docker image inspect "$(IMAGE)" > /dev/null 2>&1; then
	    echo "нет образа $(IMAGE) — запусти: make build APP=$(APP)" >&2
	    exit 1
	fi
	$(call check,$(APP_MODEL_FILES),запусти: make fetch)
	$(call check,$(APP_MODEL_IMAGES:%=$(MODELS_DIR)/%.ext4),запусти: make models-image)
	echo "== релиз $(APP) ($(INSTANCE)) из образа $(IMAGE) -> $(OUT) =="
	rm -rf "$(OUT)"
	mkdir -p "$(OUT)/conf" "$(OUT)/models"
	$(call image-copy,$(IMAGE),$(DEST_DIR)/.,$(OUT)/)
	for model in $(APP_MODELS); do
	    cp -a "$(MODELS_DIR)/$$model" "$(OUT)/models/$$model"
	done
	for model in $(APP_MODEL_IMAGES); do
	    cp "$(MODELS_DIR)/$$model.ext4" "$(OUT)/models/$$model.ext4"
	done
	# системные библиотеки образа (libssl1.1, libffi6, krb5 ...) в third/lib: нужны там,
	# где система не та, на которой собран образ, и этих версий в ней нет
	if [ -n "$(BUNDLE_OS_LIBS)" ]; then
	    docker run --rm --network none -v "$(SCRIPTS_DIR):/scripts:ro" --entrypoint sh "$(IMAGE)" \
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
# работает без Docker от того пользователя, который запустил цель. Части релиза (код,
# интерпретатор, агент, статика, модели, образы песочницы) и общий конфиг с плагинами
# заменяются целиком; conf/site.toml, conf/boba.env, conf/krb и data остаются.
# Работающий экземпляр цель не трогает — его сначала останавливают
install:
	@$(call check,$(OUT)/app $(OUT)/third $(OUT)/boba-$(INSTANCE).service,запусти: make release APP=$(APP))
	$(user-bus)
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
	for part in "$(OUT)"/*; do
	    name=$$(basename "$$part")
	    case "$$name" in
	        conf|data|*.service) continue ;;
	    esac
	    rm -rf "$(INSTALL_DIR)/$${name:?}"
	    cp -a "$$part" "$(INSTALL_DIR)/$$name"
	done
	mkdir -p "$(INSTALL_DIR)/conf/krb"
	$(call data-dirs,$(INSTALL_DIR)/data)
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

# рабочее дерево приложения для docker compose: conf, data и .env;
# docker-compose*.yml лежат в нём же и трекаются git'ом. Модели compose монтирует из
# runtime/, общий конфиг и образы песочницы лежат в образе. В conf дерева — только
# специфика машины: site.toml (пишет администратор), keytab и krb5.conf. Готовые
# .env и site.toml цель не трогает
dev: OUT = $(SRC_DIR)/compose/$(APP)
dev:
	@$(call check,$(APP_MODEL_FILES),запусти: make fetch)
	echo "== дерево compose $(APP) в $(OUT) =="
	$(call host-check,compose)
	$(call data-dirs,$(OUT)/data)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call sandbox-unit,$(APP))
	fi
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
	        echo "BOBA_ADDRESS_2="
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
# ассетов пакета (её собирает цель из WEB_<приложение>). site.toml отладки пишет
# администратор; цель его не порождает
debug: OUT = $(SRC_DIR)/debug/$(APP)
debug: $(WEB_$(APP)) vscode
	@$(call check,$(RUNTIME_LIBS),запусти: make sandbox)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	fi
	$(call check,$(APP_MODEL_FILES),запусти: make fetch)
	echo "== дерево отладки $(APP) в $(OUT) =="
	$(call host-check,debug)
	if [ -n "$(APP_SANDBOX)" ]; then
	    $(call sandbox-unit,debug)
	fi
	( umask 077; mkdir -p "$(OUT)/conf" )
	$(call data-dirs,$(OUT)/data)
	$(call site-hint,$(OUT)/conf)
	echo ">>> дерево отладки готово: $(OUT)"
	echo "    debug:   python -m $(APP_MODULE) --config $(APP_CONF)/config.toml --site $(OUT)/conf/site.toml"
	echo "    cgroup песочницы отладки для site.toml: $(call sandbox-cgroup,debug)"

# перечень ключей site.toml приложения: всё, на что общий конфиг и плагины ссылаются
# как ${env.*} и ${site.*}
site-keys:
	@$(PYTHON) "$(SCRIPTS_DIR)/site_keys.py" "$(APP_CONF)"

endif

# --- фронт чата на хосте: для отладки, образ собирает его стадиями Dockerfile ----------

.PHONY: web
web: $(PAGE_JS) $(UI_DIST)/index.html $(WEB_FILES)
	@rm -rf "$(ASSETS_VENDOR)"
	cp -a "$(WEBASSETS_DIR)" "$(ASSETS_VENDOR)"
	echo ">>> фронт чата: $(PAGE_JS), $(UI_DIST), $(ASSETS_VENDOR)"

# скрипт страницы node'ом образа: на хосте node не нужен
$(PAGE_JS): $(PAGE_SRCS)
	@echo "== скрипт страницы page.js: $(NODE_IMAGE) =="
	docker run --rm $(NETWORK_OPT) $(PROXY_ENV) --entrypoint /bin/sh -u "$(RUN_OWNER)" -e HOME=/tmp \
	    -e npm_config_update_notifier=false -v "$(AID_DIR):/pkg" -w /pkg/web/page $(NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run check && npm run build'
	ls -la "$@"

# UI chainlit (фронт + copilot) с overlay в assets/ui: тот же скрипт, что у стадии chainlit-ui-build;
# пересобирается, когда overlay, UPSTREAM, тарболл или скрипт новее сборки
$(UI_DIST)/index.html: $(UI_SRCS) $(CHAINLIT_TARBALL)
	@echo "== UI chainlit $(CHAINLIT_VERSION) (фронт + copilot) с overlay: $(NODE_IMAGE) -> $(UI_DIST) =="
	mkdir -p "$(UI_DIST)"
	docker run --rm $(NETWORK_OPT) $(PROXY_ENV) --entrypoint /bin/sh -u "$(RUN_OWNER)" \
	    -v "$(DEPS_DIR):/deps:ro" -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(UI_DIR):/ui:ro" \
	    -v "$(UI_ELEMENTS):/elements:ro" -v "$(UI_DIST):/out" $(NODE_IMAGE) \
	    /scripts/chainlit_ui.sh "/deps/$(notdir $(CHAINLIT_TARBALL))" /ui/overlay /elements /out
	touch "$@"
	ls -la "$(UI_DIST)/assets" | head -5

.PHONY: chainlit-ui
chainlit-ui:
	@rm -f "$(UI_DIST)/index.html"
	$(MAKE) -f "$(MAKEFILE)" "$(UI_DIST)/index.html"

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

# --- обслуживание ------------------------------------------------------------------

# всё целиком: артефакты, песочница, образы корней плагинов, фронт для отладки, образы
# с проверкой и compose-деревья всех приложений
.PHONY: all
all: fetch sandbox models-image plugin-rootfs-all web
	@$(MAKE) -f "$(MAKEFILE)" test APP=
	$(MAKE) -f "$(MAKEFILE)" dev APP=

.PHONY: sec
sec:
	@"$(SRC_DIR)/sec.sh" $(MODE)

# теги прошлых версий (образ под живым контейнером остаётся), затем образы без тега —
# результаты стадий и кэш классического сборщика — и кэш BuildKit до PRUNE_KEEP
.PHONY: prune
prune:
	@docker images --format '{{.Repository}}:{{.Tag}}' \
	    | { grep -E '^boba-($(subst $(space),|,$(APPS)))(-sandbox)?:' || true; } \
	    | { grep -v -E ':($(BOBA_TAG)|latest)$$' || true; } \
	    | { xargs -r -n1 docker rmi || true; }
	docker image prune -f
	docker builder prune -f --keep-storage $(PRUNE_KEEP)
	docker system df

# всё, что делают fetch, sandbox, plugin-rootfs, web и release, кроме моделей: те
# качаются долго
.PHONY: clean
clean:
	@echo "== чистка: release, артефакты fetch, собранный фронт, runtime/third и runtime/sandbox =="
	rm -rf $(RELEASE_DIR) $(DEPS_DIR)/uv $(WEBASSETS_DIR) $(ORACLEDB_DIR) $(FETCH_CONSTRAINTS) \
	       $(DEPS_DIR)/*.tar.xz $(DEPS_DIR)/*.tar.gz $(PAGE_JS) $(ASSETS_VENDOR) $(UI_DIST) \
	       $(THIRD_DIR) $(SANDBOX_DIR)
