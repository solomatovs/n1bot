# Сборка boba. Публичные цели: help, fetch, all, build APP=<деталь>, version,
# version-set. Каждая деталь проходит шаги, у каждого шага одно правило сети:
#   fetch   — curl, интернет; кладёт build/src
#   prepare — docker build, только реестры контура (apt, npm, pypi); образы boba-build-*
#   compile — docker run --network none; build/out
#   image   — docker build без сети; образы boba-mcp, boba, boba-aid
# Классический сборщик docker (DOCKER_BUILDKIT=0), в том числе rootless. Хосту нужны
# make, bash, coreutils, curl, tar и docker

THIS_MAKEFILE   := $(abspath $(lastword $(MAKEFILE_LIST)))
BUILD           := $(patsubst %/,%,$(dir $(THIS_MAKEFILE)))
ROOT            := $(abspath $(BUILD)/..)
-include $(BUILD)/conf/local.mk

SRC             := $(BUILD)/src
OUT             := $(BUILD)/out
CACHE           := $(BUILD)/cache
RUNTIME         := $(ROOT)/runtime
# каталоги программ: compile пишет в них сразу, image строит образ из каталога как есть
OUT_MCP         := $(OUT)/boba-mcp
OUT_BOBA        := $(OUT)/boba
OUT_AID         := $(OUT)/boba-aid
AID_ASSETS      := $(ROOT)/packages/apps/boba-aid/assets
PLUGIN_CONF_DIR := $(ROOT)/packages/apps/boba-mcp/conf/plugins

.DEFAULT_GOAL   := help
.ONESHELL:
.DELETE_ON_ERROR:
SHELL           := /bin/bash
.SHELLFLAGS     := -eu -o pipefail -c

# сколько рецептов идёт сразу и сколько заданий у компилятора в одной компиляции
# Nuitka: пик одной компиляции ~5 ГиБ, 4 × 8 заданий занимают 32 ядра
PARALLEL        ?= 4
JOBS            ?= 8
MAKEFLAGS       += -j$(PARALLEL) --output-sync=target --no-print-directory

VERSION         := $(shell sed -n 's/^version = "\(.*\)"$$/\1/p' $(ROOT)/packages/core/boba-core/pyproject.toml)
TAG             ?= $(VERSION)
NO_CACHE        ?=

PYTHON_VERSION  ?= 3.11
GLIBC_IMAGE     ?= dmp/glibc:2.28
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
NODE_IMAGE      ?= dmp/nodejs:20
TESTAPPS_NODE_IMAGE ?= dmp/nodejs:22

NUITKA_VERSION  ?= 4.3
# patchelf 0.18+ выравнивает новый сегмент по p_align библиотек образа (2 МБ):
# каждая .so, которой Nuitka правит RPATH, вырастает до 2 МБ; 0.17 добавляет килобайты
PATCHELF_VERSION ?= 0.17.2.4
ORDERED_SET_VERSION ?= 4.1.0
ORACLEDB_VERSION ?= 26.0.0
ORACLEDB_SHA256 ?= d15728bae7546984203ae17f8296153d52d10ffdaf4929bebe46b70d5819d270
ORACLEDB_LOCAL  ?= boba.1
CYTHON_VERSION  ?= 3.3.0
WORKSPACE_SIZE  ?= 1G

# --- что качает fetch ------------------------------------------------------------

UV_VERSION      ?= 0.11.21
UV_URL          ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-x86_64-unknown-linux-musl.tar.gz
BWRAP_VERSION   ?= 0.11.2
BWRAP_URL       ?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
# 3.12.0 — последний libfuse, которому хватает meson 0.49 из apt Astra 2.12
LIBFUSE_VERSION ?= 3.12.0
LIBFUSE_URL     ?= https://github.com/libfuse/libfuse/releases/download/fuse-$(LIBFUSE_VERSION)/fuse-$(LIBFUSE_VERSION).tar.xz
E2FSPROGS_VERSION ?= 1.47.4
E2FSPROGS_URL   ?= https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v$(E2FSPROGS_VERSION)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
CHAINLIT_VERSION := $(shell cat $(ROOT)/packages/apps/boba-aid/web/chainlit-ui/UPSTREAM)
CHAINLIT_URL    ?= https://codeload.github.com/Chainlit/chainlit/tar.gz/refs/tags/$(CHAINLIT_VERSION)
KATEX_VERSION   ?= 0.16.22
KATEX_URL       ?= https://github.com/KaTeX/KaTeX/releases/download/v$(KATEX_VERSION)/katex.tar.gz
INTER_VERSION   ?= 5.3.0
INTER_URL       ?= https://registry.npmjs.org/@fontsource/inter/-/inter-$(INTER_VERSION).tgz
INTER_WEIGHTS   ?= 400 500 700
MERMAID_VERSION ?= 11.16.1
MERMAID_URL     ?= https://registry.npmjs.org/mermaid/-/mermaid-$(MERMAID_VERSION).tgz

# модели: веса по закреплённым версиям и ревизиям, каталог на модель в build/src/models
MODELS          := rapidocr fastembed onnx-genai
HF_URL          ?= https://huggingface.co
RAPIDOCR_VERSION ?= 3.9.2
RAPIDOCR_URL    ?= https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v$(RAPIDOCR_VERSION)/onnx
RAPIDOCR_LANGS  ?= eslav
RAPIDOCR_FILES  := ch_PP-OCRv5_det_mobile.onnx ch_ppocr_mobile_v2.0_cls_mobile.onnx \
                   $(RAPIDOCR_LANGS:%=%_PP-OCRv5_rec_mobile.onnx)
