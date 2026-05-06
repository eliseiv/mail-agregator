# ---------------------------------------------------------------------------
# Mail Aggregator — local dev shortcuts.
#
# All real configuration lives in docker-compose.yml and deploy/Dockerfile.
# This Makefile is a convenience wrapper. Windows users without `make`
# can read the recipes and run the underlying commands directly.
# ---------------------------------------------------------------------------

.DEFAULT_GOAL := help
SHELL := /bin/sh

# Allow `IMAGE_TAG=v1.2.3 make build` style overrides.
COMPOSE      ?= docker compose
COMPOSE_PROD ?= $(COMPOSE) --profile prod

## ---------------------------------------------------------------------------
## Help
## ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make <target>\n\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)

## ---------------------------------------------------------------------------
## Lifecycle
## ---------------------------------------------------------------------------

.PHONY: env
env: ## Create .env from .env.example if absent
	@test -f .env || cp .env.example .env && echo ".env ready (review secrets!)"

.PHONY: build
build: ## Build api + worker images
	$(COMPOSE) build api worker

.PHONY: up
up: env ## Start full stack in background (dev profile)
	$(COMPOSE) up -d

.PHONY: up-prod
up-prod: env ## Start full stack incl. caddy (prod profile)
	$(COMPOSE_PROD) up -d

.PHONY: down
down: ## Stop and remove containers (keep volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop containers AND wipe volumes (DATA LOSS)
	$(COMPOSE) down -v

.PHONY: restart
restart: ## Restart api + worker only (keep DB/redis/minio)
	$(COMPOSE) restart api worker

## ---------------------------------------------------------------------------
## Inspection
## ---------------------------------------------------------------------------

.PHONY: ps
ps: ## List containers + health status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail api + worker logs (Ctrl+C to detach)
	$(COMPOSE) logs -f api worker

.PHONY: logs-all
logs-all: ## Tail every service's logs
	$(COMPOSE) logs -f

.PHONY: shell-api
shell-api: ## Open a shell in the api container
	$(COMPOSE) exec api sh

.PHONY: shell-worker
shell-worker: ## Open a shell in the worker container
	$(COMPOSE) exec worker sh

.PHONY: psql
psql: ## Open psql against the dev database
	$(COMPOSE) exec postgres psql -U mas -d mail_aggregator

.PHONY: redis-cli
redis-cli: ## Open redis-cli against the dev redis
	$(COMPOSE) exec redis redis-cli

## ---------------------------------------------------------------------------
## CI-equivalent local checks
## ---------------------------------------------------------------------------

.PHONY: lint
lint: ## Run ruff check + format-check (mirrors CI)
	uv run ruff check .
	uv run ruff format --check .

.PHONY: type
type: ## Run mypy on backend/worker/shared
	uv run mypy backend worker shared

.PHONY: test
test: ## Run pytest with coverage gate
	uv run pytest -v --cov=backend --cov=worker --cov=shared --cov-fail-under=75

.PHONY: check
check: lint type test ## Full local CI pass
