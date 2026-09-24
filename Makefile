.PHONY: install css test test-unit test-integration coverage lint format format-check \
	typecheck import-lint openapi openapi-check check up up-build down logs ps health migrate \
	evals e2e-install e2e

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

openapi: ## Regenerate the OpenAPI snapshot (docs/api/openapi.json)
	uv run python scripts/generate_openapi.py

openapi-check: openapi ## Fail if docs/api/openapi.json is out of date (CI drift check)
	git diff --exit-code -- docs/api/openapi.json

check: lint format-check typecheck import-lint openapi-check test ## Everything CI runs

up: ## Start app + postgres in the background
	docker compose up -d

up-build: ## Rebuild images, then start app + postgres
	docker compose up -d --build

down: ## Stop and remove the stack (keeps the postgres volume)
	docker compose down

logs: ## Follow container logs
	docker compose logs -f

ps: ## Show container + healthcheck status
	docker compose ps

health: ## Curl the running app's health endpoint
	curl -sS http://localhost:8000/health

migrate: ## Apply database migrations (Alembic)
	uv run alembic upgrade head

evals: ## Run prompt evals — costs money, run on purpose (ticket TBD)
	uv run ai-trainer-evals run $(template)

e2e-install: ## One-time Playwright browser download for e2e specs (no --with-deps: needs sudo, Linux-only)
	uv run playwright install chromium

e2e: migrate ## Run pytest-playwright specs against the running compose stack (needs `make up` first)
	uv run pytest tests/e2e
