# от каталога Makefile, а не от cwd: make -f build/Makefile из корня искал бы
# test/ и conf/ рядом с собой
BASE_DIR      	?= $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SRC_DIR       	?= $(abspath $(BASE_DIR)/..)
ARTIFACTS     	?= $(BASE_DIR)/artifacts
BUILD_CONTEXT 	?= $(SRC_DIR)
TEST_DIR      	?= $(BASE_DIR)/test
CONF_DIR      	:= $(BASE_DIR)/conf

# исходники, которые собираются внутри Dockerfile
SRC_ARTIFACTS 	:= $(ARTIFACTS)/src
# бинарные зависимости, которые внутри Dockerfile не достать
DEPS_DIR      	?= $(ARTIFACTS)


.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c


# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION_FILE := $(SRC_DIR)/packages/core/boba-toolkit/pyproject.toml
VERSION      := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))
BOBA_TAG     ?= $(VERSION)
REQUIRES_PY  := $(shell sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' $(VERSION_FILE))

# релиз для make extract
RELEASE_NAME  := boba-$(VERSION)
# корень установки на сервере: в него уедет дерево из make extract
INSTALL_DIR   ?= /srv/boba
# тот же корень внутри образа; версия живёт в app/<version>, не в нём
DEST_DIR      ?= /app/boba

# финальный runtime-образ
IMAGE         := boba:$(BOBA_TAG)
# из какого образа make extract достаёт портативную папку и куда её кладёт
RELEASE_IMAGE ?= $(IMAGE)
OUT           ?= $(ARTIFACTS)/build/$(RELEASE_NAME)

PYTHON_VERSION	?= 3.11
# чистая рантайм-база (только glibc), от неё строится финальный образ runtime
GLIBC_IMAGE  	?= dmp/glibc:2.28
# среда сборки: glibc 2.28 + gcc + python-дистрибутив
GCC_IMAGE    	?= dmp/gcc:8.5.0
PYTHON_IMAGE 	?= dmp/python:$(PYTHON_VERSION)-dist
# базовый rootfs песочницы: стадия sandbox-rootfs (glibc + office + точки)
ROOTFS_IMAGE	:= boba-sandbox-rootfs:$(PYTHON_VERSION)
# site-packages песочницы одним каталогом: стадия sandbox-site
SITE_IMAGE		:= boba-sandbox-site:$(PYTHON_VERSION)
# шаблон workspace и fuse2fs: стадия fuse2fs-build
FUSE_IMAGE		:= boba-fuse2fs:$(PYTHON_VERSION)
# артефакты песочницы: rootfs, python, site и данные подключаются профилями
SANDBOX_DIR		:= $(DEPS_DIR)/sandbox
# нативное окружение liteparse: office/растр/pdf он конвертирует этими
# программами. Убрать libreoffice — doc перестанет читать docx/xlsx/pptx
LITEPARSE_PACKAGES	?= imagemagick libreoffice-writer libreoffice-calc libreoffice-impress ghostscript fonts-dejavu-core
# тег с версией python: иначе deps соберутся на boba-base от прошлой сборки
BASE_IMAGE    := boba-base:$(PYTHON_VERSION)

# OCR-модели tesseract
TESS_LANGS    	?= eng rus osd
TESSDATA_REPO 	?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed
EMBED_MODELS  	?= intfloat/multilingual-e5-large

# фронт chainlit тянет шрифт и стили формул с CDN; в закрытом контуре их
# кладут в data/public/vendor, а ссылки в index.html переписывает Dockerfile
WEBASSETS_DIR 	:= $(DEPS_DIR)/webassets
KATEX_VERSION 	?= 0.16.22
KATEX_URL     	?= https://github.com/KaTeX/KaTeX/releases/download/v$(KATEX_VERSION)/katex.tar.gz
INTER_VERSION 	?= 5.3.0
INTER_URL     	?= https://registry.npmjs.org/@fontsource/inter/-/inter-$(INTER_VERSION).tgz
# начертания Inter, которые запрашивает фронт
INTER_WEIGHTS 	?= 400 500 700

# готовый бинарь; для внутреннего mirror переопредели UV_URL
UV_VERSION  	?= 0.11.21
UV_PLATFORM 	?= x86_64-unknown-linux-musl
UV_URL      	?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# bubblewrap: собирается стадией bwrap-build
BWRAP_VERSION	?= 0.11.2
BWRAP_URL     	?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
BWRAP_TARBALL 	:= $(SRC_ARTIFACTS)/bubblewrap-$(BWRAP_VERSION).tar.xz

# fuse2fs + libfuse: собираются стадией fuse2fs-build.
# 3.12.0 — последний релиз, которому хватает meson 0.49 из apt Astra 2.12
# (3.13+ требуют >= 0.50); релизы до 3.15 включительно выложены как .tar.xz
LIBFUSE_VERSION   ?= 3.12.0
LIBFUSE_URL       ?= https://github.com/libfuse/libfuse/releases/download/fuse-$(LIBFUSE_VERSION)/fuse-$(LIBFUSE_VERSION).tar.xz
LIBFUSE_TARBALL   := $(SRC_ARTIFACTS)/fuse-$(LIBFUSE_VERSION).tar.xz
E2FSPROGS_VERSION ?= 1.47.2
E2FSPROGS_URL     ?= https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v$(E2FSPROGS_VERSION)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
E2FSPROGS_TARBALL := $(SRC_ARTIFACTS)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
# размер шаблонного workspace-образа: лимит диска на тред
SANDBOX_WS_SIZE   ?= 1G

# сбросить ли кэш сборки: docker build --no-cache
NO_CACHE		?= 0
NO_CACHE_OPT	:= $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# --progress есть только у buildkit; на старом docker флага нет — не подставляем
PROGRESS      ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: CA внутреннего TLS (nexus). Кладётся в conf/ рядом с
# pip.conf и uv.toml; artifacts/ поддержан для совместимости
CA_CHAIN := $(firstword $(wildcard $(CONF_DIR)/ca-chain.crt) $(wildcard $(ARTIFACTS)/ca-chain.crt))
# curl на хосте: прокси берёт из env, CA — отсюда
CURL_OPTS := $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")

# nexus в docker run: каталогом, поштучный bind-mount ломается о /etc/pip.conf,
# который стадия base уже создала. CA берётся из conf/, как и в Dockerfile
CONF_OPTS := -v "$(CONF_DIR)":/tmp/conf:ro                                                    \
             $(if $(wildcard $(CONF_DIR)/pip.conf),-e PIP_CONFIG_FILE=/tmp/conf/pip.conf)     \
             $(if $(wildcard $(CONF_DIR)/uv.toml),-e UV_CONFIG_FILE=/tmp/conf/uv.toml)        \
             $(if $(wildcard $(CONF_DIR)/ca-chain.crt),-e REQUESTS_CA_BUNDLE=/tmp/conf/ca-chain.crt \
                 -e CURL_CA_BUNDLE=/tmp/conf/ca-chain.crt)

# прокси в docker build: apt/pip/uv внутри стадий ходят в nexus через него
PROXY_VARS := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))

