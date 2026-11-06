PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
RUFF := $(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff)
MYPY := $(if $(wildcard .venv/bin/mypy),.venv/bin/mypy,mypy)
ALEMBIC := $(if $(wildcard .venv/bin/alembic),.venv/bin/alembic,alembic)

.PHONY: up down migrate seed check lint typecheck test coverage openapi backup-restore

up:
	docker compose up --build

down:
	docker compose down

migrate:
	$(ALEMBIC) upgrade head

seed:
	$(PYTHON) -m scripts.seed

check: lint typecheck test openapi

lint:
	$(RUFF) check app tests alembic scripts examples/kim/build_examples.py
	$(RUFF) format --check app tests alembic scripts examples/kim/build_examples.py

typecheck:
	$(MYPY) app scripts/seed.py alembic/env.py

test:
	$(PYTHON) -m pytest -q

coverage:
	$(PYTHON) -m pytest -q --cov=app --cov-report=term --cov-fail-under=80

openapi:
	$(PYTHON) -m scripts.export_openapi
	$(PYTHON) -m scripts.check_openapi

backup-restore:
	bash scripts/backup_restore_test.sh
