BOARD ?= root@192.168.1.161
SSH_KEY ?= ~/.ssh/mkzev_rsa
APP_DIR := /mount/rfs/rw/.pioneer/app
PY := uv run --python 3.12  # вызывается из backend/

.PHONY: dev core ui build test deploy reference

dev:  ## core (mock-HAL на Mac) :8100 + Vite :5173 -> http://localhost:5173/pioneer/
	$(MAKE) -j2 core ui

DEV_PASSWORD ?= pioneer

core:  ## при первом запуске задаёт пароль входа $(DEV_PASSWORD) в .devdata
	cd backend && echo "$(DEV_PASSWORD)" | $(PY) python -m pioneer set-password --if-missing --stdin
	cd backend && $(PY) python -m pioneer

ui:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

test:
	cd backend && $(PY) pytest -q
	cd frontend && npm run typecheck
	cd frontend && npm test

reference:  ## справочник SDK в JSON (для статического сайта; UI берёт его из /api/help)
	cd backend && $(PY) python ../tools/gen_sdk_reference.py ../sdk-reference.json

vendor-ai:  ## SDK + cp312/aarch64 wheels, separately from system Python
	cd backend && uv pip install --python .venv/bin/python --target pioneer/vendor_ai --no-deps --python-platform aarch64-manylinux_2_28 --python-version 3.12 --only-binary :all: -r ../deploy/ai-requirements.txt

deploy: build vendor-ai  ## код на плату (tar по ssh — rsync на плате нет) + идемпотентный install.sh
	COPYFILE_DISABLE=1 tar czf - --exclude __pycache__ backend/pioneer boards deploy docs/sdk frontend/dist modules/examples tools/media-test-server.py \
		| ssh -i $(SSH_KEY) $(BOARD) 'set -e; mkdir -p $(APP_DIR).new && tar xzf - -C $(APP_DIR).new 2>/dev/null; \
		  rm -rf $(APP_DIR).old; [ -d $(APP_DIR) ] && mv $(APP_DIR) $(APP_DIR).old; mv $(APP_DIR).new $(APP_DIR)'
	ssh -i $(SSH_KEY) $(BOARD) 'sh $(APP_DIR)/deploy/install.sh'
