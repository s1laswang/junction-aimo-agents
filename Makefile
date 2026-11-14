# Judge entry points for Valio Aimo (junction-aimo-agents).
PYTHON ?= python3
UV ?= uv
PORT ?= 8000
HOST ?= 127.0.0.1

.PHONY: accept serve test fetch-data conformance mcp-check

accept:
	$(UV) run bash scripts/judge_accept.sh

serve:
	$(UV) run aimo serve --host $(HOST) --port $(PORT)

test:
	$(UV) run --extra dev pytest -q

fetch-data:
	$(UV) run aimo fetch-data

conformance:
	$(UV) run python -m conformance --base http://$(HOST):$(PORT)

mcp-check:
	$(UV) run aimo mcp-check http://$(HOST):$(PORT)/mcp