# раскладка кэша Hugging Face, которую fastembed читает с local_files_only
FASTEMBED_REPO  := qdrant/multilingual-e5-large-onnx
FASTEMBED_REV   := 66076b8dc6e367337e3e90e6fb309fb0f3addaf6
FASTEMBED_CACHE := models--$(subst /,--,$(FASTEMBED_REPO))
FASTEMBED_FILES := config.json model.onnx model.onnx_data special_tokens_map.json \
                   tokenizer.json tokenizer_config.json
# локальные модели агента: каталог на модель, его называет профиль модели в конфиге агента
ONNX_MODELS     := qwen3-0.6b-int4 qwen3-1.7b-int4 qwen3-4b-int4
ONNX_URL_qwen3-0.6b-int4   := $(HF_URL)/xiaoyao9184/Qwen3-0.6B-onnx-genai/resolve/dfaa6540d16eb3e12eeac660ac80cf69cd5e282d/cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4
ONNX_FILES_qwen3-0.6b-int4 := chat_template.jinja genai_config.json model.onnx model.onnx.data \
                              tokenizer.json tokenizer_config.json
ONNX_URL_qwen3-1.7b-int4   := $(HF_URL)/Hug0007/qwen3-1.7b-int4-onnx-genai/resolve/fd1e1e4bc66ad91531525d645ebbbbe14a186a92
ONNX_FILES_qwen3-1.7b-int4 := chat_template.jinja genai_config.json model.onnx model.onnx.data \
                              tokenizer.json tokenizer_config.json
ONNX_URL_qwen3-4b-int4     := $(HF_URL)/robinsmits/qwen3-4b-instruct-2507-onnx-int4/resolve/97d8690faf0cad28279dd10334a67c79f673a57f
ONNX_FILES_qwen3-4b-int4   := added_tokens.json chat_template.jinja config.json genai_config.json \
                              generation_config.json merges.txt model.onnx model.onnx.data \
                              model_config.json special_tokens_map.json tokenizer.json \
                              tokenizer_config.json vocab.json

MODEL_FILES_rapidocr   := $(RAPIDOCR_FILES:%=$(SRC)/models/rapidocr/%)
MODEL_FILES_fastembed  := $(FASTEMBED_FILES:%=$(SRC)/models/fastembed/$(FASTEMBED_CACHE)/snapshots/$(FASTEMBED_REV)/%) \
                          $(SRC)/models/fastembed/$(FASTEMBED_CACHE)/refs/main
MODEL_FILES_onnx-genai := $(foreach m,$(ONNX_MODELS),$(ONNX_FILES_$(m):%=$(SRC)/models/onnx-genai/$(m)/%))

TARBALLS        := $(SRC)/bubblewrap-$(BWRAP_VERSION).tar.xz $(SRC)/fuse-$(LIBFUSE_VERSION).tar.xz \
                   $(SRC)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
CHAINLIT_TARBALL := $(SRC)/chainlit-$(CHAINLIT_VERSION).tar.gz
WEBASSETS       := $(SRC)/webassets/katex/katex.min.css $(SRC)/webassets/inter/inter.css \
                   $(SRC)/webassets/mermaid/mermaid.min.js
FETCHED         := $(SRC)/uv/uv $(TARBALLS) $(CHAINLIT_TARBALL) $(WEBASSETS) \
                   $(foreach m,$(MODELS),$(MODEL_FILES_$(m)))

# --- программы и плагины -----------------------------------------------------------

# программы на Nuitka: пакеты дерева, файл входа, исполняемый файл, флаги Nuitka
mcp_PACKAGES    := boba-mcp boba-db-postgres boba-db-oracle boba-db-clickhouse boba-transport-http
mcp_ENTRY       := boba/mcp_server/program.py
mcp_BIN         := boba-mcp
mcp_PORT        := 8650
mcp_FLAGS       :=
mcp_DEST        := $(OUT_MCP)/bin/boba-mcp.dist
boba_PACKAGES   := boba-cli boba-llm-onnx[generation]
boba_ENTRY      := boba/cli/app.py
boba_BIN        := boba
boba_PORT       := 8660
# onnxruntime_genai находит onnxruntime через find_spec, не импортом: пакет — явно
boba_FLAGS      := --user-package-configuration-file=/conf/nuitka-boba.yml \
                   --include-distribution-metadata=boba-cli --include-package=onnxruntime_genai \
                   --include-package-data=onnxruntime_genai --include-package=onnxruntime
boba_DEST       := $(OUT_BOBA)/bin/boba.dist
# чат пока на python: дерево пакетов и интерпретатор в каталоге программы
aid_PACKAGES    := boba-aid boba-llm-http
aid_PORT        := 8501
# группа плагинов — одна программа над несколькими плагинами: `run <id> <подкоманда>`
plugins_ENTRY   := boba/sandbox/program.py

# плагины, их программы и корни — из конфигов, уходящих в образ mcp: строки
# `program = "plugins/<группа>"` и `rootfs = "rootfs/<имя>.ext4"` секции [sandbox]
PLUGIN_MAP      := $(shell cd $(PLUGIN_CONF_DIR) && for conf in *.toml; do \
                       program=$$(sed -n 's|^ *program *= *"plugins/\([^"]*\)".*|\1|p' "$$conf"); \
                       rootfs=$$(sed -n 's|^ *rootfs *= *"rootfs/\([^"]*\)\.ext4".*|\1|p' "$$conf"); \
                       printf '%s:%s:%s ' "$${conf%.toml}" "$$program" "$$rootfs"; done)
