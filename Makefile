PORT ?= 8080
PY = .venv/bin/python
DEMO_DB = var/demo.db
SCEN_PORT = $(shell echo $$(($(PORT)+1)))

.PHONY: setup check run test mcp scenarios demo scen stage

check:
	@test -e data || { echo "missing data/: run ln -s ~/aimo-agents/data data"; exit 1; }
	@test -e .env || { echo "missing .env: run ln -s ~/aimo-agents/.env .env"; exit 1; }

setup: check
	@test -e .venv || python3 -m venv .venv
	@.venv/bin/pip install -q -r requirements.txt

run: setup
	AIMO_DB=$(DEMO_DB) $(PY) -m uvicorn aimo.app:app --port $(PORT)

test: setup
	.venv/bin/pytest -q

mcp: setup
	$(PY) -m aimo.mcp_server

scenarios:
	$(PY) scenarios/run_all.py --base http://localhost:$(PORT)

demo: setup scen stage

scen:
	@mkdir -p var; rm -f var/scen.db; \
	AIMO_DB=var/scen.db $(PY) -m uvicorn aimo.app:app --port $(SCEN_PORT) > var-scen.log 2>&1 & \
	pid=$$!; trap 'kill $$pid 2>/dev/null' EXIT INT TERM; \
	ok=0; for i in $$(seq 30); do \
	  curl -sf http://localhost:$(SCEN_PORT)/.well-known/agent-card.json >/dev/null && { ok=1; break; }; sleep 1; done; \
	[ $$ok = 1 ] || { echo "scenario server did not start, see var-scen.log"; exit 1; }; \
	$(MAKE) --no-print-directory scenarios PORT=$(SCEN_PORT); true

stage: setup
	@rm -f $(DEMO_DB); mkdir -p var; \
	AIMO_DB=$(DEMO_DB) $(PY) -m uvicorn aimo.app:app --port $(PORT) > var-demo.log 2>&1 & \
	pid=$$!; trap 'kill $$pid 2>/dev/null' EXIT INT TERM; \
	ok=0; for i in $$(seq 30); do \
	  curl -sf http://localhost:$(PORT)/.well-known/agent-card.json >/dev/null && { ok=1; break; }; sleep 1; done; \
	[ $$ok = 1 ] || { echo "server did not start in 30s, see var-demo.log"; exit 1; }; \
	echo; echo "Console: http://localhost:$(PORT)/console.html"; \
	echo "Inbox:   http://localhost:$(PORT)/inbox.html"; \
	echo "Audit:   http://localhost:$(PORT)/audit"; \
	echo "Ctrl-C to stop"; wait $$pid
