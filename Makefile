.PHONY: up down logs migrate instruments instruments-download doctor authcheck eval test lint reconcile adapter worker api deps-reconcile replay shadow

# Prefer the project venv, so these targets work from a terminal where it was
# never activated. macOS has no bare `python`, only `python3`, so calling
# `python` directly fails with "No such file or directory" the moment you open
# a new shell — and this project wants several terminals open at once.
PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
                  elif command -v python3 >/dev/null 2>&1; then echo python3; \
                  else echo python; fi)

# Minimal dependency set for authcheck + reconcile: no psycopg, no fastapi.
# Enough to verify credentials and numbers before any infrastructure exists.
deps-reconcile:
	$(PYTHON) -m pip install growwapi pyotp pydantic pydantic-settings httpx

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
# Applies Phase 1 then Phase 2. Both are idempotent (everything is
# IF NOT EXISTS), so re-running is safe — but note that column *changes* to an
# existing table are not handled by either.
migrate:
	@for f in app/store/schema.sql app/store/schema_phase2.sql; do \
	  echo "applying $$f"; \
	  if command -v psql >/dev/null 2>&1; then \
	    psql "$${DATABASE_URL_PSQL:-postgresql://groww:groww@localhost:5432/growwdesk}" -f $$f; \
	  else \
	    echo "psql not on PATH — applying inside the postgres container"; \
	    docker compose exec -T postgres psql -U groww -d growwdesk < $$f; \
	  fi; \
	done

# Downloads the master AND loads it into Postgres — needs the database running.
instruments:
	$(PYTHON) -m app.tools.instruments refresh

# Just fetches the CSV. No database needed, which is what reconcile wants.
instruments-download:
	$(PYTHON) -m app.tools.instruments download

# Check every prerequisite and name the fix for each failure.
doctor:
	$(PYTHON) scripts/doctor.py

# Verify Groww credentials on their own, before anything else.
authcheck:
	$(PYTHON) scripts/authcheck.py

# Pull live holdings/positions and print computed P&L against the app.
# Spec §5.3: portfolio value must match the Groww app to the rupee before you trust anything.
# Downloads the instrument master by itself if absent.
reconcile:
	$(PYTHON) scripts/reconcile.py

eval:
	$(PYTHON) eval/run.py

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check app eval scripts tests
	$(PYTHON) -m ruff format --check app eval scripts tests

# The three app processes. Run each in its own terminal from the repo root.
adapter:
	cd adapter && npm start

worker:
	$(PYTHON) -m app.worker

# Binds loopback by default: with a tunnel, cloudflared connects outward and
# the link page never needs to be exposed. Reaching it by LAN IP instead means
# publishing a page that accepts credentials to the whole network, so that is
# opt-in: make api HOST=0.0.0.0
HOST ?= 127.0.0.1
api:
	$(PYTHON) -m uvicorn app.main:app --host $(HOST) --port 8000

# Replay a recorded shadow day through the current gate. Alert thresholds
# cannot be tuned live — a change otherwise costs a week of market to judge.
replay:
	$(PYTHON) scripts/replay.py $(if $(DAY),--day $(DAY),)

# Today's shadow report: what the watcher would have sent, and what it held
# back and why. Stays a local file on purpose.
shadow:
	@$(PYTHON) -c "from app.watcher.shadow import ShadowLog; \
	from datetime import date; \
	print(ShadowLog().report(date.today()))"
