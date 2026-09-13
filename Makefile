.PHONY: up down logs migrate instruments instruments-download doctor authcheck eval test lint reconcile adapter worker api

# Minimal dependency set for authcheck + reconcile: no psycopg, no fastapi.
# Enough to verify credentials and numbers before any infrastructure exists.
deps-reconcile:
	pip install growwapi pyotp pydantic pydantic-settings httpx

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# Compose mounts schema.sql into /docker-entrypoint-initdb.d, so on the Docker
# path the schema already exists after the first `up` and this is a no-op.
# It still matters for the Homebrew path, and for applying schema changes
# later — the named volume persists, so initdb never runs a second time.
# Docker Desktop does not put psql on the host PATH, hence the fallback.
migrate:
	@if command -v psql >/dev/null 2>&1; then \
	  psql "$${DATABASE_URL_PSQL:-postgresql://groww:groww@localhost:5432/growwdesk}" -f app/store/schema.sql; \
	else \
	  echo "psql not on PATH — applying the schema inside the postgres container"; \
	  docker compose exec -T postgres psql -U groww -d growwdesk < app/store/schema.sql; \
	fi

# Downloads the master AND loads it into Postgres — needs the database running.
instruments:
	python -m app.tools.instruments refresh

# Just fetches the CSV. No database needed, which is what reconcile wants.
instruments-download:
	python -m app.tools.instruments download

# Check every prerequisite and name the fix for each failure.
doctor:
	python scripts/doctor.py

# Verify Groww credentials on their own, before anything else.
authcheck:
	python scripts/authcheck.py

# Pull live holdings/positions and print computed P&L against the app.
# Spec §5.3: portfolio value must match the Groww app to the rupee before you trust anything.
# Downloads the instrument master by itself if absent.
reconcile:
	python scripts/reconcile.py

eval:
	python eval/run.py

test:
	pytest -q

lint:
	ruff check app eval scripts tests
	ruff format --check app eval scripts tests

# The three app processes. Run each in its own terminal from the repo root.
adapter:
	cd adapter && npm start

worker:
	python -m app.worker

api:
	uvicorn app.main:app --port 8000