PLUGINS         := $(foreach e,$(PLUGIN_MAP),$(word 1,$(subst :, ,$(e))))
GROUPS          := $(sort $(foreach e,$(PLUGIN_MAP),$(word 2,$(subst :, ,$(e)))))
ROOTFS          := $(sort $(foreach e,$(PLUGIN_MAP),$(word 3,$(subst :, ,$(e)))))
group-plugins    = $(foreach e,$(PLUGIN_MAP),$(if $(filter $(1),$(word 2,$(subst :, ,$(e)))),$(word 1,$(subst :, ,$(e)))))
rootfs-plugins   = $(foreach e,$(PLUGIN_MAP),$(if $(filter $(1),$(word 3,$(subst :, ,$(e)))),$(word 1,$(subst :, ,$(e)))))
group-rootfs     = $(sort $(foreach e,$(PLUGIN_MAP),$(if $(filter $(1),$(word 2,$(subst :, ,$(e)))),$(word 3,$(subst :, ,$(e))))))

# программы Nuitka и деревья пакетов: по ним make знает свои цели поимённо
NUITKA_PROGRAMS := mcp boba
SITES           := $(NUITKA_PROGRAMS) aid $(GROUPS:%=plugins-%)

DETAILS         := mcp boba aid rootfs models runtime testapps oracledb $(GROUPS:%=plugins-%) \
                   $(GROUPS:%=plugins-%-image)
APP             ?=
ifneq ($(filter build,$(MAKECMDGOALS)),)
ifeq ($(filter $(APP),$(DETAILS)),)
$(error APP=$(APP): such a detail does not exist, the details are: $(DETAILS))
endif
endif

# --- docker -----------------------------------------------------------------------

DOCKER_BUILD     = DOCKER_BUILDKIT=0 docker build --force-rm $(PROXY_ARGS)
# NO_CACHE=1 — сборка без кэша слоёв: заново проходят независимые сборки (стадия nuitka
# со всеми общими стадиями, деревья пакетов, корни, фронт, образы), а стадии
# base.Dockerfile после nuitka берут слои, собранные этим же прогоном
FRESH            = $(if $(NO_CACHE),--no-cache)
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))
CA_CHAIN        := $(wildcard $(BUILD)/conf/ca-chain.crt)
CURL            := curl -fsSL --retry 3 $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")
# от кого идёт docker run с каталогами хоста: rootless-демон отображает root контейнера
# на пользователя демона, обычному демону называется пользователь хоста
RUN_OWNER        = $(if $(findstring name=rootless,$(shell docker info --format '{{.SecurityOptions}}')),0:0,$(shell id -u):$(shell id -g))

BUILD_ARGS       = --build-arg PYTHON_VERSION=$(PYTHON_VERSION) --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE) \
    --build-arg GCC_IMAGE=$(GCC_IMAGE) --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)                       \
    --build-arg BWRAP_VERSION=$(BWRAP_VERSION) --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)          \
    --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION) --build-arg ORACLEDB_VERSION=$(ORACLEDB_VERSION) \
    --build-arg ORACLEDB_SHA256=$(ORACLEDB_SHA256) --build-arg ORACLEDB_LOCAL=$(ORACLEDB_LOCAL)        \
    --build-arg CYTHON_VERSION=$(CYTHON_VERSION) --build-arg NUITKA_VERSION=$(NUITKA_VERSION)          \
    --build-arg PATCHELF_VERSION=$(PATCHELF_VERSION) --build-arg ORDERED_SET_VERSION=$(ORDERED_SET_VERSION)

# стадия base.Dockerfile в образ: $(1) — стадия, $(2) — тег, $(3) — аргументы
define base-stage
$(DOCKER_BUILD) -f $(BUILD)/base.Dockerfile $(BUILD_ARGS) $(3) --target $(1) --tag $(2) $(ROOT)
endef

# каталог $(2) образа $(1) в каталог хоста $(3) целиком
define image-copy
rm -rf "$(3)" && mkdir -p "$(dir $(3))"
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# сумма дерева $(2) образа $(1) по путям, режимам и содержимому файлов (без времени):
# в файл $@ она пишется, только если изменилась, и правила за ним не перезапускаются
# зря. Файлы, которые docker кладёт в каждый контейнер, не считаются
define tree-sum
docker run --rm --network none --entrypoint sh $(1) -c 'cd "$(2)" \
    && find . -xdev \( -path ./proc -o -path ./sys -o -path ./dev -o -path ./.dockerenv \
                       -o -path ./etc/hosts -o -path ./etc/hostname -o -path ./etc/resolv.conf \) -prune \
              -o -printf "%p %m %U %G %l\n" | LC_ALL=C sort \
    && find . -xdev \( -path ./proc -o -path ./sys -o -path ./dev -o -path ./.dockerenv \
                       -o -path ./etc/hosts -o -path ./etc/hostname -o -path ./etc/resolv.conf \) -prune \
              -o -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum' | sha256sum > "$@.new"
if cmp -s "$@.new" "$@"; then rm "$@.new"; else mv "$@.new" "$@"; fi
endef

# --- справка ---------------------------------------------------------------------

define HELP
Сборка boba $(VERSION)

Цели:
  help                  эта справка (цель по умолчанию)
  fetch                 всё внешнее, что не из реестров, curl'ом в build/src: uv,
                        bubblewrap, libfuse, e2fsprogs, тег chainlit $(CHAINLIT_VERSION),
                        KaTeX/Inter/mermaid, модели $(MODELS); нужен интернет
  all                   все детали, кроме testapps, офлайн; на машине с интернетом — make fetch all
  build APP=<деталь>    одна деталь со всем, что ей нужно
  version               версия пакетов; падает, если версии или пины boba-* разошлись
  version-set VER=X.Y.Z версия и пины boba-* во всех pyproject.toml

