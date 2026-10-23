.PHONY: install css test test-unit test-integration coverage lint format format-check \
	typecheck import-lint squawk openapi openapi-check check up up-build start stop down logs ps \
	health smoke-setup smoke-up smoke-down cloud-init-check migrate purge-conversations llm-report latency-scenario evals e2e-install e2e

install: ## Install dependencies (uv sync)
	uv sync

css: ## Compile the Tailwind CSS v4 + daisyUI 5 stylesheet locally (no Node, ADR-0012)
	./scripts/build_css.sh

test: migrate ## Run the full test suite
	uv run pytest

test-unit: ## Run unit tests only (no external services needed)
	uv run pytest tests/unit

test-integration: migrate ## Run integration tests (needs `make up` first)
	uv run pytest tests/integration

coverage: migrate ## Line coverage report for src/ai_trainer and scripts
	uv run pytest --cov=ai_trainer --cov=scripts --cov-report=term-missing

lint: ## Ruff lint
	uv run ruff check .

format: ## Ruff format (writes changes)
	uv run ruff format .

format-check: ## Ruff format check (no changes)
	uv run ruff format --check .

typecheck: ## mypy --strict
	uv run mypy

import-lint: ## Check layer boundaries (import-linter, ADR-0003)
	uv run lint-imports

squawk: ## Lint the SQL of migration revisions new since origin/main with squawk (ADR-0024). Usage: make squawk [base=origin/main]
	uv run python scripts/squawk_migrations.py $(if $(base),--base $(base))

openapi: ## Regenerate the OpenAPI snapshot (docs/api/openapi.json)
	uv run python scripts/generate_openapi.py

openapi-check: openapi ## Fail if docs/api/openapi.json is out of date (CI drift check)
	git diff --exit-code -- docs/api/openapi.json

check: lint format-check typecheck import-lint squawk openapi-check cloud-init-check test ## Everything CI runs

up: ## Start app + postgres in the background
	docker compose up -d

up-build: ## Rebuild images, then start app + postgres
	docker compose up -d --build

start: ## Dev loop: app + postgres with uvicorn --reload, a live Tailwind CSS rebuild and a one-shot migrate step (#64) over src/ (foreground; needs a 2nd terminal for make health/make logs)
	docker compose -f compose.yaml -f compose.dev.yaml build
	@# Foreground first: `watch` streams only `app`, so a failing `migrate` (a blank Google key, #72) would
	@# show as a bare "exit 1". Here its output reaches this terminal and a failure stops `make`.
	docker compose -f compose.yaml -f compose.dev.yaml run --rm migrate
	docker compose -f compose.yaml -f compose.dev.yaml watch

stop: ## Stop the make start stack (keeps the postgres volume)
	docker compose -f compose.yaml -f compose.dev.yaml down

down: ## Stop and remove the stack (keeps the postgres volume)
	docker compose down

logs: ## Follow container logs (includes the one-shot migrate service from `make start`, #64)
	docker compose -f compose.yaml -f compose.dev.yaml logs -f

ps: ## Show container + healthcheck status (includes the one-shot migrate service from `make start`, #64)
	docker compose -f compose.yaml -f compose.dev.yaml ps -a

health: ## Curl the running app's health endpoint
	curl -sS http://localhost:8000/health

# The production stack run locally (#221): a locally built image instead of a registry digest,
# localhost as the site, and .smoke/ (from `make smoke-setup`) as the config directory.
SMOKE_COMPOSE = APP_IMAGE=ai-trainer:smoke SITE_HOST=localhost CONFIG_DIR=.smoke docker compose -f compose.prod.yaml

smoke-setup: ## Write .smoke/: test certificates, a database password and an app .env from yours (#221)
	./deploy/smoke-setup.sh

smoke-up: ## Build the image and run compose.prod.yaml locally behind Caddy on https://localhost (#221; needs `make smoke-setup`)
	docker build -t ai-trainer:smoke .
	$(SMOKE_COMPOSE) up -d --wait

smoke-down: ## Stop the smoke stack (keeps its volumes; add `-v` by hand to drop them)
	$(SMOKE_COMPOSE) down

cloud-init-check: ## Validate deploy/cloud-init.yaml with Debian 13's own cloud-init (ADR-0023, #222)
	docker run --rm -e DEBIAN_FRONTEND=noninteractive -v "$(CURDIR)/deploy:/src/deploy:ro" -w /src debian:trixie sh -c \
		'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends cloud-init >/dev/null && cloud-init schema --config-file deploy/cloud-init.yaml'

migrate: ## Apply database migrations (Alembic)
	uv run alembic upgrade head

purge-conversations: ## Delete conversations past CONVERSATION_RETENTION_DAYS and print how many (ADR-0021); needs the stack's database up
	uv run ai-trainer purge-conversations

llm-report: ## Summarize the LLM debug log per template and model (#179); fails when there is no log. Usage: make llm-report [since=2026-10-18T20:00Z]
	uv run ai-trainer llm-debug-report $(if $(since),--since $(since))

latency-scenario: ## Replay the latency scenario on the running app and print p50/p95 per log turn (ADR-0026, #185) — calls real models, costs money. Needs `make start`. Usage: make latency-scenario [url=http://localhost:8000]
	uv run python -m scripts.latency_scenario $(if $(url),--url $(url))

evals: ## Run prompt or decision-set evals at the app's version unless version= is given — costs money, run on purpose; promote=1 writes the baseline. Usage: make evals template=<id> [version=2] [model=openai/gpt-5] [repeat=3] [promote=1]
	uv run ai-trainer-evals run $(template) $(if $(version),--version $(version)) $(if $(model),--model $(model)) $(if $(repeat),--repeat $(repeat)) $(if $(promote),--promote)

e2e-install: ## One-time Playwright browser download for e2e specs (no --with-deps: needs sudo, Linux-only)
	uv run playwright install chromium

e2e: migrate ## Run pytest-playwright specs against the running compose stack (needs `make up` first)
	uv run pytest tests/e2e
