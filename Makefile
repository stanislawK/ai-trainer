.PHONY: install test test-unit test-integration coverage lint format format-check \
	typecheck check up up-build down logs ps health migrate evals

install: ## Install dependencies (uv sync)
	uv sync

test: migrate ## Run the full test suite
	uv run pytest

test-unit: ## Run unit tests only (no external services needed)
	uv run pytest tests/unit

test-integration: migrate ## Run integration tests (needs `make up` first)
	uv run pytest tests/integration

coverage: migrate ## Line coverage report for src/ai_trainer
	uv run pytest --cov=ai_trainer --cov-report=term-missing

lint: ## Ruff lint
	uv run ruff check .

format: ## Ruff format (writes changes)
	uv run ruff format .

format-check: ## Ruff format check (no changes)
	uv run ruff format --check .

typecheck: ## mypy --strict
	uv run mypy

check: lint format-check typecheck test ## Everything CI runs

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
