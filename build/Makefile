# от каталога Makefile, а не от cwd: make -f build/Makefile из корня искал бы
# test/ и conf/ рядом с собой
BASE_DIR        ?= $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SRC_DIR         ?= $(abspath $(BASE_DIR)/..)
# вложенные вызовы: make -f build/Makefile не подставляет его в $(MAKE)
MAKEFILE        := $(BASE_DIR)/Makefile
BUILD_CONTEXT   ?= $(SRC_DIR)
TEST_DIR        ?= $(BASE_DIR)/test
# шаблоны для сборки (make extract, Dockerfile COPY) живут в build/conf
CONF_DIR        := $(BASE_DIR)/conf

# артефакты для сборки: tarball'ы, uv, tessdata, fastembed, webassets, wheels, sandbox
DEPS_DIR        ?= $(BASE_DIR)/src
# результаты make extract — в корне репо
RELEASE_DIR     := $(SRC_DIR)/release


.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL           := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c


# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION_FILE    := $(SRC_DIR)/packages/core/boba-toolkit/pyproject.toml
VERSION         := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))
BOBA_TAG        ?= $(VERSION)
REQUIRES_PY     := $(shell sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' $(VERSION_FILE))

# релиз для make extract
RELEASE_NAME    := boba-$(VERSION)
# корень установки на сервере: в него уедет дерево из make extract
INSTALL_DIR     ?= /srv/boba
# тот же корень внутри образа; версия живёт в app/<version>, не в нём
DEST_DIR        ?= /app/boba

# финальный runtime-образ
IMAGE           := boba:$(BOBA_TAG)
# из какого образа make extract достаёт портативную папку и куда её кладёт
RELEASE_IMAGE   ?= $(IMAGE)
OUT             ?= $(RELEASE_DIR)/$(RELEASE_NAME)

# режим sec.sh: all, sast, sca или rules
MODE            ?= all

PYTHON_VERSION  ?= 3.11
# чистая рантайм-база (только glibc), от неё строится финальный образ runtime
GLIBC_IMAGE     ?= dmp/glibc:2.28
# среда сборки: glibc 2.28 + gcc + python-дистрибутив
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
# базовый rootfs песочницы: стадия sandbox-rootfs (glibc + office + точки)
ROOTFS_IMAGE    := boba-sandbox-rootfs:$(PYTHON_VERSION)
# site-packages песочницы одним каталогом: стадия sandbox-site
SITE_IMAGE      := boba-sandbox-site:$(PYTHON_VERSION)
# шаблон workspace и fuse2fs: стадия fuse2fs-build
FUSE_IMAGE      := boba-fuse2fs:$(PYTHON_VERSION)
# bwrap: стадия bwrap-build
BWRAP_IMAGE     := boba-bwrap:$(PYTHON_VERSION)
# артефакты песочницы: rootfs, python, site и данные подключаются профилями
SANDBOX_DIR     := $(DEPS_DIR)/sandbox
# нативное окружение liteparse: office/растр/pdf он конвертирует этими
# программами. Убрать libreoffice — doc перестанет читать docx/xlsx/pptx
LITEPARSE_PACKAGES?= imagemagick libreoffice-writer libreoffice-calc libreoffice-impress ghostscript fonts-dejavu-core
# тег с версией python: иначе deps соберутся на boba-base от прошлой сборки
BASE_IMAGE      := boba-base:$(PYTHON_VERSION)

# OCR-модели tesseract
TESS_LANGS      ?= eng rus osd
TESSDATA_REPO   ?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed
EMBED_MODELS    ?= intfloat/multilingual-e5-large

# фронт chainlit тянет шрифт и стили формул с CDN; в закрытом контуре их
# кладут в data/public/vendor, а ссылки в index.html переписывает Dockerfile
WEBASSETS_DIR   := $(DEPS_DIR)/webassets
# ассеты фронта в дереве исходников: отсюда их берут и образ, и make dev
ASSETS_DIR      := $(SRC_DIR)/packages/agents/boba-chainlit/assets
ASSETS_VENDOR   := $(ASSETS_DIR)/public/vendor
KATEX_VERSION   ?= 0.16.22
KATEX_URL       ?= https://github.com/KaTeX/KaTeX/releases/download/v$(KATEX_VERSION)/katex.tar.gz
INTER_VERSION   ?= 5.3.0
INTER_URL       ?= https://registry.npmjs.org/@fontsource/inter/-/inter-$(INTER_VERSION).tgz
# начертания Inter, которые запрашивает фронт
INTER_WEIGHTS   ?= 400 500 700
MERMAID_VERSION ?= 11.16.1
MERMAID_URL     ?= https://registry.npmjs.org/mermaid/-/mermaid-$(MERMAID_VERSION).tgz

# готовый бинарь; для внутреннего mirror переопредели UV_URL
UV_VERSION      ?= 0.11.21
UV_PLATFORM     ?= x86_64-unknown-linux-musl
UV_URL          ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# bubblewrap: собирается стадией bwrap-build
BWRAP_VERSION   ?= 0.11.2
BWRAP_URL       ?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
BWRAP_TARBALL   := $(DEPS_DIR)/bubblewrap-$(BWRAP_VERSION).tar.xz

# fuse2fs + libfuse: собираются стадией fuse2fs-build.
# 3.12.0 — последний релиз, которому хватает meson 0.49 из apt Astra 2.12
# (3.13+ требуют >= 0.50); релизы до 3.15 включительно выложены как .tar.xz
LIBFUSE_VERSION ?= 3.12.0
LIBFUSE_URL     ?= https://github.com/libfuse/libfuse/releases/download/fuse-$(LIBFUSE_VERSION)/fuse-$(LIBFUSE_VERSION).tar.xz
LIBFUSE_TARBALL := $(DEPS_DIR)/fuse-$(LIBFUSE_VERSION).tar.xz
E2FSPROGS_VERSION?= 1.47.2
E2FSPROGS_URL   ?= https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v$(E2FSPROGS_VERSION)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
E2FSPROGS_TARBALL:= $(DEPS_DIR)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
# размер шаблонного workspace-образа: лимит диска на тред
SANDBOX_WS_SIZE ?= 1G

# сбросить ли кэш сборки: docker build --no-cache
NO_CACHE        ?= 0
NO_CACHE_OPT    := $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# --progress есть только у buildkit; на старом docker флага нет — не подставляем
PROGRESS        ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: CA внутреннего TLS (nexus). Кладётся в conf/ рядом с pip.conf и uv.toml
CA_CHAIN        := $(firstword $(wildcard $(CONF_DIR)/ca-chain.crt) $(wildcard $(BASE_DIR)/ca-chain.crt))
# curl на хосте: прокси берёт из env, CA — отсюда
CURL_OPTS       := $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")

# nexus в docker run: каталогом, поштучный bind-mount ломается о /etc/pip.conf,
# который стадия base уже создала. CA берётся из conf/, как и в Dockerfile
CONF_OPTS       := -v "$(CONF_DIR)":/tmp/conf:ro                                             \
    $(if $(wildcard $(CONF_DIR)/pip.conf),-e PIP_CONFIG_FILE=/tmp/conf/pip.conf)             \
    $(if $(wildcard $(CONF_DIR)/uv.toml),-e UV_CONFIG_FILE=/tmp/conf/uv.toml)                \
    $(if $(wildcard $(CONF_DIR)/ca-chain.crt),-e REQUESTS_CA_BUNDLE=/tmp/conf/ca-chain.crt   \
    -e CURL_CA_BUNDLE=/tmp/conf/ca-chain.crt)

# прокси в docker build: apt/pip/uv внутри стадий ходят в nexus через него
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))

