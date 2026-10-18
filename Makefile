# Angular needs an LTS node (22/24); Homebrew's node@24 is preferred when present.
NODE_BIN := $(firstword $(wildcard /opt/homebrew/opt/node@24/bin /usr/local/opt/node@24/bin))
export PATH := $(if $(NODE_BIN),$(NODE_BIN):,)$(PATH)

.PHONY: secrets-init up up-all down gateway-check logs jwt-init token user admin connector-init connector-user install dev backend frontend test test-all e2e smoke llm-smoke build

up: secrets-init jwt-init connector-init   ## start agentgateway + connectors (A2A route -> backend on the host)
	A2A_BACKEND=host.docker.internal:8000 docker compose up -d --build

up-all: secrets-init jwt-init connector-init   ## everything in docker: gateway, connectors, backend, UI at http://localhost:8080
	docker compose --profile app up -d --build
	@echo "UI: http://localhost:8080   gateway admin: http://localhost:15000/ui"

down:
	docker compose --profile app down

logs:
	docker compose --profile app logs -f

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

jwt-init:         ## gateway token keys (secrets/jwt-private.pem, secrets/jwks.json) and secrets/trace-ingest.env
	@cd backend && uv run python -m app.tokens init

token:            ## gateway token for an MCP/A2A client: make token USER_ID=alice
	@cd backend && uv run python -m app.tokens token $(or $(USER_ID),shared)

user:             ## add a login: make user USER_ID=alice (asks for the password)
	cd backend && uv run python -m app.auth $(USER_ID)

admin:            ## make an existing user admin: make admin USER_ID=alice
	cd backend && uv run python -m app.auth --admin $(USER_ID)

gateway-check:    ## validate gateway/config.yaml with the agentgateway image in docker-compose.yml (before upgrades)
	docker compose run --rm --no-deps agentgateway -f /config/config.yaml --validate-only

secrets-init:     ## generate secrets/postgres.env (random password) and secrets/connectors.env (once; never overwrites)
	@mkdir -p secrets
	@test -f secrets/postgres.env || (umask 077 && printf 'POSTGRES_PASSWORD=%s\n' "$$(openssl rand -hex 24)" > secrets/postgres.env && echo "wrote secrets/postgres.env")
	@test -f secrets/connectors.env || (umask 077 && cp connectors/connectors.env.example secrets/connectors.env && echo "wrote secrets/connectors.env")

connector-init:   ## generate open-connector secrets (once; never overwrites)
	@python3 scripts/open_connector.py init

connector-user:   ## per-user open-connector token: make connector-user USER_ID=<login username>
	python3 scripts/open_connector.py user $(USER_ID)
	docker compose up -d agentgateway

smoke:         ## list tools through the gateway (as USER_ID, default the shared subject)
	MCP_TOKEN=$$(cd backend && uv run python -m app.tokens token $(or $(USER_ID),shared)) \
	  uv run --project connectors python scripts/mcp_smoke.py http://localhost:3000/mcp

llm-smoke:     ## list models of the configured providers and ping BUILDER_MODEL
	cd backend && uv run python -m app.llm

build:
	cd frontend && npx ng build

e2e:           ## functional test: builder creates the HN bot from chat, runs it, follow-up (real LiteLLM + gateway)
	cd backend && E2E=1 uv run pytest -m e2e tests/e2e -v -rs
