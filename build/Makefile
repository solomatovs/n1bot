BASE_DIR      	?= $(abspath .)
SRC_DIR       	?= $(abspath ..)
ARTIFACTS     	?= $(BASE_DIR)/artifacts
BUILD_CONTEXT 	?= $(SRC_DIR)
TEST_DIR		?= $(BASE_DIR)/test


.DEFAULT_GOAL := help


# тег образа: git-тег (если HEAD ровно на теге) либо короткий sha, плюс -dirty на грязном дереве
GIT_TAG := $(shell git -C $(SRC_DIR) describe --tags --exact-match 2>/dev/null)
GIT_SHA := $(shell git -C $(SRC_DIR) rev-parse --short HEAD 2>/dev/null)
DIRTY   := $(shell test -n "$$(git -C $(SRC_DIR) status --porcelain 2>/dev/null)" && echo -dirty)

ifeq ($(strip $(BOBA_TAG)),)
  ifneq ($(strip $(GIT_TAG)),)
    BOBA_TAG := $(GIT_TAG)$(DIRTY)
  else
    BOBA_TAG := $(GIT_SHA)$(DIRTY)
  endif
endif


# Единая lockstep-версия всех пакетов — статикой в каждом packages/**/pyproject.toml.
# Читаем из корневого пакета (boba-patterns), меняем через `make version-set VER=X.Y.Z`.
VERSION_FILE := $(SRC_DIR)/packages/core/boba-patterns/pyproject.toml
VERSION      := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))


# версия python — берётся из образа-дистрибутива dmp/python
PYTHON_VERSION	?= 3.11
# тулчейн gcc — рантайм-база (glibc + gcc8 + libstdc++)
GCC_IMAGE    	?= dmp/gcc:8.5.0
# python-дистрибутив (дерево /opt/python), накатывается поверх gcc
PYTHON_IMAGE 	?= dmp/python:$(PYTHON_VERSION)-dist
# выполнять ли сброс кэша: docker build --no-cache
NO_CACHE		?= 0

# OCR-модели tesseract (скачиваются в make sources)
TESS_LANGS    	?= eng rus osd
TESSDATA_REPO 	?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed (скачиваются в make fastembed)
EMBED_MODELS  	?= intfloat/multilingual-e5-large

# uv — готовый статический бинарь (НЕ компилируется, в отличие от wheels).
# Платформа = выбор релиза под arch+libc: musl = статический, без зависимости от glibc.
# Для внутреннего mirror достаточно переопределить UV_URL.
UV_VERSION  	?= 0.11.21
UV_PLATFORM 	?= x86_64-unknown-linux-musl
UV_URL      	?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# базовое название для образа
IMAGE			:= boba:$(BOBA_TAG)
NO_CACHE_TEXT	:= $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# закрытый контур: подхватываем ca-chain.crt, если положили в artifacts
CA_CHAIN := $(wildcard $(ARTIFACTS)/ca-chain.crt)


.PHONY: help
help:
	@echo "make (тег образа: $(BOBA_TAG)):"
	@echo "  make all        собрать всё: sources base wheels fastembed build test"
	@echo "  make sources    скачать OCR-модели tesseract и бинарь uv (нужен инет)"
	@echo "  make base       собрать базовый образ boba-base ($(GCC_IMAGE) + $(PYTHON_IMAGE))"
	@echo "  make wheels     пересобрать offline-wheels внутри boba-base по ../packages"
	@echo "  make fastembed  скачать embedding-веса в artifacts/fastembed (нужны base+wheels+инет; ДО build)"
	@echo "  make build      собрать runtime $(IMAGE) (+ boba:latest), offline из wheels"
	@echo "  make test       проверить образ boba:latest: python, uv, пакеты, OCR, инструменты"
	@echo "  make dist       собрать wheel'ы всех пакетов на хосте в build/dist (без Docker)"
	@echo "  make print-tag  показать тег, который будет присвоен образу"
	@echo ""
	@echo "  Версия пакетов (lockstep, статикой в pyproject.toml): $(VERSION)"
	@echo "  make version       показать текущую версию"
	@echo "  make version-set   VER=X.Y.Z — проставить версию во все пакеты и их внутренние пины"
	@echo "  make version-check  проверить, что версия едина и пины boba-* ей соответствуют"
	@echo ""
	@echo "  Образ базируется на dmp/gcc (glibc+gcc8) с python из dmp/python -dist."
	@echo "  Зависимости ставятся offline из artifacts/wheels через uv."
	@echo "  Параметры: BOBA_TAG=<тег> (запинить), NO_CACHE=1, GCC_IMAGE=<ref>, PYTHON_IMAGE=<ref>"
	@echo "  Полный цикл с нуля: make all"
	@echo ""


# Все pyproject.toml пакетов; packages/<pkg>/build/ — артефакты сборки, не пакеты.
PYPROJECTS = $(shell find $(SRC_DIR)/packages -name pyproject.toml -not -path '*/build/*')


.PHONY: version
version:
	@echo $(VERSION)