# общие build-arg: образы тулчейна и версии
DOCKER_ARGS = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)             \
              --build-arg GCC_IMAGE=$(GCC_IMAGE)                 \
              --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)           \
              --build-arg PYTHON_VERSION=$(PYTHON_VERSION)       \
              --build-arg BWRAP_VERSION=$(BWRAP_VERSION)         \
              --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)     \
              --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION) \
              --build-arg SANDBOX_WS_SIZE=$(SANDBOX_WS_SIZE)     \
              --build-arg LITEPARSE_PACKAGES="$(LITEPARSE_PACKAGES)"


.PHONY: help
help:
	@echo "Сборка boba $(VERSION) из дерева пакетов $(SRC_DIR) -> $(IMAGE)"
	echo ""
	echo "  make sources   исходники, собираемые внутри Dockerfile -> $(SRC_ARTIFACTS)"
	echo "  make deps      бинарные зависимости -> $(DEPS_DIR)/"
	echo "                 {uv,tessdata,fastembed,webassets,"
	echo "                  sandbox/{rootfs,python,site,data,workspace.ext4}}"
	echo "                 цели по отдельности: uv, tessdata, fastembed,"
	echo "                 webassets, sandbox-rootfs, sandbox-site,"
	echo "                 sandbox-python, sandbox-data, sandbox-workspace"
	echo "  make build     собрать образ $(IMAGE)"
	echo "  make test      проверить релиз внутри образа"
	echo "  make extract   дерево установки из готового образа:"
	echo "                 RELEASE_IMAGE=$(RELEASE_IMAGE), OUT=$(OUT)"
	echo "                 {third,sandbox,app/$(VERSION)/{boba-chainlit,boba-tool},conf,data}"
	echo "                 ставится в INSTALL_DIR=$(INSTALL_DIR)"
	echo "  make clean     удалить исходники, deps и собранную папку (кроме fastembed)"
	echo ""
	echo "  make all       sources + deps + build + test"
	echo ""
	echo "  Версия (одна на все пакеты, лежит в pyproject.toml):"
	echo "    version        показать"
	echo "    version-set    VER=X.Y.Z — проставить во все пакеты и во внутренние пины"
	echo "    version-check  проверить, что версия одна и пины boba-* совпадают"
	echo ""
	echo "  Закрытый контур (offline + nexus):"
	echo "    conf/pip.conf, conf/uv.toml  индексы пакетов (примеры рядом)"
	echo "    conf/ca-chain.crt            CA внутреннего TLS: ставится в bundle"
	echo "                                 образа и подставляется в curl хоста"
	echo "    http_proxy/https_proxy/no_proxy из env уходят в docker build"
	echo "    Зеркала tarball'ов (иначе github/kernel.org/npm):"
	echo "      UV_URL, BWRAP_URL, LIBFUSE_URL, E2FSPROGS_URL, TESSDATA_REPO,"
	echo "      KATEX_URL, INTER_URL"
	echo ""
	echo "  Переменные: INSTALL_DIR=$(INSTALL_DIR), DEST_DIR=$(DEST_DIR),"
	echo "              BOBA_TAG=<тег>, NO_CACHE=1,"
	echo "              PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE=$(GLIBC_IMAGE),"
	echo "              GCC_IMAGE=$(GCC_IMAGE), PYTHON_IMAGE=$(PYTHON_IMAGE)"
	echo "  Пакетам нужен python >=$(REQUIRES_PY)"