# сборка одной стадии: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT)   \
    $(DOCKER_ARGS) $(PROXY_ARGS) $(3)                    \
    --target $(1)                                        \
    --tag $(2)                                           \
    $(PROGRESS) $(BUILD_CONTEXT)
endef

# выемка артефакта из образа: $(1) — образ, $(2) — путь внутри, $(3) — куда
define image-copy
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# общие build-arg: образы тулчейна и версии
DOCKER_ARGS     = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)   \
    --build-arg GCC_IMAGE=$(GCC_IMAGE)                     \
    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)               \
    --build-arg PYTHON_VERSION=$(PYTHON_VERSION)           \
    --build-arg BWRAP_VERSION=$(BWRAP_VERSION)             \
    --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)         \
    --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION)     \
    --build-arg SANDBOX_WS_SIZE=$(SANDBOX_WS_SIZE)         \
    --build-arg LITEPARSE_PACKAGES="$(LITEPARSE_PACKAGES)"


.PHONY: help
help:
	@echo "Сборка boba $(VERSION) из дерева пакетов $(SRC_DIR) -> $(IMAGE)"
	echo ""
	echo "  make sources   исходники, собираемые внутри Dockerfile -> $(DEPS_DIR)"
	echo "  make deps      бинарные зависимости -> $(DEPS_DIR)/"
	echo "                 {uv,tessdata,fastembed,webassets,"
	echo "                  sandbox/{rootfs,site,workspace.ext4,"
	echo "                           third/{python,tessdata,fastembed}}}"
	echo "                 цели по отдельности: uv, tessdata, fastembed,"
	echo "                 webassets, sandbox-rootfs, sandbox-site, sandbox-python,"
	echo "                 sandbox-bin, sandbox-data, sandbox-workspace"
	echo "  make build     собрать образ $(IMAGE)"
	echo "  make test      проверить релиз внутри образа"
	echo "  make dev       рантайм в $(SRC_DIR)/compose: то же дерево из build/src,"
	echo "                 без образа — им пользуются и отладка, и docker compose"
	echo "  make release   дерево установки из готового образа:"
	echo "                 RELEASE_IMAGE=$(RELEASE_IMAGE), OUT=$(OUT)"
	echo "                 {conf,data,app_root,code/{app,sandbox},deps/{app,sandbox},"
	echo "                  sandbox,third}, ставится в INSTALL_DIR=$(INSTALL_DIR)"
	echo "  make sec       ruff, bandit, semgrep и pip-audit (MODE=all|sast|sca|rules)"
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
	echo "      KATEX_URL, INTER_URL, MERMAID_URL"
	echo ""
	echo "  Переменные: INSTALL_DIR=$(INSTALL_DIR), DEST_DIR=$(DEST_DIR),"
	echo "              BOBA_TAG=<тег>, NO_CACHE=1,"
	echo "              PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE=$(GLIBC_IMAGE),"
	echo "              GCC_IMAGE=$(GCC_IMAGE), PYTHON_IMAGE=$(PYTHON_IMAGE)"
	echo "  Пакетам нужен python >=$(REQUIRES_PY)"


