.PHONY: up down logs migrate instruments eval test lint reconcile shell

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

migrate:
	psql "$${DATABASE_URL_PSQL:-postgresql://groww:groww@localhost:5432/growwdesk}" -f app/store/schema.sql

instruments:
	python -m app.tools.instruments refresh

# Pull live holdings/positions and print computed P&L against the app.
# Spec §5.3: portfolio value must match the Groww app to the rupee before you trust anything.
reconcile:
	python scripts/reconcile.py

eval:
	python eval/run.py

test:
	pytest -q

lint:
	ruff check app eval scripts tests
	ruff format --check app eval scripts tests
