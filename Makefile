.PHONY: install db migrate demo run test lint check

install:
	UV_CACHE_DIR=/workspace/.cache/uv uv sync --all-groups --frozen

db:
	docker compose up -d db

migrate:
	UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py migrate

demo:
	UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py seed_demo

run:
	UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py runserver 0.0.0.0:8000

test:
	UV_CACHE_DIR=/workspace/.cache/uv uv run coverage run manage.py test
	UV_CACHE_DIR=/workspace/.cache/uv uv run coverage report

lint:
	UV_CACHE_DIR=/workspace/.cache/uv uv run ruff check .

check:
	UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py check