# значения, по которым отлаживают сборку: печатаются при вызове цели
DEBUG_VARS      := BASE_DIR SRC_DIR CONF_DIR DEPS_DIR RELEASE_DIR TEST_DIR   \
    VERSION RELEASE_NAME IMAGE RELEASE_IMAGE OUT DEST_DIR INSTALL_DIR        \
    BASE_IMAGE GLIBC_IMAGE GCC_IMAGE PYTHON_IMAGE REQUIRES_PY NO_CACHE       \
    UV_URL CA_CHAIN

.PHONY: debug
debug:
	@$(foreach v,$(DEBUG_VARS),printf '%-14s = %s\n' "$(v)" "$($(v))";)


# Все pyproject.toml пакетов; packages/<pkg>/build/ — артефакты сборки, не пакеты.
PYPROJECTS      = $(shell find $(SRC_DIR)/packages -name pyproject.toml -not -path '*/build/*')


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
	sed -i -e 's/^version = "$(VERSION)"$$/version = "$(VER)"/'   \
	       -e 's/"\(boba-[a-z0-9-]*\(\[[a-z0-9,_-]*\]\)\?\)==$(VERSION)"/"\1==$(VER)"/g' $(PYPROJECTS)
	echo ">>> $(VERSION) -> $(VER) в $(words $(PYPROJECTS)) пакетах"
	$(MAKE) -f $(MAKEFILE) --no-print-directory version-check VERSION=$(VER)