.PHONY: debug
debug:
	@true
	$(info BASE_DIR       = $(BASE_DIR))
	$(info SRC_DIR        = $(SRC_DIR))
	$(info ARTIFACTS      = $(ARTIFACTS))
	$(info SRC_ARTIFACTS  = $(SRC_ARTIFACTS))
	$(info DEPS_DIR       = $(DEPS_DIR))
	$(info TEST_DIR       = $(TEST_DIR))
	$(info VERSION        = $(VERSION))
	$(info RELEASE_NAME   = $(RELEASE_NAME))
	$(info IMAGE          = $(IMAGE))
	$(info RELEASE_IMAGE  = $(RELEASE_IMAGE))
	$(info OUT            = $(OUT))
	$(info DEST_DIR       = $(DEST_DIR))
	$(info INSTALL_DIR    = $(INSTALL_DIR))
	$(info BASE_IMAGE     = $(BASE_IMAGE))
	$(info GLIBC_IMAGE    = $(GLIBC_IMAGE))
	$(info GCC_IMAGE      = $(GCC_IMAGE))
	$(info PYTHON_IMAGE   = $(PYTHON_IMAGE))
	$(info REQUIRES_PY    = $(REQUIRES_PY))
	$(info NO_CACHE       = $(NO_CACHE))
	$(info UV_URL         = $(UV_URL))
	$(info CA_CHAIN       = $(CA_CHAIN))


# Все pyproject.toml пакетов; packages/<pkg>/build/ — артефакты сборки, не пакеты.
PYPROJECTS = $(shell find $(SRC_DIR)/packages -name pyproject.toml -not -path '*/build/*')


.PHONY: version
version:
	@echo $(VERSION)


# правит version и пины boba-*==<old> сразу во всех пакетах: врозь их держать нельзя
.PHONY: version-set
version-set:
	@if [ -z "$(VER)" ]; then
	    echo "укажи версию: make version-set VER=X.Y.Z" >&2
	    exit 1
	fi
	if ! echo "$(VER)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$$'; then
	    echo "версия должна быть X.Y.Z или X.Y.Z.devN, а не '$(VER)'" >&2
	    exit 1
	fi
	sed -i -e 's/^version = "$(VERSION)"$$/version = "$(VER)"/' \
	       -e 's/"\(boba-[a-z0-9-]*\(\[[a-z0-9,_-]*\]\)\?\)==$(VERSION)"/"\1==$(VER)"/g' $(PYPROJECTS)
	echo ">>> $(VERSION) -> $(VER) в $(words $(PYPROJECTS)) пакетах"
	$(MAKE) --no-print-directory version-check VERSION=$(VER)