Детали (APP=):
  mcp                   образ boba-mcp:$(TAG): сервер, программы плагинов ($(GROUPS)),
                        корни песочниц rootfs/{$(subst $(space),$(comma),$(ROOTFS))}.ext4, libexec, workspace.ext4.gz
  boba                  образ boba:$(TAG): агент с onnxruntime-genai, без моделей
  aid                   образ boba-aid:$(TAG): чат (python) и его фронт
  plugins-<группа>      программа группы плагинов -> build/out/boba-mcp/plugins/<группа>,
                        проверенная в корне каждого плагина; группы: $(GROUPS)
  plugins-<группа>-image  образ boba-plugins-<группа>:$(TAG): корень группы и её программа в
                        /plugin, из тех же артефактов; в all не входит
  rootfs                корни песочниц ext4 -> build/out/boba-mcp/rootfs
  models                каталоги моделей для админа -> build/out/models/{$(subst $(space),$(comma),$(MODELS))}
  runtime               запуск из исходников и тесты -> runtime/ (libexec, workspace, корни с
                        python, программы плагинов на python, модели), фронт чата -> assets boba-aid
  testapps              образ boba-testapps:$(TAG) — серверы MCP для тестов, в all не входит
  oracledb              колесо python-oracledb $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL) для .venv -> build/out/oracledb

