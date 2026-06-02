.PHONY: install dev test lint typecheck format db-up db-down db-wait migrate revision tweet clean

install:  ## Sync deps into .venv (incl. dev group)
	uv sync

dev:  ## Run the app (FastAPI + Slack Socket Mode)
	uv run uvicorn app.main:app --reload

test:  ## Run the fast test suite (excludes @pytest.mark.slow)
	uv run pytest -q -m "not slow"

test-all:  ## Run every test, including slow/integration
	uv run pytest -q

lint:  ## ruff + mypy
	uv run ruff check .
	uv run mypy app scripts

format:  ## Auto-format + autofix
	uv run ruff format .
	uv run ruff check --fix .

db-up:  ## Start Postgres
	docker compose up -d postgres

db-wait:  ## Block until Postgres is healthy
	@until docker compose exec -T postgres pg_isready -U ghostwyre >/dev/null 2>&1; do \
		echo "waiting for postgres…"; sleep 1; done; echo "postgres ready"

db-down:  ## Stop Postgres (keeps the volume)
	docker compose down

migrate:  ## Apply migrations
	uv run alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="add drafts"
	uv run alembic revision --autogenerate -m "$(m)"

tweet:  ## Phase 0 derisk: run the (dry-run) hardcoded publish
	uv run python -m scripts.post_hardcoded_tweet

clean:
	rm -rf .ruff_cache .mypy_cache .pytest_cache
