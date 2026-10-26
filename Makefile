.PHONY: install css test test-unit test-integration coverage lint format format-check \
	typecheck import-lint squawk openapi openapi-check check up up-build start stop down logs ps \
	health smoke-setup smoke-up smoke-down alloy-check cloud-init-check backup-check migrate purge-conversations llm-report latency-scenario evals e2e-install e2e

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

check: lint format-check typecheck import-lint squawk openapi-check cloud-init-check alloy-check test ## Everything CI runs

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
# `env` makes it one command, so deploy/monitor-password.sh can take it as its arguments.
SMOKE_COMPOSE = env APP_IMAGE=ai-trainer:smoke SITE_HOST=localhost CONFIG_DIR=.smoke docker compose -f compose.prod.yaml

smoke-setup: ## Write .smoke/: test certificates, a database password and an app .env from yours (#221)
	./deploy/smoke-setup.sh

smoke-up: ## Build the image and run compose.prod.yaml locally behind Caddy on https://localhost (#221; needs `make smoke-setup`)
	docker build -t ai-trainer:smoke .
	$(SMOKE_COMPOSE) up -d --wait
	./deploy/monitor-password.sh .smoke/alloy.env $(SMOKE_COMPOSE)

smoke-down: ## Stop the smoke stack (keeps its volumes; add `-v` by hand to drop them)
	$(SMOKE_COMPOSE) down

ALLOY_IMAGE = grafana/alloy:v1.20.1@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f
config ?= deploy/alloy/config.alloy

# Placeholders for the load step: no network, so nothing is ever sent.
ALLOY_CHECK_ENV = -e GRAFANA_CLOUD_OTLP_URL=http://127.0.0.1:1 -e GRAFANA_CLOUD_OTLP_USER=check \
	-e GRAFANA_CLOUD_METRICS_URL=http://127.0.0.1:1/api/v1/write -e GRAFANA_CLOUD_METRICS_USER=check \
	-e POSTGRES_MONITOR_PASSWORD=check0123456789abcdef0123 \
	-e GRAFANA_CLOUD_LOGS_URL=http://127.0.0.1:1/loki/api/v1/push -e GRAFANA_CLOUD_LOGS_USER=check \
	-e GRAFANA_CLOUD_TOKEN=check
# prometheus.exporter.unix opens the host's /proc and /sys when it is built, as compose.prod.yaml
# mounts them (#273); here they are the Docker VM's.
ALLOY_CHECK_MOUNTS = -v /proc:/host/proc:ro -v /sys:/host/sys:ro -v /:/host/root:ro

alloy-check: ## Format-check, validate and load the Alloy config with the image compose.prod.yaml runs (ADR-0030, #272); `config=<file>` checks another
	docker run --rm -v "$(abspath $(config)):/check/config.alloy:ro" $(ALLOY_IMAGE) fmt --test /check/config.alloy
	docker run --rm $(ALLOY_CHECK_MOUNTS) -v "$(abspath $(config)):/check/config.alloy:ro" $(ALLOY_IMAGE) validate /check/config.alloy
	@# `validate` skips what fails only when components are built, like a regex RE2 rejects.
	@cid=$$(docker run -d --network none $(ALLOY_CHECK_ENV) $(ALLOY_CHECK_MOUNTS) -v "$(abspath $(config)):/check/config.alloy:ro" \
		$(ALLOY_IMAGE) run --storage.path=/tmp/alloy /check/config.alloy) && sleep 6; \
	running=$$(docker inspect -f '{{.State.Running}}' $$cid); \
	[ "$$running" = true ] || docker logs $$cid 2>&1 | grep -E '^Error'; \
	docker rm -f $$cid >/dev/null; \
	[ "$$running" = true ] && echo "alloy-check: $(config) loads"

cloud-init-check: ## Validate deploy/cloud-init.yaml with Debian 13's own cloud-init (ADR-0023, #222)
	docker run --rm -e DEBIAN_FRONTEND=noninteractive -v "$(CURDIR)/deploy:/src/deploy:ro" -w /src debian:trixie sh -c \
		'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends cloud-init >/dev/null && cloud-init schema --config-file deploy/cloud-init.yaml'

backup-check: ## Round-trip deploy/backup.sh and deploy/restore-drill.sh against the local stack with a throwaway key, in Debian 13 with the host's age and rclone (#223, #224; needs `make start`)
	docker run --rm -e DEBIAN_FRONTEND=noninteractive -v /var/run/docker.sock:/var/run/docker.sock -v "$(CURDIR)/deploy:/src/deploy:ro" -w /src debian:trixie sh -c \
		'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends age rclone docker-cli >/dev/null && deploy/backup-check.sh'

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
