# Angular needs an LTS node (22/24); Homebrew's node@24 is preferred when present.
NODE_BIN := $(firstword $(wildcard /opt/homebrew/opt/node@24/bin /usr/local/opt/node@24/bin))
export PATH := $(if $(NODE_BIN),$(NODE_BIN):,)$(PATH)

.PHONY: up down logs install dev backend frontend test test-all e2e smoke llm-smoke build

up:            ## start agentgateway + connectors
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

install:
	cd backend && uv sync
	cd frontend && npm install

backend:       ## FastAPI on :8000 (reload)
	cd backend && uv run app

frontend:      ## Angular dev server on :4200 (proxies /api -> :8000)
	cd frontend && npx ng serve

dev:           ## backend + frontend together (Ctrl-C stops both)
	@trap 'kill 0' INT TERM; \
	(cd backend && uv run app) & \
	(cd frontend && npx ng serve) & \
	wait

test:          ## unit tests (no network)
	cd backend && uv run pytest -m "not network and not e2e"

test-all:      ## includes live gateway tests (needs `make up`)
	cd backend && uv run pytest -m "not e2e"

smoke:         ## list tools through the gateway
	uv run --project connectors python scripts/mcp_smoke.py http://localhost:3000/mcp

llm-smoke:     ## check the LiteLLM connection from .env
	cd backend && uv run python -m app.llm

build:
	cd frontend && npx ng build

e2e:           ## functional test: builder creates the HN bot from chat, runs it, follow-up (real LiteLLM + gateway)
	cd backend && E2E=1 uv run pytest -m e2e tests/e2e -v -rs
