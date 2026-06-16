SRC_DIR       ?= ..
ARTIFACTS     ?= artifacts
BUILD_CONTEXT ?= ..
export SRC_DIR

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

export BOBA_TAG

.DEFAULT_GOAL := help
.PHONY: help build print-tag base sources tessdata fastembed wheels uv


GLIBC_VERSION  ?= 2.28
GCC_VERSION    ?= 8.5.0
PYTHON_VERSION ?= 3.11.12
GMP_VERSION    ?= 6.1.2
MPFR_VERSION   ?= 4.0.2
MPC_VERSION    ?= 1.1.0

# OCR-модели tesseract (make tessdata) и embedding-веса fastembed (make fastembed)
TESS_LANGS    ?= eng rus osd
TESSDATA_REPO ?= https://github.com/tesseract-ocr/tessdata_best/raw/main
EMBED_MODELS  ?= intfloat/multilingual-e5-large

# uv — готовый статический бинарь (НЕ компилируется, в отличие от wheels).
# Платформа = выбор релиза под arch+libc: musl = статический, без зависимости от glibc.
# Для внутреннего mirror достаточно переопределить UV_URL.
UV_VERSION  ?= 0.11.21
UV_PLATFORM ?= x86_64-unknown-linux-musl
UV_URL      ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

help:
	@echo "Boba runtime — make-цели (тег образа: $(BOBA_TAG)):"
	@echo "  make print-tag  показать тег, который будет присвоен образу"
	@echo "  make sources    скачать тарболы glibc/gcc/gmp/mpfr/mpc/python для base (нужен инет)"
	@echo "  make tessdata   скачать OCR-модели tesseract в $(ARTIFACTS)/tessdata (нужен инет)"
	@echo "  make base       собрать базовый образ boba-base (glibc 2.28 + gcc 8 + python 3.11)"
	@echo "  make wheels     пересобрать offline-wheels внутри boba-base по ../packages"
	@echo "  make fastembed  скачать embedding-веса в $(ARTIFACTS)/fastembed (boba-base + wheels + инет; ДО build)"
	@echo "  make uv         скачать uv (статич. бинарь) в $(ARTIFACTS)/bin/uv (mirror — через UV_URL)"
	@echo "                  (закрытый контур — через $(ARTIFACTS)/pip.conf + uv.toml + ca-chain.crt, если есть)"
	@echo "  make build      собрать runtime boba:<тег> (+ boba:latest), offline из wheels"
	@echo ""
	@echo "Параметры: BOBA_TAG=<тег> (запинить)"
	@echo "Полный цикл с нуля: make sources tessdata base wheels fastembed build"

# Скачать исходники для make base (glibc/gcc/gmp/mpfr/mpc/python)
sources:
	@mkdir -p $(ARTIFACTS)/glibc-src $(ARTIFACTS)/gcc-src $(ARTIFACTS)/python-src
	@set -e; for spec in \
	  "$(ARTIFACTS)/glibc-src/glibc-$(GLIBC_VERSION).tar.xz=https://ftp.gnu.org/gnu/glibc/glibc-$(GLIBC_VERSION).tar.xz" \
	  "$(ARTIFACTS)/gcc-src/gcc-$(GCC_VERSION).tar.xz=https://ftp.gnu.org/gnu/gcc/gcc-$(GCC_VERSION)/gcc-$(GCC_VERSION).tar.xz" \
	  "$(ARTIFACTS)/gcc-src/gmp-$(GMP_VERSION).tar.xz=https://ftp.gnu.org/gnu/gmp/gmp-$(GMP_VERSION).tar.xz" \
	  "$(ARTIFACTS)/gcc-src/mpfr-$(MPFR_VERSION).tar.xz=https://ftp.gnu.org/gnu/mpfr/mpfr-$(MPFR_VERSION).tar.xz" \
	  "$(ARTIFACTS)/gcc-src/mpc-$(MPC_VERSION).tar.gz=https://ftp.gnu.org/gnu/mpc/mpc-$(MPC_VERSION).tar.gz" \
	  "$(ARTIFACTS)/python-src/Python-$(PYTHON_VERSION).tar.xz=https://www.python.org/ftp/python/$(PYTHON_VERSION)/Python-$(PYTHON_VERSION).tar.xz" \
	; do \
	  dest="$${spec%%=*}"; url="$${spec#*=}"; \
	  if [ -f "$$dest" ]; then echo "  ✓ $$dest"; \
	  else echo ">>> $$url"; curl -fSL -o "$$dest" "$$url"; fi; \
	done

