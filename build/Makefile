# boba-chainlit: fetch скачивает исходники в build/src, модели и uv — в build/out.
# all собирает образ с apt/npm/pypi через Nexus из настроек образов и conf/.
# Все sandbox входят в образ; модели подключаются отдельным volume.

# Пути считаются от каталога Makefile, независимо от cwd.
BASE_DIR        := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SRC_DIR         ?= $(abspath $(BASE_DIR)/..)
# вложенные вызовы: make -f build/Makefile не подставляет его в $(MAKE)
MAKEFILE        := $(BASE_DIR)/Makefile
BUILD_CONTEXT   ?= $(SRC_DIR)
SCRIPTS_DIR     := $(BASE_DIR)/scripts
CONF_DIR        := $(BASE_DIR)/conf
# Исходники и внешняя статика остаются в src между сборками.
DEPS_DIR        := $(SRC_DIR)/build/src

APP             := chainlit
APP_PACKAGES     = boba-chainlit $(PLUGIN_PACKAGES) boba-llm-http boba-llm-onnx[generation] boba-llm-fastembed
APP_HEALTH      := /
# порт и префикс по умолчанию в Docker-образе
APP_PORT        := 8501
APP_PREFIX      := /boba

.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.NOTPARALLEL:
.ONESHELL:
SHELL           := /bin/bash
.SHELLFLAGS     := -eu -o pipefail -c

PYTHON_VERSION  := 3.11

# Для чтения pyproject.toml и деклараций плагинов нужен Python проекта.
# Пока .venv нет (его создаёт dev.sh), берётся
# python версии проекта из PATH
ifeq ($(origin PYTHON),undefined)
PYTHON          := $(firstword $(wildcard $(SRC_DIR)/.venv/bin/python$(PYTHON_VERSION)) $(shell command -v python$(PYTHON_VERSION)))
endif
ifeq ($(PYTHON),)
$(error нет ни $(SRC_DIR)/.venv/bin/python, ни python$(PYTHON_VERSION) в PATH: поставь python $(PYTHON_VERSION) или задай PYTHON=<путь>)
endif

# Готовые бинарники и результаты сборки лежат в out.
BUILD_OUT       := $(BASE_DIR)/out
MODELS_DIR      := $(BUILD_OUT)/models
SANDBOX_DEV_KRB := $(SRC_DIR)/compose/conf/krb

# --- docker ---------------------------------------------------------------------