Шаги и сеть: fetch — интернет (curl); prepare — docker build, только реестры
контура: apt из sources.list образов dmp/*, pypi из build/conf/pip.conf и uv.toml,
npm из build/conf/npmrc (без него — из образа nodejs), CA — build/conf/ca-chain.crt;
compile — docker run --network none; image — docker build без сети.

Переменные: PARALLEL=$(PARALLEL) (рецептов сразу), JOBS=$(JOBS) (заданий компилятора
в одной компиляции Nuitka), TAG=$(TAG), NO_CACHE=1 (docker build --no-cache),
версии и адреса внешних артефактов (*_VERSION, *_URL, HF_URL), образы dmp/*
(GLIBC_IMAGE, GCC_IMAGE, PYTHON_IMAGE, NODE_IMAGE). Значения машины — в build/conf/local.mk.
endef
empty           :=
space           := $(empty) $(empty)
comma           := ,

.PHONY: help all build version version-set fetch FORCE
help:
	@:$(info $(HELP))

FORCE:

all: app-mcp app-boba app-aid app-models app-runtime app-oracledb

build: app-$(APP)

# --- версия ------------------------------------------------------------------------

PYPROJECTS       = $(shell find $(ROOT)/packages -name pyproject.toml -not -path '*/node_modules/*')

version:
	@versions=$$(sed -n 's/^version = "\(.*\)"$$/\1/p' $(PYPROJECTS) | sort -u)
	pins=$$(grep -ohE '"boba-[a-z0-9-]+(\[[a-z0-9,_-]+\])?==[^"]+"' $(PYPROJECTS) | sed 's/.*==\(.*\)"/\1/' | sort -u)
	if [ "$$versions" != "$(VERSION)" ] || [ -n "$$(echo "$$pins" | grep -vx '$(VERSION)' || true)" ]; then
	    echo "versions of the packages differ: versions [$$(echo $$versions)], pins boba-*== [$$(echo $$pins)]; set one with make version-set VER=X.Y.Z" >&2
	    exit 1
	fi
	echo "$(VERSION)"

version-set:
	@if ! [[ "$(VER)" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$$ ]]; then
	    echo "version-set expects VER=X.Y.Z or X.Y.Z.devN, got '$(VER)'" >&2
	    exit 1
	fi
	sed -i -E -e 's/^version = ".*"$$/version = "$(VER)"/' \
	    -e 's/("boba-[a-z0-9-]+(\[[a-z0-9,_-]+\])?==)[^"]+"/\1$(VER)"/g' $(PYPROJECTS)
	echo "version $(VER) is set; refresh uv.lock: uv lock"

# --- fetch -------------------------------------------------------------------------

fetch: $(FETCHED)
	@du -sh $(SRC)/* | sort -k2

# в интернет ходит только fetch: входа, которого нет, другие цели не качают сами
define fetch-only
if [ -z "$(filter fetch,$(MAKECMDGOALS))" ]; then
    echo "missing $@: make fetch puts it there, the other goals do not go to the internet" >&2
    exit 1
fi
endef

# загрузка $(1) в файл $@ через временный файл рядом: оборванная загрузка не ляжет целью
define download
$(fetch-only)
mkdir -p "$(@D)"
$(CURL) -o "$@.part" "$(1)"
mv "$@.part" "$@"
endef

$(SRC)/bubblewrap-%.tar.xz:
	@$(call download,$(BWRAP_URL))

$(SRC)/fuse-%.tar.xz:
	@$(call download,$(LIBFUSE_URL))

$(SRC)/e2fsprogs-%.tar.xz:
	@$(call download,$(E2FSPROGS_URL))

$(CHAINLIT_TARBALL):
	@$(call download,$(CHAINLIT_URL))

$(SRC)/uv/uv:
	@$(fetch-only)
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(CURL) -o "$$tmp/uv.tar.gz" "$(UV_URL)"
	tar -xzf "$$tmp/uv.tar.gz" -C "$$tmp" --strip-components=1
	install -D -m 0755 "$$tmp/uv" "$@"

$(SRC)/webassets/katex/katex.min.css:
	@$(fetch-only)
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(CURL) -o "$$tmp/katex.tar.gz" "$(KATEX_URL)"
	tar -xzf "$$tmp/katex.tar.gz" -C "$$tmp" --wildcards katex/katex.min.css 'katex/fonts/*.woff2'
	mkdir -p "$(@D)/fonts"
	cp "$$tmp"/katex/fonts/*.woff2 "$(@D)/fonts/"
	cp "$$tmp/katex/katex.min.css" "$@"

$(SRC)/webassets/inter/inter.css:
	@$(fetch-only)
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(CURL) -o "$$tmp/inter.tgz" "$(INTER_URL)"
	tar -xzf "$$tmp/inter.tgz" -C "$$tmp"
	mkdir -p "$(@D)/files"
	: > "$$tmp/inter.css"
	for weight in $(INTER_WEIGHTS); do
	    cat "$$tmp/package/$$weight.css" >> "$$tmp/inter.css"
	    cp "$$tmp"/package/files/inter-*-$$weight-normal.woff2 "$(@D)/files/"
	done
	mv "$$tmp/inter.css" "$@"

$(SRC)/webassets/mermaid/mermaid.min.js:
	@$(fetch-only)
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(CURL) -o "$$tmp/mermaid.tgz" "$(MERMAID_URL)"
	tar -xzf "$$tmp/mermaid.tgz" -C "$$tmp" package/dist/mermaid.min.js
	install -D -m 0644 "$$tmp/package/dist/mermaid.min.js" "$@"

# OCR: детектор и классификатор общие, распознаватель — на язык (boba.doc OcrModel)
$(SRC)/models/rapidocr/ch_PP-OCRv5_det_mobile.onnx:
	@$(call download,$(RAPIDOCR_URL)/PP-OCRv5/det/$(@F))

$(SRC)/models/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx:
	@$(call download,$(RAPIDOCR_URL)/PP-OCRv4/cls/$(@F))

$(SRC)/models/rapidocr/%_PP-OCRv5_rec_mobile.onnx:
	@$(call download,$(RAPIDOCR_URL)/PP-OCRv5/rec/$(@F))

$(SRC)/models/fastembed/$(FASTEMBED_CACHE)/snapshots/$(FASTEMBED_REV)/%:
	@$(call download,$(HF_URL)/$(FASTEMBED_REPO)/resolve/$(FASTEMBED_REV)/$*)

$(SRC)/models/fastembed/$(FASTEMBED_CACHE)/refs/main:
	@$(fetch-only)
	mkdir -p "$(@D)"
	printf '%s' "$(FASTEMBED_REV)" > "$@"

$(SRC)/models/onnx-genai/%:
	@$(call download,$(ONNX_URL_$(firstword $(subst /, ,$*)))/$(@F))

# --- prepare: образы среды сборки ---------------------------------------------------

.PHONY: prepare-nuitka prepare-os prepare-pyos prepare-share
prepare-nuitka: $(SRC)/uv/uv $(TARBALLS)
	@$(call base-stage,nuitka,boba-build-nuitka:$(TAG),$(FRESH))
	$(call base-stage,base,boba-build-base:$(TAG))

# стадии base.Dockerfile идут после nuitka: она проходит общие стадии один раз, а
# параллельные сборки того же файла берут их из кэша, а не собирают заново
prepare-os: | prepare-nuitka
	@$(call base-stage,os,boba-build-os:$(TAG))

prepare-pyos: | prepare-nuitka
	@$(call base-stage,pyos,boba-build-pyos:$(TAG))

# share/ чата сразу в каталог программы aid
prepare-share: $(CHAINLIT_TARBALL) $(WEBASSETS) | prepare-os
	@$(DOCKER_BUILD) $(FRESH) -f $(BUILD)/ui.Dockerfile --build-arg NODE_IMAGE=$(NODE_IMAGE) \
	    --build-arg OS_IMAGE=boba-build-os:$(TAG) --build-arg CHAINLIT_VERSION=$(CHAINLIT_VERSION) \
	    --target share --tag boba-build-share:$(TAG) $(ROOT)
	$(call image-copy,boba-build-share:$(TAG),/share,$(OUT_AID)/share)

$(OUT_MCP)/libexec/bwrap $(OUT_MCP)/libexec/fuse2fs $(OUT_MCP)/libexec/mke2fs &: $(TARBALLS) $(SRC)/uv/uv \
        $(BUILD)/base.Dockerfile $(BUILD)/patches/e2fsprogs-$(E2FSPROGS_VERSION)-ro-mount.patch | prepare-nuitka
	@$(call base-stage,libexec,boba-build-libexec:$(TAG))
	$(call image-copy,boba-build-libexec:$(TAG),/libexec,$(OUT_MCP)/libexec)
	chmod -R go-w "$(OUT_MCP)/libexec"
	touch $(OUT_MCP)/libexec/*

# дерево пакетов детали $*: программы — пакеты $*_PACKAGES, группы плагинов — id плагинов
$(SITES:%=$(OUT)/site/%.sum): $(OUT)/site/%.sum: FORCE | prepare-nuitka
	@mkdir -p "$(@D)"
	$(DOCKER_BUILD) $(FRESH) -f $(BUILD)/site.Dockerfile --build-arg BUILD_IMAGE=boba-build-nuitka:$(TAG) \
	    --tag boba-build-site-$*:$(TAG) --build-arg PACKAGES="$($*_PACKAGES)" \
	    --build-arg PLUGINS="$(call group-plugins,$(patsubst plugins-%,%,$(filter plugins-%,$*)))" $(ROOT)
	$(call tree-sum,boba-build-site-$*:$(TAG),/opt/site)

# корень песочницы $* (плагины, чей конфиг его называет); -python — вариант для тестов
$(ROOTFS:%=$(OUT)/rootfs/%.sum): $(OUT)/rootfs/%.sum: FORCE | prepare-nuitka prepare-os
	@mkdir -p "$(@D)"
	$(DOCKER_BUILD) $(FRESH) -f $(BUILD)/rootfs.Dockerfile --build-arg BUILD_IMAGE=boba-build-nuitka:$(TAG) \
	    --build-arg OS_IMAGE=boba-build-os:$(TAG) --build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
	    --build-arg PLUGINS="$(call rootfs-plugins,$*)" --tag boba-build-rootfs-$*:$(TAG) $(ROOT)
	$(call tree-sum,boba-build-rootfs-$*:$(TAG),/)

$(ROOTFS:%=$(OUT)/rootfs-python/%.sum): $(OUT)/rootfs-python/%.sum: FORCE | prepare-nuitka prepare-os
	@mkdir -p "$(@D)"
	$(DOCKER_BUILD) $(FRESH) -f $(BUILD)/rootfs.Dockerfile --build-arg BUILD_IMAGE=boba-build-nuitka:$(TAG) \
	    --build-arg OS_IMAGE=boba-build-os:$(TAG) --build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
	    --build-arg PLUGINS="$(call rootfs-plugins,$*)" --build-arg PYTHON=1 \
	    --tag boba-build-rootfs-$*-python:$(TAG) $(ROOT)
	$(call tree-sum,boba-build-rootfs-$*-python:$(TAG),/)

# --- compile ----------------------------------------------------------------------

# ext4 корня: дерево образа tar'ом в mke2fs сборки, root'ом в контейнере glibc без сети
define rootfs-ext4
mkdir -p "$(@D)"
stage=$$(mktemp -d "$(@D)/.stage.XXXXXX")
trap 'rm -rf "$$stage"; docker rm -f "$$cid" > /dev/null 2>&1 || true' EXIT
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:/." - | docker run --rm -i --network none -e MKE2FS_CONFIG=/tools/mke2fs.conf \
    -e PATH=/tools:/usr/sbin:/usr/bin:/sbin:/bin -v "$(OUT_MCP)/libexec:/tools:ro" \
    -v "$(BUILD)/mkrootfs.sh:/mkrootfs.sh:ro" -v "$$stage:/out" --entrypoint sh $(GLIBC_IMAGE) \
    /mkrootfs.sh /out/rootfs.ext4 $(PYTHON_VERSION) 16 $(JOBS) "$(RUN_OWNER)"
chmod 0644 "$$stage/rootfs.ext4"
mv -f "$$stage/rootfs.ext4" "$@"
endef

$(ROOTFS:%=$(OUT_MCP)/rootfs/%.ext4): $(OUT_MCP)/rootfs/%.ext4: $(OUT)/rootfs/%.sum $(BUILD)/mkrootfs.sh | $(OUT_MCP)/libexec/mke2fs
	@$(call rootfs-ext4,boba-build-rootfs-$*:$(TAG))

$(ROOTFS:%=$(OUT)/rootfs-python/%.ext4): $(OUT)/rootfs-python/%.ext4: $(OUT)/rootfs-python/%.sum $(BUILD)/mkrootfs.sh | $(OUT_MCP)/libexec/mke2fs
	@$(call rootfs-ext4,boba-build-rootfs-$*-python:$(TAG))

# шаблон workspace сжат: слой образа дыр разреженного файла не хранит
$(OUT_MCP)/sandbox/workspace.ext4.gz: | $(OUT_MCP)/libexec/mke2fs
	@mkdir -p "$(@D)"
	docker run --rm --network none -e MKE2FS_CONFIG=/tools/mke2fs.conf -v "$(OUT_MCP)/libexec:/tools:ro" \
	    -v "$(@D):/out" --entrypoint sh $(GLIBC_IMAGE) -c ' \
	        truncate -s $(WORKSPACE_SIZE) /tmp/workspace.ext4 \
	        && /tools/mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L workspace /tmp/workspace.ext4 \
	        && gzip -n -c /tmp/workspace.ext4 > /out/workspace.ext4.gz.part \
	        && chown "$(RUN_OWNER)" /out/workspace.ext4.gz.part && chmod 0644 /out/workspace.ext4.gz.part'
	mv "$@.part" "$@"

# пакеты базового образа glibc: программе плагина на них опираться нельзя
$(OUT)/glibc-packages.txt:
	@mkdir -p "$(@D)"
	docker run --rm --network none --entrypoint dpkg-query $(GLIBC_IMAGE) -W -f '$${Package}\n' > "$@"

# компиляция Nuitka детали $(1) в каталог $(5) каталога программы: файл входа $(2),
# исполняемый файл $(3), флаги $(4)
define nuitka-compile
mkdir -p "$(CACHE)" "$(dir $(5))"
stage=$$(mktemp -d "$(dir $(5)).stage.XXXXXX")
trap 'rm -rf "$$stage"' EXIT
docker run --rm --network none -u "$(RUN_OWNER)" -v "$(CACHE):/cache" -v "$(BUILD)/conf:/conf:ro" \
    -v "$(BUILD)/nuitka.sh:/nuitka.sh:ro" -v "$$stage:/out" --entrypoint sh boba-build-site-$(1):$(TAG) \
    /nuitka.sh /opt/site "/opt/site/$(2)" "$(3)" $(1) $(JOBS) $(4) < /dev/null
rm -rf "$(5)"
mv "$$stage/$(1)" "$(5)"
endef

# сумма дерева пакетов ложится в $(OUT)/compiled последней: по ней make видит, что
# программа собрана из этого дерева
$(NUITKA_PROGRAMS:%=$(OUT)/compiled/%.sum): $(OUT)/compiled/%.sum: $(OUT)/site/%.sum $(BUILD)/nuitka.sh $(BUILD)/conf/nuitka.yml $(BUILD)/conf/nuitka-boba.yml
	@$(call nuitka-compile,$*,$($*_ENTRY),$($*_BIN).bin,$($*_FLAGS),$($*_DEST))
	cp "$<" "$@"

# проверка программы группы $(1) в её корне $(2): build/plugin_check.sh в образе
# корня без сети, программа на /plugin
define plugin-check
docker run --rm --network none -v "$(OUT_MCP)/plugins/$(1):/plugin:ro" \
    -v "$(BUILD)/plugin_check.sh:/plugin_check.sh:ro" -v "$(OUT)/glibc-packages.txt:/base.txt:ro" \
    --entrypoint sh boba-build-rootfs-$(2):$(TAG) /plugin_check.sh /base.txt \
    $(filter $(call rootfs-plugins,$(2)),$(call group-plugins,$(1)))

endef

# группа плагинов: программа `run` в plugins/<группа> каталога mcp, затем проверка в
# корне каждого её плагина
$(GROUPS:%=$(OUT)/compiled/plugins-%.sum): $(OUT)/compiled/plugins-%.sum: $(OUT)/site/plugins-%.sum $(BUILD)/nuitka.sh $(BUILD)/conf/nuitka.yml \
        $(BUILD)/plugin_check.sh $(OUT)/glibc-packages.txt | $(ROOTFS:%=$(OUT)/rootfs/%.sum)
	@$(call nuitka-compile,plugins-$*,$(plugins_ENTRY),run,$(plugins-$*_FLAGS),$(OUT_MCP)/plugins/$*)
	$(foreach rootfs,$(call group-rootfs,$*),$(call plugin-check,$*,$(rootfs)))
	cp "$<" "$@"

# чат на python: интерпретатор, дерево пакетов и вход в каталог программы, без сети
$(OUT)/compiled/aid.sum: $(OUT)/site/aid.sum
	@mkdir -p "$(OUT)/compiled" "$(OUT_AID)"
	stage=$$(mktemp -d "$(OUT_AID)/.stage.XXXXXX")
	trap 'rm -rf "$$stage"' EXIT
	docker run --rm --network none -u "$(RUN_OWNER)" -v "$$stage:/out" --entrypoint sh \
	    boba-build-site-aid:$(TAG) -c ' \
	        mkdir -p /out/bin /out/third/lib \
	        && cp -a /opt/python-slim/. /out/third/ \
	        && cp "$$(readlink -f /usr/lib/x86_64-linux-gnu/libstdc++.so.6)" /out/third/lib/libstdc++.so.6 \
	        && cp /usr/lib/x86_64-linux-gnu/libgcc_s.so.1 /out/third/lib/ \
	        && cp -a /opt/site /out/app \
	        && sed -i "1s|^#!.*python3.*|#!/app/boba/third/bin/python3|" /out/app/bin/*' < /dev/null
	printf '%s\n' '#!/bin/sh' 'root=$$(cd "$$(dirname "$$0")/.." && pwd -P)' \
	    'export BOBA_RUNTIME="$$root" PYTHONHOME="$$root/third" PYTHONPATH="$$root/app" PYTHONNOUSERSITE=1 PYTHONUTF8=1 LD_LIBRARY_PATH="$$root/third/lib"' \
	    'exec "$$root/third/bin/python3" -m boba.aid.main "$$@"' > "$$stage/bin/boba-aid"
	chmod 0755 "$$stage/bin/boba-aid"
	for part in bin third app; do
	    rm -rf "$(OUT_AID)/$$part"
	    mv "$$stage/$$part" "$(OUT_AID)/$$part"
	done
	cp "$<" "$@"

# --- image и детали ------------------------------------------------------------------

# образ программы из её каталога $(1) (он и есть контекст сборки): тег $(2), основа
# $(3), исполняемый файл $(4), порт $(5), самопроверка $(6)
define program-image
mkdir -p "$(1)/conf"
$(DOCKER_BUILD) $(FRESH) --network none -f $(BUILD)/program.Dockerfile --build-arg BASE=$(3) \
    --build-arg PROGRAM=$(4) --build-arg PORT=$(5) --build-arg CHECK="$(6)" \
    --tag $(2):$(TAG) "$(1)"
echo ">>> image $(2):$(TAG) from $(1) ($$(du -sh "$(1)" | cut -f1))"
endef

.PHONY: $(DETAILS:%=app-%)

app-mcp: $(OUT)/compiled/mcp.sum $(GROUPS:%=$(OUT)/compiled/plugins-%.sum) $(ROOTFS:%=$(OUT_MCP)/rootfs/%.ext4) \
        $(OUT_MCP)/sandbox/workspace.ext4.gz $(OUT_MCP)/libexec/bwrap | prepare-os
	@rm -rf "$(OUT_MCP)/conf/plugins"
	mkdir -p "$(OUT_MCP)/conf"
	cp -a "$(PLUGIN_CONF_DIR)" "$(OUT_MCP)/conf/plugins"
	$(call program-image,$(OUT_MCP),boba-mcp,boba-build-os:$(TAG),bin/boba-mcp.dist/boba-mcp.bin,$(mcp_PORT),self-check)

app-boba: $(OUT)/compiled/boba.sum | prepare-os
	@$(call program-image,$(OUT_BOBA),boba,boba-build-os:$(TAG),bin/boba.dist/boba.bin,$(boba_PORT),self-check)

app-aid: $(OUT)/compiled/aid.sum prepare-share | prepare-pyos
	@$(call program-image,$(OUT_AID),boba-aid,boba-build-pyos:$(TAG),bin/boba-aid,$(aid_PORT),--help)

app-rootfs: $(ROOTFS:%=$(OUT_MCP)/rootfs/%.ext4)

$(GROUPS:%=app-plugins-%): app-plugins-%: $(OUT)/compiled/plugins-%.sum

# docker-образ группы плагинов: её корень — образ rootfs.Dockerfile, её программа — в
# /plugin; оба из тех же артефактов, что у образа mcp. Корень у группы один: плагины
# группы, назвавшие разные корни, такого образа не дают
$(GROUPS:%=app-plugins-%-image): app-plugins-%-image: $(OUT)/compiled/plugins-%.sum
	@$(if $(word 2,$(call group-rootfs,$*)),$(error plugins-$*-image: the plugins of group $* name different roots: $(foreach p,$(call group-plugins,$*),$(p) names rootfs/$(word 3,$(subst :, ,$(filter $(p):$*:%,$(PLUGIN_MAP)))).ext4;) one image carries one root, give the group one rootfs in plugins/*.toml))
	$(DOCKER_BUILD) $(FRESH) --network none -f $(BUILD)/plugin.Dockerfile \
	    --build-arg ROOTFS=boba-build-rootfs-$(call group-rootfs,$*):$(TAG) \
	    --tag boba-plugins-$*:$(TAG) "$(OUT_MCP)/plugins/$*"
	echo ">>> image boba-plugins-$*:$(TAG): root $(call group-rootfs,$*), program plugins/$*"

# каталоги моделей для админа: копии скачанного, повтор докопирует только новое
app-models: $(foreach m,$(MODELS),$(MODEL_FILES_$(m)))
	@$(foreach m,$(MODELS),mkdir -p "$(OUT)/models/$(m)" && cp -a -u "$(SRC)/models/$(m)/." "$(OUT)/models/$(m)/"
	)
	du -sh $(OUT)/models/*

# runtime/ повторяет каталог программы mcp, только корни — с python, а программы
# плагинов — python исходников; фронт чата — в assets пакета boba-aid
app-runtime: $(ROOTFS:%=$(OUT)/rootfs-python/%.ext4) $(OUT_MCP)/sandbox/workspace.ext4.gz $(OUT_MCP)/libexec/bwrap \
        $(foreach m,$(MODELS),$(MODEL_FILES_$(m))) prepare-share
	@mkdir -p "$(RUNTIME)/sandbox" "$(RUNTIME)/rootfs" "$(RUNTIME)/plugins" "$(RUNTIME)/models"
	rm -rf "$(RUNTIME)/libexec"
	cp -a "$(OUT_MCP)/libexec" "$(RUNTIME)/libexec"
	cp "$(OUT_MCP)/sandbox/workspace.ext4.gz" "$(RUNTIME)/sandbox/workspace.ext4.gz.part"
	mv "$(RUNTIME)/sandbox/workspace.ext4.gz.part" "$(RUNTIME)/sandbox/workspace.ext4.gz"
	$(foreach rootfs,$(ROOTFS),cp "$(OUT)/rootfs-python/$(rootfs).ext4" "$(RUNTIME)/rootfs/$(rootfs).ext4.part" && mv "$(RUNTIME)/rootfs/$(rootfs).ext4.part" "$(RUNTIME)/rootfs/$(rootfs).ext4"
	)
	$(foreach group,$(GROUPS),mkdir -p "$(RUNTIME)/plugins/$(group)" && printf '%s\n' '#!/bin/sh' 'exec python3 -m boba.sandbox.program "$$@"' > "$(RUNTIME)/plugins/$(group)/run" && chmod 0755 "$(RUNTIME)/plugins/$(group)/run"
	)
	$(foreach m,$(MODELS),mkdir -p "$(RUNTIME)/models/$(m)" && cp -a -u "$(SRC)/models/$(m)/." "$(RUNTIME)/models/$(m)/"
	)
	rm -rf "$(AID_ASSETS)/ui" "$(AID_ASSETS)/public/vendor"
	cp -a "$(OUT_AID)/share/ui" "$(AID_ASSETS)/ui"
	cp -a "$(OUT_AID)/share/public/vendor" "$(AID_ASSETS)/public/vendor"
	cp "$(OUT_AID)/share/public/page.js" "$(AID_ASSETS)/public/page.js"
	chmod -R go-w "$(RUNTIME)/libexec"
	chmod go-w "$(RUNTIME)"
	echo ">>> runtime: $(RUNTIME), frontend: $(AID_ASSETS)"

app-testapps: | prepare-nuitka
	@$(DOCKER_BUILD) $(FRESH) -f $(ROOT)/tests/services/testapps/Dockerfile --build-arg NODE_IMAGE=$(TESTAPPS_NODE_IMAGE) \
	    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) --build-arg BASE_IMAGE=boba-build-base:$(TAG) \
	    --tag boba-testapps:$(TAG) --tag boba-testapps:latest $(ROOT)/tests/services/testapps

app-oracledb: | prepare-nuitka
	@$(call base-stage,oracledb,boba-build-oracledb:$(TAG))
	$(call image-copy,boba-build-oracledb:$(TAG),/out,$(OUT)/oracledb)
	ls -l $(OUT)/oracledb