# ловит ручную правку pyproject.toml: разные version или пин boba-* на чужую версию
.PHONY: version-check
version-check:
	@bad=$$(grep -h '^version = ' $(PYPROJECTS) | sort -u | grep -v '^version = "$(VERSION)"$$' || true)
	if [ -n "$$bad" ]; then
	    echo "версии пакетов разошлись с $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	bad=$$(grep -hoE '^[[:space:]]+"boba-[a-z0-9-]+[^"]*"' $(PYPROJECTS) | tr -d '[:blank:]'   \
	       | grep -v '^"boba-[a-z0-9-]*\(\[[a-z0-9,_-]*\]\)\?==$(VERSION)"$$' | sort -u || true)
	if [ -n "$$bad" ]; then
	    echo "внутренние зависимости не запинены на $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	echo "version-check: $(VERSION), $(words $(PYPROJECTS)) пакетов — ok"


.PHONY: sources
sources:
	@mkdir -p $(DEPS_DIR)
	echo "== bubblewrap $(BWRAP_VERSION) =="
	[ -f "$(BWRAP_TARBALL)" ] || curl -fSL $(CURL_OPTS) -o "$(BWRAP_TARBALL)" "$(BWRAP_URL)"
	echo "== libfuse $(LIBFUSE_VERSION) =="
	[ -f "$(LIBFUSE_TARBALL)" ] || curl -fSL $(CURL_OPTS) -o "$(LIBFUSE_TARBALL)" "$(LIBFUSE_URL)"
	echo "== e2fsprogs $(E2FSPROGS_VERSION) =="
	[ -f "$(E2FSPROGS_TARBALL)" ] || curl -fSL $(CURL_OPTS) -o "$(E2FSPROGS_TARBALL)" "$(E2FSPROGS_URL)"
	ls -lh $(DEPS_DIR)/


.PHONY: deps
deps: uv tessdata fastembed webassets sandbox-rootfs sandbox-site sandbox-python   \
      sandbox-bin sandbox-data sandbox-workspace
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


# статика фронта: KaTeX (формулы), Inter (шрифт UI), mermaid (диаграммы) локально
.PHONY: webassets
webassets:
	@mkdir -p $(WEBASSETS_DIR)/katex/fonts $(WEBASSETS_DIR)/inter/files $(WEBASSETS_DIR)/mermaid
	echo "== katex $(KATEX_VERSION) =="
	if [ -f "$(WEBASSETS_DIR)/katex/katex.min.css" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/katex"
	else
	    tmp=$$(mktemp -d)
	    curl -fSL $(CURL_OPTS) -o "$$tmp/katex.tar.gz" "$(KATEX_URL)"
	    # ttf и woff не нужны: woff2 стоит в src первым и его понимают все браузеры
	    tar -xzf "$$tmp/katex.tar.gz" -C "$$tmp" --wildcards   \
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
	echo "== mermaid $(MERMAID_VERSION) =="
	if [ -f "$(WEBASSETS_DIR)/mermaid/mermaid.min.js" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/mermaid"
	else
	    tmp=$$(mktemp -d)
	    curl -fSL $(CURL_OPTS) -o "$$tmp/mermaid.tgz" "$(MERMAID_URL)"
	    # из пакета нужен один IIFE-бандл: он выставляет globalThis.mermaid
	    tar -xzf "$$tmp/mermaid.tgz" -C "$$tmp" package/dist/mermaid.min.js
	    cp "$$tmp/package/dist/mermaid.min.js" "$(WEBASSETS_DIR)/mermaid/"
	    rm -rf "$$tmp"
	fi
	# assets/public/theme.json просит /public/vendor/inter/inter.css — путь обязан
	# существовать и при запуске из IDE, где public берётся из дерева исходников.
	# В образ тот же каталог кладёт COPY build/src/webassets (стадия runtime)
	rm -rf "$(ASSETS_VENDOR)"
	mkdir -p "$(dir $(ASSETS_VENDOR))"
	cp -a "$(WEBASSETS_DIR)" "$(ASSETS_VENDOR)"
	du -sh $(WEBASSETS_DIR)/*


# базовый rootfs песочницы: glibc + office-конвертеры + точки монтирования
.PHONY: sandbox-rootfs
sandbox-rootfs:
	@dest="$(SANDBOX_DIR)/rootfs"
	echo "== rootfs песочницы из $(GLIBC_IMAGE) + утилиты =="
	$(call docker-stage,sandbox-rootfs,$(ROOTFS_IMAGE))
	rm -rf "$$dest"
	mkdir -p "$$dest"
	cid=$$(docker create $(ROOTFS_IMAGE) /bin/true)
	docker export "$$cid" | tar -x -C "$$dest"
	docker rm "$$cid" >/dev/null
	mkdir -p "$$dest/workspace"
	# в образе krb монтируется в свой же путь ($(DEST_DIR)/conf/krb) — точка есть
	# в rootfs. При запуске из IDE путь хостовый, точку под него создаём здесь:
	# bwrap не заводит её сам на read-only корне
	mkdir -p "$$dest$(SRC_DIR)/compose/conf/krb"


# site-packages песочницы одним каталогом: его монтируют профили в /opt/site.
# version-check первым: иначе разъехавшиеся версии всплывут отказом резолвера uv
.PHONY: sandbox-site
sandbox-site: version-check sandbox-names
	@dest="$(SANDBOX_DIR)/site"
	echo "== site-packages песочницы =="
	$(call docker-stage,sandbox-site,$(SITE_IMAGE))
	rm -rf "$$dest"
	mkdir -p "$$dest"
	$(call image-copy,$(SITE_IMAGE),/opt/site/.,$$dest/)


# python-дистрибутив песочницы: тот же, что у приложения (стадия python-dist),
# лежит в third/ как монтируемый артефакт наравне с tessdata и fastembed
.PHONY: sandbox-python
sandbox-python: base
	@dest="$(SANDBOX_DIR)/third/python"
	echo "== python-дистрибутив песочницы =="
	rm -rf "$$dest"
	mkdir -p "$$dest"
	$(call image-copy,$(PYTHON_IMAGE),/opt/python/usr/local/.,$$dest/)

	# артефакт монтируется в /usr/local и перекрывает lib64 из rootfs,
	# поэтому свежий libstdc++ едет вместе с ним: onnxruntime требует CXXABI 1.3.11
	mkdir -p "$$dest/lib64"
	$(call image-copy,$(BASE_IMAGE),/usr/local/lib64/.,$$dest/lib64/)


# состав песочницы объявляют сами пакеты: у кого есть extra payload, тот и едет
# внутрь вместе с ним (парсеры, эмбеддер, plotly)
.PHONY: sandbox-names
sandbox-names:
	@dest="$(DEPS_DIR)/names-sandbox.txt"
	echo "== пакеты песочницы =="
	mkdir -p "$(DEPS_DIR)"
	: > "$$dest"

	for project in $(PYPROJECTS); do
	    extras=$$(sed -n '/^\[project.optional-dependencies\]/,/^\[/p' "$$project")

	    if ! echo "$$extras" | grep -q '^payload'; then
	        continue
	    fi

	    name=$$(sed -n 's/^name = "\(.*\)"/\1/p' "$$project" | head -1)
	    echo "$$name[payload]" >> "$$dest"
	done

	sort -o "$$dest" "$$dest"
	sed 's/^/    /' "$$dest"


# bwrap и fuse2fs рядом с остальными артефактами: без них dev-дерево
# пришлось бы доставать из релизного образа
.PHONY: sandbox-bin
sandbox-bin:
	@dest="$(SANDBOX_DIR)/third/bin"
	echo "== bwrap и fuse2fs =="
	$(call docker-stage,bwrap-build,$(BWRAP_IMAGE))
	$(call docker-stage,fuse2fs-build,$(FUSE_IMAGE))
	mkdir -p "$$dest"
	$(call image-copy,$(BWRAP_IMAGE),/out/bwrap,$$dest/bwrap)
	$(call image-copy,$(FUSE_IMAGE),/out/fuse2fs,$$dest/fuse2fs)
	chmod 0755 "$$dest/bwrap" "$$dest/fuse2fs"
	"$$dest/bwrap" --version
	"$$dest/fuse2fs" -V


# артефакты песочницы: python, веса эмбеддера и модели OCR в одном third/
.PHONY: sandbox-data
sandbox-data: tessdata fastembed
	@mkdir -p "$(SANDBOX_DIR)/third"
	echo "== данные песочницы: tessdata + fastembed =="
	rm -rf "$(SANDBOX_DIR)/third/tessdata" "$(SANDBOX_DIR)/third/fastembed"
	cp -a "$(DEPS_DIR)/tessdata" "$(SANDBOX_DIR)/third/tessdata"
	cp -a "$(DEPS_DIR)/fastembed" "$(SANDBOX_DIR)/third/fastembed"
	ln -sf eng.traineddata "$(SANDBOX_DIR)/third/tessdata/rus+eng.traineddata"
	ln -sf rus.traineddata "$(SANDBOX_DIR)/third/tessdata/eng+rus.traineddata"


.PHONY: sandbox-workspace
sandbox-workspace:
	@dest="$(SANDBOX_DIR)/workspace.ext4"
	# шаблон делает стадия fuse2fs-build тем же mke2fs, что уедет в релиз:
	# второй способ создания означал бы два разных образа под одним именем
	echo "== шаблон workspace $(SANDBOX_WS_SIZE) =="
	$(call docker-stage,fuse2fs-build,$(FUSE_IMAGE))
	mkdir -p "$(SANDBOX_DIR)"
	$(call image-copy,$(FUSE_IMAGE),/out/sandbox-workspace.ext4,$$dest)
	ls -lh "$$dest"


.PHONY: fastembed
fastembed: base
	@mkdir -p $(DEPS_DIR)/fastembed
	echo "== веса fastembed: $(EMBED_MODELS) =="
	docker run --rm                                                                       \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                     \
	  -e EMBED_MODELS="$(EMBED_MODELS)"                                                   \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy   \
	  $(CONF_OPTS)                                                                        \
	  -v "$(DEPS_DIR)/fastembed":/fastembed                                               \
	  --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    pip3 install --no-cache-dir fastembed
	    for m in $$EMBED_MODELS; do
	      echo ">>> fastembed: $$m"
	      python3 -c "import sys; from fastembed import TextEmbedding;
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/fastembed\");
	        list(e.embed([\"probe\"]))" "$$m"   \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed"
	    done
	    chown -R "$$HOST_UID:$$HOST_GID" /fastembed'


.PHONY: base
base: uv
	@if [ "$$(printf '%s\n' "$(REQUIRES_PY)" "$(PYTHON_VERSION)" | sort -V | head -1)" != "$(REQUIRES_PY)" ]; then
	    echo "PYTHON_VERSION=$(PYTHON_VERSION), а пакетам нужен python >=$(REQUIRES_PY)" >&2
	    exit 1
	fi
	$(call docker-stage,base,$(BASE_IMAGE),--tag boba-base:latest)


# что должно лежать в build/src до сборки образа: путь:цель, которая его кладёт
BUILD_INPUTS    :=                                   \
    $(BWRAP_TARBALL):sources                         \
    $(LIBFUSE_TARBALL):sources                       \
    $(E2FSPROGS_TARBALL):sources                     \
    $(DEPS_DIR)/uv/uv:deps                           \
    $(DEPS_DIR)/tessdata/eng.traineddata:deps        \
    $(DEPS_DIR)/fastembed:deps                       \
    $(WEBASSETS_DIR)/katex/katex.min.css:webassets   \
    $(WEBASSETS_DIR)/inter/inter.css:webassets       \
    $(DEPS_DIR)/names-sandbox.txt:sandbox-names

.PHONY: check-artifacts
check-artifacts:
	@for item in $(BUILD_INPUTS); do
	    path="$${item%%:*}"
	    goal="$${item##*:}"

	    if [ ! -e "$$path" ]; then
	        echo "нет $$path — запусти: make $$goal" >&2
	        exit 1
	    fi

	    if [ -d "$$path" ] && [ -z "$$(ls -A "$$path")" ]; then
	        echo "пуст $$path — запусти: make $$goal" >&2
	        exit 1
	    fi
	done


.PHONY: build
build: version-check check-artifacts
	@echo ">>> образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR)"
	$(call docker-stage,runtime,$(IMAGE),--tag boba:latest   \
	    --build-arg BOBA_DIR="$(DEST_DIR)" --build-arg BOBA_VERSION="$(VERSION)")


.PHONY: test
test: build
	@[ -f "$(TEST_DIR)/bobacheck.py" ] || {
	    echo "нет $(TEST_DIR)/bobacheck.py — файл лежит в репозитории, TEST_DIR=$(TEST_DIR)" >&2
	    exit 1
	}
	echo "== проверка $(RELEASE_NAME) в образе $(IMAGE) =="
	# boba.env в образе нет: окружение даёт тот, кто запускает процесс.
	# тест берёт тот же шаблон, что уедет в compose и systemd
	env_file=$$(mktemp)
	trap 'rm -f "$$env_file"' EXIT
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}' < "$(CONF_DIR)/boba.env"   \
	    | grep -v '^$$' > "$$env_file"

	docker run --rm -i --env-file "$$env_file"   \
	  --entrypoint sh $(IMAGE) -c '
	    "$$PYTHONHOME/bin/python3" - --names "$(DEST_DIR)/deps/app/packages.txt"
	  ' < "$(TEST_DIR)/bobacheck.py"

# что обязано лежать в дереве установки: путь:цель, которая его кладёт
RELEASE_PARTS   :=                                \
    sandbox/rootfs/bin/sh:deps                    \
    sandbox/third/python/bin/python3:deps         \
    sandbox/third/fastembed:deps                  \
    sandbox/third/tessdata/eng.traineddata:deps   \
    sandbox/workspace.ext4:deps                   \
    third/bin/bwrap:deps                          \
    third/bin/fuse2fs:deps                        \
    code/app/bin:build                            \
    code/sandbox/boba:build                       \
    deps/app:build                                \
    deps/sandbox:build                            \
    app_root/public:build

.PHONY: release
release:
	@docker image inspect $(RELEASE_IMAGE) > /dev/null 2>&1 || {
	    echo "нет образа $(RELEASE_IMAGE) — собери: make build" >&2
	    exit 1
	}

	rm -rf "$(OUT)"
	mkdir -p "$(OUT)"
	cid=$$(docker create --entrypoint /bin/sh $(RELEASE_IMAGE))
	trap 'docker rm -f "$$cid" > /dev/null 2>&1 || true' EXIT
	docker cp "$$cid:$(DEST_DIR)/." "$(OUT)/"
	docker rm "$$cid" > /dev/null
	trap - EXIT

	$(MAKE) -f $(MAKEFILE) --no-print-directory install-tree OUT="$(OUT)" BASE="$(INSTALL_DIR)"

	# стабильный путь для внешних ссылок: они не знают версию
	ln -sfn "$(RELEASE_NAME)" "$(RELEASE_DIR)/current"

	echo ">>> релиз готов: $(OUT)  ($$(du -sh --apparent-size "$(OUT)" 2>/dev/null | cut -f1))"
	echo "    свой код:       $$(du -sh --apparent-size "$(OUT)/code" 2>/dev/null | cut -f1)"
	echo "    зависимости:    $$(du -sh --apparent-size "$(OUT)/deps" 2>/dev/null | cut -f1)"
	echo "    песочница:      $$(du -sh --apparent-size "$(OUT)/sandbox" 2>/dev/null | cut -f1)"
	echo "    установка:      cp -a $(OUT)/. $(INSTALL_DIR)/"


# рабочее дерево для отладки и docker compose: та же раскладка, что у релиза,
# но собирается из build/src, а не из образа — образ для отладки не нужен
.PHONY: dev
dev: check-artifacts
	@out="$(SRC_DIR)/compose"
	echo "== рантайм в $$out =="
	mkdir -p "$$out/conf" "$$out/data/workspace" "$$out/data/tool-logs" "$$out/data/dump"

	# песочница жёсткими ссылками: гигабайты не копируются, файлы настоящие
	rm -rf "$$out/sandbox"
	cp -al "$(SANDBOX_DIR)" "$$out/sandbox"
	mkdir -p "$$out/sandbox/rootfs$$out/conf/krb"

	mkdir -p "$$out/third"
	rm -rf "$$out/third/bin"
	cp -al "$(SANDBOX_DIR)/third/bin" "$$out/third/bin"

	# зависимости песочницы: код инструментов приходит биндом в /usr/src,
	# поэтому .pth ведёт туда же и в отладке, и в релизе
	rm -rf "$$out/deps"
	mkdir -p "$$out/deps"
	cp -al "$(SANDBOX_DIR)/site" "$$out/deps/sandbox"
	$(MAKE) -f $(MAKEFILE) --no-print-directory dev-pth OUT="$$out"

	# ассеты фронта: тот же набор, что уходит в образ
	rm -rf "$$out/app_root"
	mkdir -p "$$out/app_root/.chainlit" "$$out/app_root/.files"
	cp -a "$(ASSETS_DIR)/chainlit/config.toml" "$$out/app_root/.chainlit/"
	cp -a "$(ASSETS_DIR)/chainlit/translations" "$$out/app_root/.chainlit/"
	cp -a "$(ASSETS_DIR)/chainlit/chainlit.md" "$$out/app_root/"
	cp -a "$(ASSETS_DIR)/public" "$$out/app_root/public"

	# окружение процесса в контейнере: пути внутри него, а не на хосте
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}'   \
	    < "$(CONF_DIR)/boba.env" > "$$out/conf/boba.env"
	sed -i 's|^BOBA_CGROUP_BASE=.*|BOBA_CGROUP_BASE=/cgroup/boba-sandbox|' "$$out/conf/boba.env"

	if [ ! -f "$$out/conf/config.toml" ]; then
	    cp "$(CONF_DIR)/config.toml.example" "$$out/conf/config.toml"
	    echo "    conf/config.toml создан из шаблона — впиши секцию [site]"
	fi

	# umask каталога сборки даёт g+w, а TrustedBinaries такой third/bin
	# не примет — то же самое делает install-tree для релиза
	chmod go-w "$$out"
	chmod -R go-w "$$out/third" "$$out/sandbox" "$$out/deps"

	echo ">>> дерево готово: $$out"
	echo "    debug:  BOBA_BASE=$$out BOBA_APP=$(SRC_DIR)/packages"
	echo "    compose: cd $$out && docker compose up -d"


# список путей исходников для sys.path песочницы; в релизе тот же файл
# содержит один /usr/src, потому что код там уже плоский
.PHONY: dev-pth
dev-pth:
	@pth="$(OUT)/deps/sandbox/boba-src.pth"
	: > "$$pth"
	for project in $$(find "$(SRC_DIR)/packages" -name pyproject.toml -not -path '*/build/*'); do
	    src="$$(dirname "$$project")/src"

	    if [ ! -d "$$src" ]; then
	        continue
	    fi

	    echo "/usr/src/$${src#$(SRC_DIR)/packages/}" >> "$$pth"
	done
	echo "    boba-src.pth: $$(wc -l < "$$pth") путей"


# общая часть release и любой установки: права, шебанги, conf, проверки
.PHONY: install-tree
install-tree:
	@mkdir -p "$(OUT)/data/workspace" "$(OUT)/data/tool-logs" "$(OUT)/data/dump"

	# krb монтируется в песочницу своим же путём: точка под него зависит от
	# BOBA_BASE, поэтому создаётся здесь, а не в образе
	mkdir -p "$(OUT)/sandbox/rootfs$(BASE)/conf/krb"

	# шебанги указывают внутрь образа: переставляем на путь установки
	sed -i "1s|^#!.*python3.*|#!$(BASE)/third/bin/python3|" "$(OUT)/code/app/bin"/*

	# отсюда берутся bwrap и fuse2fs: групповая или чужая запись на пути до
	# них — отказ TrustedBinaries, а umask каталога сборки даёт как раз g+w
	chmod go-w "$(OUT)"
	chmod -R go-w "$(OUT)/third" "$(OUT)/sandbox"

	# envsubst с явным списком: $${PYTHONHOME} и TOML-интерполяции не трогаются
	export BOBA_BASE="$(BASE)" BOBA_VERSION="$(VERSION)" INSTALL_DIR="$(BASE)"
	envsubst '$${INSTALL_DIR}'   \
	    < "$(CONF_DIR)/boba-chainlit.service" > "$(OUT)/boba-chainlit.service"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}'   \
	    < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"

	if [ ! -f "$(OUT)/conf/config.toml" ]; then
	    cp "$(CONF_DIR)/config.toml.example" "$(OUT)/conf/config.toml"
	fi

	$(MAKE) -f $(MAKEFILE) --no-print-directory tree-check OUT="$(OUT)"


# проверки готового дерева: без них приложение падало бы уже на сервере
.PHONY: tree-check
tree-check:
	@for item in $(RELEASE_PARTS); do
	    path="$${item%%:*}"
	    goal="$${item##*:}"

	    if [ ! -e "$(OUT)/$$path" ]; then
	        echo "в дереве нет $$path — запусти: make $$goal" >&2
	        exit 1
	    fi
	done

	# то же, что проверит TrustedBinaries на старте приложения
	writable=$$(find "$(OUT)/third/bin" -maxdepth 1 -type f -perm /022 -print -quit)
	if [ -n "$$writable" ]; then
	    echo "third/bin доступен на запись посторонним ($$writable)" >&2
	    exit 1
	fi

	echo ">>> артефакты на месте"


# fastembed и wheels не трогаем: веса качаются долго, колеса — кэш
.PHONY: clean
clean:
	rm -rf $(RELEASE_DIR)                                                        \
	       $(DEPS_DIR)/uv $(DEPS_DIR)/tessdata $(WEBASSETS_DIR) $(SANDBOX_DIR)   \
	       $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL)


# SAST и SCA: сканеры ставятся изолированно через uvx, правила semgrep лежат
# в conf/semgrep (обновляются sec.sh rules)
.PHONY: sec
sec:
	@"$(SRC_DIR)/sec.sh" $(MODE)


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources deps build test
