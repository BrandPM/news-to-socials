.PHONY: help install dev test lint format check cms-up cms-down cms-logs

PY ?= python3
PIP ?= $(PY) -m pip

help:
	@echo "Common targets:"
	@echo "  make install     - pip install -e ."
	@echo "  make dev         - install dev + ml + api extras"
	@echo "  make test        - run unit tests"
	@echo "  make lint        - ruff check"
	@echo "  make format      - ruff format"
	@echo "  make check       - lint + mypy + tests (CI)"
	@echo "  make cms-up      - start Directus + Postgres + Caddy"
	@echo "  make cms-down    - stop CMS stack"
	@echo "  make cms-logs    - tail CMS logs"

install:
	$(PIP) install -e .

dev:
	$(PIP) install -e ".[dev,ml,api]"

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .

check: lint
	$(PY) -m mypy pipeline bot
	$(PY) -m pytest tests/ -q --cov=pipeline --cov-report=term-missing

cms-up:
	cd cms && docker compose up -d

cms-down:
	cd cms && docker compose down

cms-logs:
	cd cms && docker compose logs -f --tail 100
