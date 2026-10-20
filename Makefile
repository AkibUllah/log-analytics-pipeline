.PHONY: install test lint fmt sample ingest report html api clean

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
LOGPIPE ?= .venv/bin/logpipe

install:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

sample:
	$(LOGPIPE) generate-sample --lines 50000 --out data/access.log

ingest:
	$(LOGPIPE) ingest data/access.log

report:
	$(LOGPIPE) report --since 24h

html:
	$(LOGPIPE) report --since 24h --html report.html

api:
	$(PY) -m uvicorn logpipe.api:app --port 8080 --reload

clean:
	rm -rf .pytest_cache .ruff_cache logpipe.duckdb logpipe.duckdb.wal logpipe.checkpoint.json report.html
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
