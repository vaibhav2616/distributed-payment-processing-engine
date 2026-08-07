.PHONY: dev migrate-up migrate-down lint format

dev:
	uv run fastapi dev main.py

migrate-up:
	uv run alembic upgrade head

migrate-down:
	uv run alembic downgrade -1

lint:
	uv run ruff check .

format:
	uv run ruff format .