# квота CPU cgroup, в котором идёт make: квоту ставит и любой предок, поэтому берётся
# наименьшая по пути до корня; без квоты — nproc. Сам nproc квоту не видит
define cpu-quota
jobs=$$(nproc)
dir=/sys/fs/cgroup$$(sed -n 's/^0:://p' /proc/self/cgroup)
while [ "$$dir" != /sys/fs/cgroup ]; do
    if [ -r "$$dir/cpu.max" ]; then
        read -r quota period < "$$dir/cpu.max"
        if [ "$$quota" != max ]; then
            limit=$$(( (quota + period - 1) / period ))
            if [ "$$limit" -lt "$$jobs" ]; then
                jobs=$$limit
            fi
        fi
    fi
    dir=$${dir%/*}
done
echo "$$jobs"
endef

# число заданий одного компилирующего шага сборки (ninja, make, compileall, сборки и
# установки uv, esbuild фронта). BuildKit ведёт несколько стадий сразу, поэтому суммарно
# заданий бывает в два-три раза больше
ifeq ($(origin JOBS),undefined)
JOBS            := $(shell $(cpu-quota))
endif
PARALLEL        ?= 8

# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION         := $(shell $(PYTHON) -c 'import tomllib; print(tomllib.load(open("$(SRC_DIR)/packages/agents/boba-$(APP)/pyproject.toml", "rb"))["project"]["version"])')
# плагины приложения — пакеты с entry points группы boba.tools; списка в extras нет
PLUGIN_PACKAGES := $(shell $(PYTHON) $(SCRIPTS_DIR)/plugin_rootfs.py list $(SRC_DIR)/packages)
BOBA_TAG        ?= $(VERSION)

# корень приложения в Docker-образе
DEST_DIR        ?= /app/boba

IMAGE           := boba-$(APP):$(BOBA_TAG)
SANDBOX_IMAGE   := boba-$(APP)-sandbox:$(BOBA_TAG)

GLIBC_IMAGE     ?= dmp/glibc:2.28
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
NODE_IMAGE      ?= dmp/nodejs:20

# --- что скачивает fetch -------------------------------------------------------

UV_VERSION      ?= 0.11.21
UV_PLATFORM     ?= x86_64-unknown-linux-musl
UV_URL          ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz
UV_BIN          := $(BUILD_OUT)/uv/uv

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

HF_URL          ?= https://huggingface.co
# раскладка кэша Hugging Face, которую fastembed читает с local_files_only
FASTEMBED_REPO  ?= qdrant/multilingual-e5-large-onnx
FASTEMBED_REV   ?= $(or $(strip $(shell cat "$(MODELS_DIR)/fastembed/models--$(subst /,--,$(FASTEMBED_REPO))/refs/main" 2>/dev/null)),66076b8dc6e367337e3e90e6fb309fb0f3addaf6)
FASTEMBED_CACHE := models--$(subst /,--,$(FASTEMBED_REPO))
FASTEMBED_FILES := config.json model.onnx model.onnx_data special_tokens_map.json \
                   tokenizer.json tokenizer_config.json
# локальные модели агента: каталог на модель, его называет профиль модели в конфиге агента
ONNX_MODELS     ?= qwen3-0.6b-int4 qwen3-1.7b-int4 qwen3-4b-int4
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

FASTEMBED_TARGETS := $(FASTEMBED_FILES:%=$(MODELS_DIR)/fastembed/$(FASTEMBED_CACHE)/snapshots/$(FASTEMBED_REV)/%) \
                     $(MODELS_DIR)/fastembed/$(FASTEMBED_CACHE)/refs/main
ONNX_TARGETS     := $(foreach m,$(ONNX_MODELS),$(ONNX_FILES_$(m):%=$(MODELS_DIR)/onnx-genai/$(m)/%))

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
# версии пинится в boba-db-oracle, колесо для .venv хоста лежит в out/oracledb
ORACLEDB_VERSION ?= 26.0.0
ORACLEDB_SHA256  ?= d15728bae7546984203ae17f8296153d52d10ffdaf4929bebe46b70d5819d270
ORACLEDB_LOCAL   ?= boba.1
CYTHON_VERSION   ?= 3.3.0
ORACLEDB_DIR     := $(BUILD_OUT)/oracledb

# исходники chainlit: UI собирается из тега, версия — в UPSTREAM рядом с overlay
WEB_DIR         := $(SRC_DIR)/packages/agents/boba-chainlit
UI_DIR          := $(WEB_DIR)/web/chainlit-ui
CHAINLIT_VERSION := $(shell cat $(UI_DIR)/UPSTREAM)
CHAINLIT_SRC_URL ?= https://codeload.github.com/Chainlit/chainlit/tar.gz/refs/tags/$(CHAINLIT_VERSION)
CHAINLIT_TARBALL := $(DEPS_DIR)/chainlit-$(CHAINLIT_VERSION).tar.gz

INPUTS          := $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL) $(UV_BIN) \
                   $(RAPIDOCR_FILES) $(FASTEMBED_TARGETS) $(ONNX_TARGETS) $(WEB_FILES) $(CHAINLIT_TARBALL)

# --- фронт и sandbox -------------------------------------------------------------

ASSETS_DIR      := $(WEB_DIR)/assets
BUILD_INPUTS    := $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL) $(UV_BIN) $(WEB_FILES) $(CHAINLIT_TARBALL)

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

# прокси передаются аргументами docker build
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))

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
    --build-arg SANDBOX_WS_SIZE=$(SANDBOX_WS_SIZE)                     \
    --build-arg JOBS=$(JOBS)

# аргументы стадий одного приложения
APP_ARGS         = --build-arg APP_PACKAGES="$(APP_PACKAGES)"                      \
    --build-arg APP_PORT="$(APP_PORT)" --build-arg APP_PREFIX="$(APP_PREFIX)" \
    --build-arg HEALTH_PATH="$(APP_HEALTH)"                         \
    --build-arg SANDBOX_DEV_KRB="$(SANDBOX_DEV_KRB)"                \
    --build-arg BOBA_DIR="$(DEST_DIR)"

# сборка стадии в образ: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --tag $(2) $(PROGRESS) $(BUILD_CONTEXT)
endef

# образ стадии без тега: $(1) — стадия, $(2) — файл для ID
# образа, $(3) — аргументы. Лог сборки уходит в stderr: stdout рецепта бывает занят tar'ом
define docker-image
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --iidfile "$(2)" $(PROGRESS) $(BUILD_CONTEXT) >&2
endef

# Внешние URL разрешены только явной цели fetch.
define fetch-only
$(if $(filter fetch,$(MAKECMDGOALS)),,echo 'Нет $@: сначала выполни make fetch' >&2; exit 1)
endef

define fetch
$(fetch-only)
mkdir -p "$(dir $(2))"
curl -fSL $(CURL_OPTS) -o "$(2).part" "$(1)"
mv "$(2).part" "$(2)"
endef

# Скопировать дерево из локального Docker-образа.
define image-copy
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# --- цели -----------------------------------------------------------------------

define HELP
Сборка boba-$(APP) $(VERSION)
  fetch                 скачать исходники в build/src, модели и uv в build/out
  all                   собрать все sandbox, фронт и Docker-образ
  plugin-rootfs         обновить sandbox в Docker: PLUGIN=boba-tool-postgres
  web                   собрать фронт в assets пакета для F5 на хосте
  version               показать версию приложения
  version-set VER=X.Y.Z  изменить версии пакетов и их внутренние пины

Порядок: make fetch; make all
Внешние загрузки выполняет только fetch. all использует локальный src
и реестры из настроек образов и conf/. Песочницы собираются внутри Docker
и входят в образ приложения; на хост не выгружаются.
Переменные: BOBA_TAG, PARALLEL=$(PARALLEL) (плагинов одновременно),
JOBS=$(JOBS) (заданий внутри одной сборки), NO_CACHE=1.
endef

.PHONY: help version version-set fetch plugin-rootfs all web
help:
	@:$(info $(HELP))

version:
	@echo $(VERSION)

version-set:
	@$(if $(VER),,$(error укажи VER=X.Y.Z))
	$(PYTHON) - "$(SRC_DIR)/packages" "$(VER)" <<-'PYCODE'
	import pathlib, re, sys, tomllib
	root, version = pathlib.Path(sys.argv[1]), sys.argv[2]
	paths = [p for p in root.rglob('pyproject.toml') if not any(x in p.relative_to(root).parts for x in ('build', 'dist', 'node_modules', '.venv'))]
	names = {tomllib.loads(p.read_text())['project']['name'] for p in paths}
	pins = re.compile(r'("(?:' + '|'.join(re.escape(n) for n in sorted(names)) + r')(?:\[[^\]]+\])?==)[^"]+')
	for path in paths: path.write_text(pins.sub(lambda m: m[1] + version, re.sub(r'^version = ".*"$$', 'version = "' + version + '"', path.read_text(), count=1, flags=re.M)))
	print('Версия пакетов:', version)
	PYCODE

fetch: $(INPUTS)
	@du -sh "$(DEPS_DIR)" "$(MODELS_DIR)" "$(dir $(UV_BIN))"

$(BWRAP_TARBALL):
	@$(call fetch,$(BWRAP_URL),$@)

$(LIBFUSE_TARBALL):
	@$(call fetch,$(LIBFUSE_URL),$@)

$(E2FSPROGS_TARBALL):
	@$(call fetch,$(E2FSPROGS_URL),$@)

$(CHAINLIT_TARBALL):
	@$(call fetch,$(CHAINLIT_SRC_URL),$@)

# dev.sh запрашивает этот каталог только при отсутствии колеса для .venv.
.PHONY: $(ORACLEDB_DIR)
$(ORACLEDB_DIR): $(SCRIPTS_DIR)/oracledb-$(ORACLEDB_VERSION)-arrow-duplicates.patch $(BASE_DIR)/Dockerfile | $(UV_BIN)
	@echo "== python-oracledb $(ORACLEDB_VERSION)+$(ORACLEDB_LOCAL) =="
	tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	$(call docker-image,oracledb-wheel,$$tmp/oracledb.iid)
	rm -rf "$(ORACLEDB_DIR)"
	$(call image-copy,$$(cat "$$tmp/oracledb.iid"),/out,$(ORACLEDB_DIR))
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

$(MODELS_DIR)/fastembed/$(FASTEMBED_CACHE)/snapshots/$(FASTEMBED_REV)/%:
	@$(call fetch,$(HF_URL)/$(FASTEMBED_REPO)/resolve/$(FASTEMBED_REV)/$*,$@)

$(MODELS_DIR)/fastembed/$(FASTEMBED_CACHE)/refs/main:
	@$(fetch-only)
	mkdir -p "$(@D)"
	printf '%s' "$(FASTEMBED_REV)" > "$@"

$(MODELS_DIR)/onnx-genai/%:
	@$(call fetch,$(ONNX_URL_$(firstword $(subst /, ,$*)))/$(@F),$@)

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

# --- sandbox: ext4 остаются в служебном Docker-образе ---------------------------

plugin-rootfs: $(BUILD_INPUTS)
	@$(if $(PLUGIN),,$(error укажи PLUGIN=<имя пакета>))
	base="$(SANDBOX_BASE)"
	if [ -z "$$base" ]; then
	    base="$(SANDBOX_IMAGE)"
	    docker image inspect "$$base" > /dev/null 2>&1 || base=scratch
	fi
	$(call docker-stage,sandbox-images,$(SANDBOX_IMAGE),$(APP_ARGS) --build-arg PLUGIN=$(PLUGIN) --build-arg SANDBOX_IMAGE="$$base")

all: $(BUILD_INPUTS)
	@[[ "$(PARALLEL)" =~ ^[1-9][0-9]*$$ ]] || { echo 'PARALLEL должен быть целым числом больше нуля' >&2; exit 1; }
	build_plugin() {
	    local name="$$1"
	    $(call docker-stage,plugin-output,$(SANDBOX_IMAGE)-$$name,$(APP_ARGS) --build-arg PLUGIN="$$name")
	}
	export -f build_plugin
	printf '%s\0' $(PLUGIN_PACKAGES) | xargs -0 -r -n 1 -P "$(PARALLEL)" bash -c 'build_plugin "$$1"' _
	base=scratch
	for name in $(PLUGIN_PACKAGES); do
	    $(call docker-stage,sandbox-images,$(SANDBOX_IMAGE),$(APP_ARGS) --build-arg PLUGIN="$$name" --build-arg PLUGIN_IMAGE="$(SANDBOX_IMAGE)-$$name" --build-arg SANDBOX_IMAGE="$$base")
	    base="$(SANDBOX_IMAGE)"
	done
	$(call docker-stage,runtime-$(APP),$(IMAGE),--tag boba-$(APP):latest $(APP_ARGS) --build-arg SANDBOX_IMAGE=$(SANDBOX_IMAGE))

web: $(BUILD_INPUTS)
	@tmp=$$(mktemp -d)
	trap 'rm -rf "$$tmp"' EXIT
	mkdir -p "$(ASSETS_DIR)/public"
	$(call docker-image,chainlit-ui-build,$$tmp/ui.iid)
	rm -rf "$(ASSETS_DIR)/ui"
	$(call image-copy,$$(cat "$$tmp/ui.iid"),/out/ui,$(ASSETS_DIR)/ui)
	$(call docker-image,web-build,$$tmp/page.iid)
	$(call image-copy,$$(cat "$$tmp/page.iid"),/src/assets/public/page.js,$(ASSETS_DIR)/public/page.js)
	rm -rf "$(ASSETS_DIR)/public/vendor"
	cp -a "$(WEBASSETS_DIR)" "$(ASSETS_DIR)/public/vendor"