# Единственный способ поменять версию: правит поле version и пины "boba-*==<old>"
# во всех пакетах разом. Держать их в рассинхроне нельзя — lockstep.
.PHONY: version-set
version-set:
	@test -n "$(VER)" || { echo "укажи версию: make version-set VER=X.Y.Z" >&2; exit 1; }
	@echo "$(VER)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$$' \
	  || { echo "версия должна быть X.Y.Z или X.Y.Z.devN, а не '$(VER)'" >&2; exit 1; }
	@sed -i -e 's/^version = "$(VERSION)"$$/version = "$(VER)"/'                     \
	        -e 's/"\(boba-[a-z0-9-]*\)==$(VERSION)"/"\1==$(VER)"/g' $(PYPROJECTS)
	@echo ">>> $(VERSION) -> $(VER) в $(words $(PYPROJECTS)) пакетах"
	@$(MAKE) --no-print-directory version-check VERSION=$(VER)


# Ловит рассинхрон после ручной правки pyproject.toml: разные version у пакетов
# либо пин boba-* на чужую версию (в т.ч. пропущенный при бампе).
.PHONY: version-check
version-check:
	@bad=$$(grep -h '^version = ' $(PYPROJECTS) | sort -u | grep -v '^version = "$(VERSION)"$$'); \
	  if [ -n "$$bad" ]; then echo "версии пакетов разошлись с $(VERSION):" >&2;                  \
	    echo "$$bad" >&2; exit 1; fi
	@bad=$$(grep -hoE '^[[:space:]]+"boba-[a-z0-9-]+[^"]*"' $(PYPROJECTS) | tr -d '[:blank:]'      \
	        | grep -v '^"boba-[a-z0-9-]*==$(VERSION)"$$' | sort -u);                              \
	  if [ -n "$$bad" ]; then echo "внутренние зависимости не запинены на $(VERSION):" >&2;       \
	    echo "$$bad" >&2; exit 1; fi
	@echo "version-check: $(VERSION), $(words $(PYPROJECTS)) пакетов — ok"


.PHONY: debug
debug:
	$(info BASE_DIR       = $(BASE_DIR))
	$(info SRC_DIR        = $(SRC_DIR))
	$(info ARTIFACTS      = $(ARTIFACTS))
	$(info BUILD_CONTEXT  = $(BUILD_CONTEXT))
	$(info TEST_DIR       = $(TEST_DIR))
	$(info BOBA_TAG       = $(BOBA_TAG))
	$(info VERSION        = $(VERSION))
	$(info IMAGE          = $(IMAGE))
	$(info GCC_IMAGE      = $(GCC_IMAGE))
	$(info PYTHON_IMAGE   = $(PYTHON_IMAGE))
	$(info NO_CACHE       = $(NO_CACHE))
	$(info NO_CACHE_TEXT  = $(NO_CACHE_TEXT))
	$(info UV_URL         = $(UV_URL))
	$(info CA_CHAIN       = $(CA_CHAIN))
	@true


# Скачать то, что качается заранее: OCR-модели tesseract + бинарь uv.
# Идемпотентно (уже скачанное пропускает). Внутренний mirror — через переопределение URL.
# gcc/python/glibc НЕ качаем и НЕ собираем: они уже в образах dmp/gcc и dmp/python.
.PHONY: sources
sources:
	@mkdir -p $(ARTIFACTS)/tessdata $(ARTIFACTS)/bin
	@echo ">>> OCR-модели tesseract"
	@set -e; for lang in $(TESS_LANGS); do                                                     \
	  dest="$(ARTIFACTS)/tessdata/$$lang.traineddata";                                         \
	  if [ -f "$$dest" ]; then echo "  ✓ $$dest";                                              \
	  else echo ">>> $$lang"; curl -fSL -o "$$dest" "$(TESSDATA_REPO)/$$lang.traineddata"; fi; \
	done
	@echo ">>> uv ($(UV_PLATFORM))"
	@if [ -f "$(ARTIFACTS)/bin/uv" ]; then echo "  ✓ $(ARTIFACTS)/bin/uv"; \
	else echo ">>> $(UV_URL)";                                             \
	  curl -fSL "$(UV_URL)" | tar -xz -C /tmp;                             \
	  install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(ARTIFACTS)/bin/uv;       \
	  rm -rf /tmp/uv-$(UV_PLATFORM);                                       \
	  $(ARTIFACTS)/bin/uv --version; fi


# Базовый образ boba-base: gcc-тулчейн + python + uv, без установленных пакетов.
# Нужен для offline-пересборки wheels и скачивания весов fastembed.
.PHONY: base
base:
	docker build -f Dockerfile --target base $(NO_CACHE_TEXT) \
		--build-arg GCC_IMAGE=$(GCC_IMAGE)                \
		--build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)          \
		--tag boba-base:latest                            \
		--progress plain $(BUILD_CONTEXT)