# Скачать OCR-модели tesseract в $(ARTIFACTS)/tessdata
tessdata:
	@mkdir -p $(ARTIFACTS)/tessdata
	@set -e; for lang in $(TESS_LANGS); do \
	  dest="$(ARTIFACTS)/tessdata/$$lang.traineddata"; \
	  if [ -f "$$dest" ]; then echo "  ✓ $$dest"; \
	  else echo ">>> $$lang"; curl -fSL -o "$$dest" "$(TESSDATA_REPO)/$$lang.traineddata"; fi; \
	done

base:
	docker build -f Dockerfile --target base -t boba-base:latest --progress plain $(BUILD_CONTEXT)

# Закрытый контур если есть ca-chain.crt
CA_CHAIN := $(wildcard $(ARTIFACTS)/ca-chain.crt)

# Пересобрать wheels offline
wheels:
	docker run --rm \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)" \
	  $(if $(CA_CHAIN),-v "$$(pwd)/$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro \
	    -e PIP_CERT=/tmp/ca-chain.crt -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e SSL_CERT_FILE=/tmp/ca-chain.crt) \
	  -v "$$(pwd)/$(ARTIFACTS)":/artifacts \
	  -v "$(abspath $(SRC_DIR))":/boba:ro \
	  $(if $(wildcard $(ARTIFACTS)/pip.conf),-v "$$(pwd)/$(ARTIFACTS)/pip.conf":/etc/pip.conf:ro) \
	  -w /artifacts --entrypoint sh boba-base:latest -c '\
	    set -e; rm -rf wheels && mkdir wheels; \
	    cp -r /boba/packages /tmp/packages; \
	    pip3 wheel --no-cache-dir \
	      $$(find /tmp/packages -name pyproject.toml -printf "%h ") \
	      pip setuptools wheel -w wheels/; \
	    chown -R "$$HOST_UID:$$HOST_GID" wheels'

# Скачать uv на ХОСТ (прямой инет; внутренний mirror — через UV_URL) → $(ARTIFACTS)/bin/uv
uv:
	@mkdir -p $(ARTIFACTS)/bin
	@echo ">>> $(UV_URL)"
	curl -fSL "$(UV_URL)" | tar -xz -C /tmp
	install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(ARTIFACTS)/bin/uv
	@rm -rf /tmp/uv-$(UV_PLATFORM)
	@$(ARTIFACTS)/bin/uv --version

build:
	@echo ">>> source:   $(SRC_DIR) @ $(BOBA_TAG)"
	@echo ">>> building: boba:$(BOBA_TAG)  (+ boba:latest)"
	docker build -f Dockerfile \
	  -t boba:$(BOBA_TAG) -t boba:latest \
	  $(BUILD_CONTEXT)

# Скачать embedding-веса fastembed (ONNX) в $(ARTIFACTS)/fastembed/.
# Работает на boba-base: fastembed ставится offline из wheelhouse, веса качаются с HF.
# Нужны boba-base + wheels + сеть/mirror к HuggingFace; make build НЕ требуется
# (наоборот — fastembed должен быть ДО build, т.к. веса запекаются в образ).
fastembed:
	@mkdir -p $(ARTIFACTS)/fastembed
	docker run --rm \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)" \
	  -e EMBED_MODELS="$(EMBED_MODELS)" \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \
	  $(if $(CA_CHAIN),-v "$$(pwd)/$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro \
	    -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e CURL_CA_BUNDLE=/tmp/ca-chain.crt) \
	  -v "$$(pwd)/$(ARTIFACTS)":/artifacts \
	  --entrypoint sh boba-base:latest -c '\
	    set -e; \
	    pip3 install --no-cache-dir --no-index --find-links=/artifacts/wheels fastembed; \
	    for m in $$EMBED_MODELS; do echo ">>> fastembed: $$m"; \
	      python3 -c "import sys; from fastembed import TextEmbedding; \
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/artifacts/fastembed\"); \
	        list(e.embed([\"probe\"]))" "$$m" \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed (TextEmbedding.list_supported_models())"; \
	    done; \
	    chown -R "$$HOST_UID:$$HOST_GID" /artifacts/fastembed'

print-tag:
	@echo $(BOBA_TAG)
