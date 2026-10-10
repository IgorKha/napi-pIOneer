BOARD ?= root@192.168.1.161
SSH_KEY ?= ~/.ssh/mkzev_rsa
APP_DIR := /mount/rfs/rw/.pioneer/app
PY := uv run --python 3.12  # вызывается из backend/

.PHONY: dev core ui build test deploy

dev:  ## core (mock-HAL на Mac) :8100 + Vite :5173 -> http://localhost:5173/pioneer/
	$(MAKE) -j2 core ui

core:
	cd backend && $(PY) python -m pioneer

ui:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

test:
	cd backend && $(PY) pytest -q
	cd frontend && npm run typecheck

deploy: build  ## rsync на плату + идемпотентный install.sh
	# раскладка как в репо: REPO_ROOT в config.py = app/
	rsync -azR --delete -e "ssh -i $(SSH_KEY)" --exclude __pycache__ \
		backend/pioneer boards deploy frontend/dist $(BOARD):$(APP_DIR)/
	ssh -i $(SSH_KEY) $(BOARD) 'sh $(APP_DIR)/deploy/install.sh'
