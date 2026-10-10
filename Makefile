BOARD ?= root@192.168.1.161
SSH_KEY ?= ~/.ssh/mkzev_rsa
APP_DIR := /mount/rfs/rw/.pioneer/app
PY := uv run --python 3.12  # вызывается из backend/

.PHONY: dev core ui build test deploy

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

deploy: build  ## код на плату (tar по ssh — rsync на плате нет) + идемпотентный install.sh
	COPYFILE_DISABLE=1 tar czf - --exclude __pycache__ backend/pioneer boards deploy frontend/dist \
		| ssh -i $(SSH_KEY) $(BOARD) 'set -e; mkdir -p $(APP_DIR).new && tar xzf - -C $(APP_DIR).new 2>/dev/null; \
		  rm -rf $(APP_DIR).old; [ -d $(APP_DIR) ] && mv $(APP_DIR) $(APP_DIR).old; mv $(APP_DIR).new $(APP_DIR)'
	ssh -i $(SSH_KEY) $(BOARD) 'sh $(APP_DIR)/deploy/install.sh'