# ловит ручную правку pyproject.toml: разные version или пин boba-* на чужую версию
.PHONY: version-check
version-check:
	@bad=$$(grep -h '^version = ' $(PYPROJECTS) | sort -u | grep -v '^version = "$(VERSION)"$$' || true)
	if [ -n "$$bad" ]; then
	    echo "версии пакетов разошлись с $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	bad=$$(grep -hoE '^[[:space:]]+"boba-[a-z0-9-]+[^"]*"' $(PYPROJECTS) | tr -d '[:blank:]' \
	       | grep -v '^"boba-[a-z0-9-]*\(\[[a-z0-9,_-]*\]\)\?==$(VERSION)"$$' | sort -u || true)
	if [ -n "$$bad" ]; then
	    echo "внутренние зависимости не запинены на $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	echo "version-check: $(VERSION), $(words $(PYPROJECTS)) пакетов — ok"


.PHONY: sources
sources:
	@mkdir -p $(SRC_ARTIFACTS)
	echo "== bubblewrap $(BWRAP_VERSION) =="
	[ -f "$(BWRAP_TARBALL)" ] || curl -fSL $(CURL_OPTS) -o "$(BWRAP_TARBALL)" "$(BWRAP_URL)"
	echo "== libfuse $(LIBFUSE_VERSION) =="
	[ -f "$(LIBFUSE_TARBALL)" ] || curl -fSL $(CURL_OPTS) -o "$(LIBFUSE_TARBALL)" "$(LIBFUSE_URL)"
	echo "== e2fsprogs $(E2FSPROGS_VERSION) =="
	[ -f "$(E2FSPROGS_TARBALL)" ] || curl -fSL $(CURL_OPTS) -o "$(E2FSPROGS_TARBALL)" "$(E2FSPROGS_URL)"
	ls -lh $(SRC_ARTIFACTS)/


.PHONY: deps
deps: uv tessdata fastembed webassets sandbox-rootfs sandbox-site sandbox-python \
      sandbox-data sandbox-workspace
	@du -sh $(DEPS_DIR)/uv $(WEBASSETS_DIR) $(SANDBOX_DIR)/*


.PHONY: uv
uv:
	@mkdir -p $(DEPS_DIR)/uv
	echo "== uv $(UV_VERSION) ($(UV_PLATFORM)) =="
	if [ -f "$(DEPS_DIR)/uv/uv" ]; then
	    echo "  ✓ $(DEPS_DIR)/uv/uv"
	else
	    curl -fSL $(CURL_OPTS) "$(UV_URL)" | tar -xz -C /tmp
	    install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(DEPS_DIR)/uv/uv
	    rm -rf /tmp/uv-$(UV_PLATFORM)
	    $(DEPS_DIR)/uv/uv --version
	fi


.PHONY: tessdata
tessdata:
	@mkdir -p $(DEPS_DIR)/tessdata
	echo "== OCR-модели tesseract: $(TESS_LANGS) =="
	for lang in $(TESS_LANGS); do
	    dest="$(DEPS_DIR)/tessdata/$$lang.traineddata"
	    if [ -f "$$dest" ]; then
	        echo "  ✓ $$dest"
	    else
	        curl -fSL $(CURL_OPTS) -o "$$dest" "$(TESSDATA_REPO)/$$lang.traineddata"
	    fi
	done


# статика фронта: KaTeX (стили формул) и Inter (шрифт UI) целиком локально
.PHONY: webassets
webassets:
	@mkdir -p $(WEBASSETS_DIR)/katex/fonts $(WEBASSETS_DIR)/inter/files
	echo "== katex $(KATEX_VERSION) =="
	if [ -f "$(WEBASSETS_DIR)/katex/katex.min.css" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/katex"
	else
	    tmp=$$(mktemp -d)
	    curl -fSL $(CURL_OPTS) -o "$$tmp/katex.tar.gz" "$(KATEX_URL)"
	    # ttf и woff не нужны: woff2 стоит в src первым и его понимают все браузеры
	    tar -xzf "$$tmp/katex.tar.gz" -C "$$tmp" --wildcards \
	        katex/katex.min.css 'katex/fonts/*.woff2'
	    cp "$$tmp/katex/katex.min.css" "$(WEBASSETS_DIR)/katex/"
	    cp "$$tmp"/katex/fonts/*.woff2 "$(WEBASSETS_DIR)/katex/fonts/"
	    rm -rf "$$tmp"
	fi
	echo "== Inter $(INTER_VERSION): $(INTER_WEIGHTS) =="
	if [ -f "$(WEBASSETS_DIR)/inter/inter.css" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/inter"
	else
	    tmp=$$(mktemp -d)
	    curl -fSL $(CURL_OPTS) -o "$$tmp/inter.tgz" "$(INTER_URL)"
	    tar -xzf "$$tmp/inter.tgz" -C "$$tmp"
	    : > "$(WEBASSETS_DIR)/inter/inter.css"
	    # <вес>.css несёт все подсеты с unicode-range, включая кириллицу
	    for w in $(INTER_WEIGHTS); do
	        cat "$$tmp/package/$$w.css" >> "$(WEBASSETS_DIR)/inter/inter.css"
	        cp "$$tmp"/package/files/inter-*-$$w-normal.woff2 "$(WEBASSETS_DIR)/inter/files/"
	    done
	    rm -rf "$$tmp"
	fi
	du -sh $(WEBASSETS_DIR)/*


# базовый rootfs песочницы: glibc + office-конвертеры + точки монтирования
.PHONY: sandbox-rootfs
sandbox-rootfs:
	@dest="$(SANDBOX_DIR)/rootfs"
	echo "== rootfs песочницы из $(SANDBOX_IMAGE) + утилиты =="
		docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS) $(PROXY_ARGS)                       \
	    --target sandbox-rootfs                            \
	    --tag $(ROOTFS_IMAGE)                              \
	    $(PROGRESS) $(BUILD_CONTEXT)
	rm -rf "$$dest"
	mkdir -p "$$dest"
	cid=$$(docker create $(ROOTFS_IMAGE) /bin/true)
	docker export "$$cid" | tar -x -C "$$dest"
	docker rm "$$cid" >/dev/null
	mkdir -p "$$dest/workspace"


# site-packages песочницы одним каталогом: его монтируют профили в /opt/site.
# version-check первым: иначе разъехавшиеся версии всплывут отказом резолвера uv
.PHONY: sandbox-site
sandbox-site: version-check
	@dest="$(SANDBOX_DIR)/site"
	echo "== site-packages песочницы =="
		docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS) $(PROXY_ARGS)                       \
	    --target sandbox-site                              \
	    --tag $(SITE_IMAGE)                                \
	    $(PROGRESS) $(BUILD_CONTEXT)
	rm -rf "$$dest"
	mkdir -p "$$dest"
	cid=$$(docker create $(SITE_IMAGE) /bin/true)
	docker cp "$$cid:/opt/site/." "$$dest/"
	docker rm "$$cid" >/dev/null


# python-дистрибутив песочницы: тот же, что у приложения (стадия python-dist)
.PHONY: sandbox-python
sandbox-python:
	@dest="$(SANDBOX_DIR)/python"
	echo "== python-дистрибутив песочницы =="
		rm -rf "$$dest"
	mkdir -p "$$dest"
	cid=$$(docker create $(PYTHON_IMAGE) /bin/true)
	docker cp "$$cid:/opt/python/usr/local/." "$$dest/"
	docker rm "$$cid" >/dev/null


# данные песочницы: веса эмбеддера и модели OCR лежат отдельными каталогами
.PHONY: sandbox-data
sandbox-data: tessdata fastembed
	@mkdir -p "$(SANDBOX_DIR)/data"
	echo "== данные песочницы: tessdata + fastembed =="
	rm -rf "$(SANDBOX_DIR)/data/tessdata" "$(SANDBOX_DIR)/data/fastembed"
	cp -a "$(DEPS_DIR)/tessdata" "$(SANDBOX_DIR)/data/tessdata"
	cp -a "$(DEPS_DIR)/fastembed" "$(SANDBOX_DIR)/data/fastembed"
	ln -sf eng.traineddata "$(SANDBOX_DIR)/data/tessdata/rus+eng.traineddata"
	ln -sf rus.traineddata "$(SANDBOX_DIR)/data/tessdata/eng+rus.traineddata"


.PHONY: sandbox-workspace
sandbox-workspace:
	@dest="$(SANDBOX_DIR)/workspace.ext4"
	echo "== шаблон workspace $(SANDBOX_WS_SIZE) =="
		# шаблон делает стадия fuse2fs-build тем же mke2fs, что уедет в релиз:
	# второй способ создания означал бы два разных образа под одним именем
	docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS) $(PROXY_ARGS)                       \
	    --target fuse2fs-build                             \
	    --tag $(FUSE_IMAGE)                                \
	    $(PROGRESS) $(BUILD_CONTEXT)
	mkdir -p "$(SANDBOX_DIR)"
	cid=$$(docker create $(FUSE_IMAGE) /bin/true)
	docker cp "$$cid:/out/sandbox-workspace.ext4" "$$dest"
	docker rm "$$cid" >/dev/null
	ls -lh "$$dest"


.PHONY: fastembed
fastembed: base
	@mkdir -p $(DEPS_DIR)/fastembed
	echo "== веса fastembed: $(EMBED_MODELS) =="
	docker run --rm \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)" \
	  -e EMBED_MODELS="$(EMBED_MODELS)" \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \
	  $(CONF_OPTS) \
	  -v "$(DEPS_DIR)/fastembed":/fastembed \
	  --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    pip3 install --no-cache-dir fastembed
	    for m in $$EMBED_MODELS; do
	      echo ">>> fastembed: $$m"
	      python3 -c "import sys; from fastembed import TextEmbedding;
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/fastembed\");
	        list(e.embed([\"probe\"]))" "$$m" \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed"
	    done
	    chown -R "$$HOST_UID:$$HOST_GID" /fastembed'


.PHONY: base
base: uv
	@if [ "$$(printf '%s\n' "$(REQUIRES_PY)" "$(PYTHON_VERSION)" | sort -V | head -1)" != "$(REQUIRES_PY)" ]; then
	    echo "PYTHON_VERSION=$(PYTHON_VERSION), а пакетам нужен python >=$(REQUIRES_PY)" >&2
	    exit 1
	fi
	docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS) $(PROXY_ARGS)                       \
	    --target base                                      \
	    --tag $(BASE_IMAGE) --tag boba-base:latest         \
	    $(PROGRESS) $(BUILD_CONTEXT)


.PHONY: check-artifacts
check-artifacts:
	@[ -f "$(BWRAP_TARBALL)" ] || {
	    echo "нет исходников bubblewrap — запусти: make sources" >&2; exit 1
	}
	[ -f "$(LIBFUSE_TARBALL)" ] || {
	    echo "нет исходников libfuse — запусти: make sources" >&2; exit 1
	}
	[ -f "$(E2FSPROGS_TARBALL)" ] || {
	    echo "нет исходников e2fsprogs — запусти: make sources" >&2; exit 1
	}
	[ -f "$(DEPS_DIR)/uv/uv" ] || {
	    echo "нет бинаря uv — запусти: make deps" >&2; exit 1
	}
	[ -f "$(DEPS_DIR)/tessdata/eng.traineddata" ] || {
	    echo "нет моделей OCR — запусти: make deps" >&2; exit 1
	}
	[ -n "$$(ls -A "$(DEPS_DIR)/fastembed" 2>/dev/null)" ] || {
	    echo "нет весов fastembed — запусти: make deps" >&2; exit 1
	}
	[ -f "$(WEBASSETS_DIR)/katex/katex.min.css" ] || {
	    echo "нет стилей katex — запусти: make webassets" >&2; exit 1
	}
	[ -f "$(WEBASSETS_DIR)/inter/inter.css" ] || {
	    echo "нет шрифта Inter — запусти: make webassets" >&2; exit 1
	}


.PHONY: build
build: version-check check-artifacts
	@echo ">>> образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR)"
	docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS) $(PROXY_ARGS)                       \
	    --target runtime                                   \
	    --build-arg BOBA_DIR="$(DEST_DIR)"                 \
	    --build-arg BOBA_VERSION="$(VERSION)"              \
	    --tag $(IMAGE) --tag boba:latest                   \
	    $(PROGRESS) $(BUILD_CONTEXT)


.PHONY: test
test: build
	@[ -f "$(TEST_DIR)/bobacheck.py" ] || {
	    echo "нет $(TEST_DIR)/bobacheck.py — файл лежит в репозитории, TEST_DIR=$(TEST_DIR)" >&2
	    exit 1
	}
	echo "== проверка $(RELEASE_NAME) в образе $(IMAGE) =="
	docker run --rm -i \
	  --entrypoint sh $(IMAGE) -c '
	    set -a && . "$(DEST_DIR)/conf/boba.env" && set +a
	    "$$PYTHONHOME/bin/python3" - --names "$(DEST_DIR)/conf/packages.txt"
	  ' < "$(TEST_DIR)/bobacheck.py"

.PHONY: extract
extract:
	@docker image inspect $(RELEASE_IMAGE) >/dev/null 2>&1 || {
	    echo "нет образа $(RELEASE_IMAGE) — собери: make build" >&2; exit 1
	}
	rm -rf "$(OUT)"
	mkdir -p "$(OUT)/image"
	cid=$$(docker create --entrypoint /bin/sh $(RELEASE_IMAGE))
	trap 'docker rm -f "$$cid" >/dev/null 2>&1 || true' EXIT
	docker cp "$$cid:$(DEST_DIR)/." "$(OUT)/image/"
	docker rm "$$cid" >/dev/null
	trap - EXIT

	mkdir -p "$(OUT)/app/$(VERSION)" "$(OUT)/conf" \
	         "$(OUT)/data/workspace" "$(OUT)/data/dumps"
	mv "$(OUT)/image/third"                             "$(OUT)/third"
	mv "$(OUT)/image/sandbox"                           "$(OUT)/sandbox"
	mv "$(OUT)/image/app/$(VERSION)/boba-chainlit"  "$(OUT)/app/$(VERSION)/boba-chainlit"
	mv "$(OUT)/image/app/$(VERSION)/boba-tool"      "$(OUT)/app/$(VERSION)/boba-tool"
	rm -rf "$(OUT)/image"

	# шебанги указывают внутрь образа: переставляем на путь установки
	sed -i "1s|^#!.*python3.*|#!$(INSTALL_DIR)/third/bin/python3|" \
	    "$(OUT)/app/$(VERSION)/boba-chainlit/bin"/*

	# envsubst с явным списком: ${PYTHONHOME} и TOML-интерполяции не трогаются
	export INSTALL_DIR="$(INSTALL_DIR)"
	export BOBA_BASE="$(INSTALL_DIR)" BOBA_VERSION="$(VERSION)"
	envsubst '$${INSTALL_DIR}' \
	    < "$(CONF_DIR)/boba-chainlit.service" > "$(OUT)/conf/boba-chainlit.service"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}' \
	    < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"
	cp "$(CONF_DIR)/config.toml.example" "$(OUT)/conf/config.toml"

	# артефакты песочницы: без них профили [tool.*.sandbox] не поднимутся
	for art in rootfs/bin/sh python/bin/python3 \
	           data/fastembed data/tessdata/eng.traineddata workspace.ext4; do
	    [ -e "$(OUT)/sandbox/$$art" ] || {
	        echo "в релизе нет sandbox/$$art — запусти: make deps" >&2
	        exit 1
	    }
	done
	[ -e "$(OUT)/app/$(VERSION)/boba-tool/liteparse" ] || {
	    echo "в релизе нет app/$(VERSION)/boba-tool/liteparse — запусти: make deps" >&2
	    exit 1
	}
	echo ">>> артефакты песочницы на месте"

	echo ">>> релиз готов: $(OUT)  ($$(du -sh --apparent-size "$(OUT)" 2>/dev/null | cut -f1))"
	echo "    третья сторона: $$(du -sh --apparent-size "$(OUT)/third" 2>/dev/null | cut -f1)"
	echo "    песочница:      $$(du -sh --apparent-size "$(OUT)/sandbox" 2>/dev/null | cut -f1)"
	echo "    версия:         $$(du -sh --apparent-size "$(OUT)/app/$(VERSION)" 2>/dev/null | cut -f1)"
	echo "    установка:      cp -a $(OUT)/. $(INSTALL_DIR)/"


# fastembed не трогаем: веса качаются долго
.PHONY: clean
clean:
	rm -rf $(SRC_ARTIFACTS) $(ARTIFACTS)/build \
	       $(DEPS_DIR)/uv $(DEPS_DIR)/tessdata $(WEBASSETS_DIR) $(SANDBOX_DIR)


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources deps build test