# Пересобрать offline-wheels в artifacts/wheels по всем pyproject.toml из ../packages.
# Каждый pyproject.toml внутри packages/ — это отдельный пакет.
.PHONY: wheels
wheels: version-check
	@echo ">>> версия пакетов: $(VERSION)";                                                                      \
	  docker run --rm                                                                                            \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                                             \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro                                        \
	    -e PIP_CERT=/tmp/ca-chain.crt -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e SSL_CERT_FILE=/tmp/ca-chain.crt) \
	  -v "$(ARTIFACTS)":/artifacts                                                                                \
	  -v "$(SRC_DIR)":/boba:ro                                                                                    \
	  $(if $(wildcard $(ARTIFACTS)/pip.conf),-v "$(ARTIFACTS)/pip.conf":/etc/pip.conf:ro)                         \
	  -w /artifacts --entrypoint sh boba-base:latest -c '                                                         \
	    set -e; mkdir -p wheels;                                                                                  \
	    cp -r /boba/packages /tmp/packages;                                                                       \
	    pip3 wheel --no-cache-dir                                                                                 \
	      $$(find /tmp/packages -name pyproject.toml -not -path "*/build/*" -printf "%h ")                        \
	      pip setuptools wheel -w wheels/;                                                                        \
	    chown -R "$$HOST_UID:$$HOST_GID" wheels'


# Скачать embedding-веса fastembed (ONNX) в artifacts/fastembed/.
# Работает на boba-base: fastembed ставится offline из wheelhouse, веса качаются с HF.
# Нужны boba-base + wheels + сеть/mirror к HuggingFace; make build НЕ требуется
# (наоборот — fastembed должен быть ДО build, т.к. веса запекаются в образ).
.PHONY: fastembed
fastembed:
	@mkdir -p $(ARTIFACTS)/fastembed
	docker run --rm                                                                                                      \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                                                    \
	  -e EMBED_MODELS="$(EMBED_MODELS)"                                                                                  \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy                                  \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro                                               \
	    -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e CURL_CA_BUNDLE=/tmp/ca-chain.crt)                                     \
	  -v "$(ARTIFACTS)":/artifacts                                                                                       \
	  --entrypoint sh boba-base:latest -c '                                                                              \
	    set -e;                                                                                                          \
	    pip3 install --no-cache-dir --no-index --find-links=/artifacts/wheels fastembed;                                 \
	    for m in $$EMBED_MODELS; do echo ">>> fastembed: $$m";                                                           \
	      python3 -c "import sys; from fastembed import TextEmbedding;                                                   \
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/artifacts/fastembed\");                                 \
	        list(e.embed([\"probe\"]))" "$$m"                                                                            \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed (TextEmbedding.list_supported_models())"; \
	    done;                                                                                                            \
	    chown -R "$$HOST_UID:$$HOST_GID" /artifacts/fastembed'


# Собрать runtime-образ boba: пакеты ставятся offline из artifacts/wheels через uv.
.PHONY: build
build: version-check
	@echo ">>> source:   $(SRC_DIR) @ $(BOBA_TAG)";                \
	  echo ">>> version:  $(VERSION)";                             \
	  echo ">>> building: $(IMAGE)  (+ boba:latest)";              \
	  docker build -f Dockerfile $(NO_CACHE_TEXT)                  \
	  --build-arg GCC_IMAGE=$(GCC_IMAGE)                           \
	  --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)                     \
	  --build-arg BOBA_VERSION="$(VERSION)"                        \
	  -t $(IMAGE) -t boba:latest                                   \
	  --progress plain $(BUILD_CONTEXT)


# Собрать wheel'ы всех пакетов на хосте в build/dist/ (без Docker).
# Версия берётся из pyproject.toml; --no-sources проверяет, что сборка не зависит
# от [tool.uv.sources] (то есть колесо соберётся и вне workspace).
# Нужен uv в artifacts/bin (make sources) и сеть/mirror для build-зависимостей.
.PHONY: dist
dist: version-check
	@echo ">>> версия пакетов: $(VERSION)";                                      \
	  rm -rf "$(BASE_DIR)/dist";                                                 \
	  cd "$(SRC_DIR)" &&                                                         \
	    "$(ARTIFACTS)/bin/uv" build --all-packages --wheel --no-sources          \
	      -o "$(BASE_DIR)/dist";                                                 \
	  echo ">>> wheels в build/dist:"; ls -1 "$(BASE_DIR)/dist"


# Функциональная проверка собранного образа boba:latest.
.PHONY: test
test:
	docker run --rm --entrypoint /bin/bash -v $(TEST_DIR):/test:ro boba:latest -euc ' \
		echo "== image ==";                                                       \
		python3 --version;                                                        \
		uv --version;                                                             \
		echo "== functional checks ==";                                           \
		python3 /test/bobacheck.py'


# Удалить скачанные источники (tessdata + бинарь uv). wheels/fastembed НЕ трогаем.
.PHONY: clean
clean:
	rm -rf $(ARTIFACTS)/tessdata $(ARTIFACTS)/bin/uv


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources base wheels fastembed build test
